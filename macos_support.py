"""macOS optical devices: DiscRecording's cached state, diskutil and IOKit TOCs."""
from __future__ import annotations

import contextlib
import ctypes as C
import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess

from macos_native import CoreFoundation, bind, framework


def device_id(info: dict) -> str:
    # The media's BSD name disappears on eject and can change on reinsertion.
    # The physical registry path remains the same while the drive stays connected.
    path = info.get("DRDeviceIORegistryEntryPathKey", "")
    if not path:
        raise OSError("The optical drive has no registry identity.")
    return "macos:" + hashlib.sha256(path.encode()).hexdigest()[:24]


@contextlib.contextmanager
def devices():
    cf, dr = CoreFoundation(), framework("DiscRecording")
    array = bind(dr, "DRCopyDeviceArray", C.c_void_p)()
    if not array:
        raise OSError("macOS optical-drive discovery is unavailable.")
    try:
        rows = []
        for i in range(cf.count(array)):
            ref = cf.at(array, i)
            info = cf.take(bind(dr, "DRDeviceCopyInfo", C.c_void_p, C.c_void_p)(ref)) or {}
            status = cf.take(bind(dr, "DRDeviceCopyStatus", C.c_void_p, C.c_void_p)(ref)) or {}
            rows.append((ref, info, status))
        yield dr, rows
    finally:
        cf.release(array)


def _bsd(status):
    name = (status.get("DRDeviceMediaInfoKey") or {}).get("DRDeviceMediaBSDNameKey", "")
    return "/dev/" + name if re.fullmatch(r"disk[0-9]+", name) else ""


def _matches(device, info, status):
    if device == device_id(info):
        return True
    # Resolve only names belonging to a currently enumerated optical device.
    return bool(re.fullmatch(r"/dev/r?disk[0-9]+", device)
                and device.replace("/dev/rdisk", "/dev/disk") == _bsd(status))


def media_device(device: str) -> str:
    with devices() as (_, rows):
        for _, info, status in rows:
            if _matches(device, info, status):
                if name := _bsd(status):
                    return name
                raise OSError("There is no readable media in this optical drive.")
    raise ValueError("That optical drive is no longer connected.")


def _plist(args):
    result = subprocess.run(args, capture_output=True, timeout=15)
    if result.returncode:
        raise OSError("macOS could not read the optical media information.")
    try:
        return plistlib.loads(result.stdout)
    except (ValueError, plistlib.InvalidFileException):
        raise OSError("macOS returned invalid optical media information.") from None


def _volumes(bsd):
    """Volumes belonging to this optical disk, including partitioned UDF/HFS media."""
    tree = _plist(["/usr/sbin/diskutil", "list", "-plist", bsd])
    names = [bsd.removeprefix("/dev/")]
    def visit(node):
        if isinstance(node, dict):
            name = node.get("DeviceIdentifier", "")
            if re.fullmatch(re.escape(names[0]) + r"(?:s[0-9]+)+", name):
                names.append(name)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(tree)
    return [_plist(["/usr/sbin/diskutil", "info", "-plist", "/dev/" + name]) for name in dict.fromkeys(names)]


def mounted_root(device: str) -> str | None:
    try:
        for volume in _volumes(media_device(device)):
            root = Path(volume.get("MountPoint") or "")
            if root.is_absolute() and root.is_dir() and os.access(root, os.R_OK | os.X_OK):
                return str(root)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def _cd_tocs():
    tree = _plist(["/usr/sbin/ioreg", "-a", "-r", "-c", "IOCDMedia"])
    found = {}
    def visit(node):
        if isinstance(node, dict):
            name, toc = node.get("BSD Name", ""), node.get("TOC")
            if re.fullmatch(r"disk[0-9]+", name) and isinstance(toc, bytes):
                found["/dev/" + name] = toc
            for child in node.get("IORegistryEntryChildren", []):
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(tree)
    return found


