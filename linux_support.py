"""Linux optical-drive integration, using sysfs, udev and the kernel CD-ROM ABI.

No drive commands are issued during discovery: udev's cached media properties let
other drives keep ripping. Hardware reads happen only when reading an audio TOC.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SYS_BLOCK = Path('/sys/class/block')
SYS_DEV = Path('/sys/dev/block')
MOUNTINFO = Path('/proc/self/mountinfo')


def optical_device(device: str) -> str:
    """Resolve aliases and reject anything except a kernel optical block device."""
    path = Path(device).resolve(strict=True)
    info = path.stat()
    if not stat.S_ISBLK(info.st_mode):
        raise ValueError(f'{device} is not an optical drive')
    kind = SYS_DEV / f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}' / 'device/type'
    if kind.read_text().strip() != '5':
        raise ValueError(f'{device} is not an optical drive')
    return str(path)


def mounted_root(device: str) -> str | None:
    """Find a visible whole-disc mount by device number, never by label or path spelling.

    mountinfo belongs to this process's mount namespace. Full-root bind mounts
    are usable; subtree binds and mounts hidden by another filesystem are not.
    This only reads metadata and does not mount media or change permissions.
    """
    def unescape(value):
        return re.sub(r'\\(040|011|012|134)', lambda m: chr(int(m[1], 8)), value)

    try:
        info = Path(optical_device(device)).stat()
        number = f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}'
        lines = MOUNTINFO.read_text(encoding='utf-8', errors='surrogateescape').splitlines()
        for line in lines:
            fields = line.partition(' - ')[0].split()
            if ' - ' not in line or len(fields) < 6 or fields[2] != number or unescape(fields[3]) != '/':
                continue
            root = Path(unescape(fields[4]))
            if not root.is_absolute():
                continue
            try:
                mounted = root.stat()
                if (stat.S_ISDIR(mounted.st_mode) and mounted.st_dev == info.st_rdev
                        and os.access(root, os.R_OK | os.X_OK)):
                    return str(root)
            except OSError:  # inaccessible, stale or hidden; another mount may work
                continue
    except (OSError, ValueError):  # removed drive, unavailable procfs or not an optical device
        pass
    return None


def _decode_label(value: str) -> str:
    # udev encodes unsafe bytes (including UTF-8) as \xNN.
    raw = re.sub(rb'\\x([0-9a-fA-F]{2})', lambda m: bytes([int(m[1], 16)]), value.encode('utf-8'))
    return raw.decode('utf-8', errors='replace')


def access_problem(device: str) -> str:
    """Explain missing device access without changing permissions or loading drivers."""
    if not os.access(device, os.R_OK | os.W_OK):
        return f'No read/write access to {device}. Grant your user optical-drive access through your system permissions.'
    generic = SYS_BLOCK / Path(device).name / 'device/scsi_generic'
    nodes = list(generic.glob('sg*'))
    if not nodes:
        return f'{device} has no SCSI-generic device. Enable the Linux sg driver before using MakeMKV.'
    for node in nodes:
        path = '/dev/' + node.name
        if not os.access(path, os.R_OK | os.W_OK):
            return f'No read/write access to {path}, the SCSI-generic device for {device}. Grant access to this optical drive only.'
    return ''


def scan_drives() -> list[dict] | None:
    """None means discovery is unavailable; [] means no optical drives exist."""
    if not SYS_BLOCK.is_dir():
        return None
    drives = []
    for block in sorted(SYS_BLOCK.iterdir()):
        try:
            if (block / 'device/type').read_text().strip() != '5':
                continue
        except FileNotFoundError:  # non-SCSI device, or unplugged during discovery
            continue
        udev = shutil.which('udevadm')
        if not udev:
            return None
        result = subprocess.run([udev, 'info', '--query=property', '--path', str(block)],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise OSError(f'Could not read optical drive properties for {block.name}: {result.stderr.strip()}')
        props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        device = '/dev/' + block.name
        loaded = props.get('ID_CDROM_MEDIA') == '1'
        audio = int(props.get('ID_CDROM_MEDIA_TRACK_COUNT_AUDIO') or 0) > 0
        label = _decode_label(props.get('ID_FS_LABEL_ENC', props.get('ID_FS_LABEL', '')))
        drives.append({'index': len(drives), 'device': device, 'loaded': loaded,
                       'drive_name': props.get('ID_MODEL', block.name).replace('_', ' '),
                       'disc_label': label if loaded else '', 'audio': loaded and audio,
                       'access_error': access_problem(device)})
    return drives


def eject(device: str) -> None:
    device = optical_device(device)
    exe = shutil.which('eject')
    if not exe:
        raise OSError('Install eject to open the disc tray on Linux.')
    result = subprocess.run([exe, '--', device], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise OSError(f'Could not eject {device}: {result.stderr.strip() or "drive is busy or access was denied"}')


def read_toc(device: str) -> bytes:
    """Read Linux cdrom_tochdr/tocentry and return the existing TOC parser's wire format.

    ABI: include/uapi/linux/cdrom.h (CDROMREADTOCHDR, CDROMREADTOCENTRY,
    CDROM_MSF). The address union is aligned at byte 4, and the struct is 12 bytes.
    """
    import fcntl

    device = optical_device(device)
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except PermissionError as exc:
        raise PermissionError(f'Cannot read {device}. Grant your user access to the optical drive, then sign in again.') from exc
    try:
        header = bytearray(2)
        fcntl.ioctl(fd, 0x5305, header, True)
        first, last = header
        if not 1 <= first <= last <= 99:
            raise OSError(f'Invalid CD table of contents from {device}')
        raw = bytearray([0, 0, first, last])
        for track in [*range(first, last + 1), 0xAA]:
            entry = bytearray(12)
            entry[0], entry[2] = track, 2  # CDROM_MSF
            fcntl.ioctl(fd, 0x5306, entry, True)
            control = entry[1] >> 4 if sys.byteorder == 'little' else entry[1] & 15
            raw.extend([0, control, track, 0, 0, *entry[4:7]])
        return bytes(raw)
    finally:
        os.close(fd)