def parse_toc(raw: bytes) -> bytes:
    """Convert IOKit's full session TOC (IOCDTypes.h) to music.parse_toc's format.

    Preserve all sessions so the shared parser can exclude enhanced-CD data
    tracks and derive the audio lead-out. IOKit numbers are binary, not BCD.
    """
    size = int.from_bytes(raw[:2], "big") + 2
    if len(raw) < 4 or size > len(raw) or size < 4 or (size - 4) % 11:
        raise ValueError("Invalid macOS CD table of contents.")
    tracks, leadouts = {}, {}
    for pos in range(4, size, 11):
        e = raw[pos:pos + 11]
        if e[1] >> 4 != 1 or not raw[2] <= e[0] <= raw[3]:
            continue
        point, msf = e[3], e[8:11]
        if point == 0xA2 or 1 <= point <= 99:
            if msf[1] >= 60 or msf[2] >= 75:
                raise ValueError("Invalid CD track address.")
            if point == 0xA2:
                leadouts[e[0]] = bytes([0, 0, 0xAA, 0, 0, *msf])
            else:
                track = bytes([0, e[1] & 15, point, 0, 0, *msf])
                if point in tracks and tracks[point] != track:
                    raise ValueError("Conflicting CD track addresses.")
                tracks[point] = track
    if not tracks or raw[3] not in leadouts:
        raise ValueError("Incomplete macOS CD table of contents.")
    first, last, leadout = min(tracks), max(tracks), leadouts[raw[3]]
    if any(n not in tracks for n in range(first, last + 1)):
        raise ValueError("Incomplete macOS CD table of contents.")
    addresses = [tuple(tracks[n][5:8]) for n in range(first, last + 1)] + [tuple(leadout[5:8])]
    if any(a >= b for a, b in zip(addresses, addresses[1:])):
        raise ValueError("Invalid CD track order.")
    return bytes([0, 0, first, last]) + b"".join(tracks[n] for n in range(first, last + 1)) + leadout


def read_toc(device: str) -> bytes:
    bsd = media_device(device)
    return parse_toc(_cd_tocs().get(bsd, b""))


def scan_drives() -> list[dict]:
    found = []
    with devices() as (_, rows):
        for _, info, status in rows:
            loaded = status.get("DRDeviceMediaStateKey") == "DRDeviceMediaStateMediaPresent"
            media = status.get("DRDeviceMediaInfoKey") or {}
            loaded = loaded and not media.get("DRDeviceMediaIsBlankKey", False)
            bsd = _bsd(status)
            label, audio = "", False
            if loaded and bsd:
                try:
                    volumes = _volumes(bsd)
                    label = next((v.get("VolumeName") for v in volumes if v.get("VolumeName")), "")
                    audio = any(v.get("FilesystemType") == "cddafs" for v in volumes)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass  # an unmounted disc is still available to MakeMKV
                if str(media.get("DRDeviceMediaTypeKey", "")).startswith("DRDeviceMediaTypeCD"):
                    try:
                        toc = parse_toc(_cd_tocs().get(bsd, b""))
                        audio = any(not toc[i + 1] & 4 for i in range(4, len(toc) - 8, 8))
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        pass
            found.append({"index": len(found), "device": device_id(info), "loaded": loaded,
                          "drive_name": " ".join(str(info.get(k, "")).strip() for k in
                              ("DRDeviceVendorNameKey", "DRDeviceProductNameKey")).strip(),
                          "disc_label": label, "audio": audio, "media_device": bsd})
    return found


def eject(device: str) -> None:
    with devices() as (dr, rows):
        for ref, info, status in rows:
            if _matches(device, info, status):
                result = bind(dr, "DRDeviceEjectMedia", C.c_int32, C.c_void_p)(ref)
                if result:
                    raise OSError(f"macOS could not eject the optical drive (status {result}); it may be busy.")
                return
    raise ValueError("That optical drive is no longer connected; nothing was ejected.")


def process_ids(names: set[str]) -> list[int]:
    """Only this user's actual executable paths, never a match in shell arguments."""
    result = subprocess.run(["/bin/ps", "-axo", "pid=,uid="], capture_output=True, text=True, timeout=10)
    if result.returncode:
        return []
    proc = C.CDLL("/usr/lib/libproc.dylib")
    pidpath = bind(proc, "proc_pidpath", C.c_int, C.c_int, C.c_void_p, C.c_uint32)
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts) or int(parts[1]) != os.getuid():
            continue
        pid = int(parts[0])
        buf = C.create_string_buffer(4096)
        if pidpath(pid, buf, len(buf)) > 0 and Path(os.fsdecode(buf.value)).name in names:
            found.append(pid)
    return found
