r"""
build_installer.py - makes the Shelfrip installer you give to other people.

Run "Build Installer.bat" (it runs this). It:
  1. installs PyInstaller (one time),
  2. builds installer\dist\Shelfrip\ : Shelfrip.exe with Python and the whole app inside,
  3. zips that folder as a portable copy, and
  4. if Inno Setup 6 is installed, makes installer\Shelfrip Setup <version>.exe from installer.iss.

Inno Setup is free: https://jrsoftware.org/isdl.php (or: winget install JRSoftware.InnoSetup).
Your settings and any copy you run from the scripts aren't touched.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "installer"
NAME = "Shelfrip"
MODULES = ["app", "review", "identify", "rip_all", "split", "music", "plex", "extras", "discdb",
           "catalog", "crosscheck", "naming", "paths", "community", "desktop", "progress", "health", "tidy", "dupes", "trouble", "importer", "ripfiles",
           "subcheck", "epmatch", "refsubs", "handbrake", "processes", "background",
           "linux_support", "linux_tray", "linux_keyring", "linux_window", "linux_install", "linux_update"]
PORTABLE_NOTE = ("This file makes Shelfrip portable: it keeps your settings, shelf and disc database in this\r\n"
                 "folder instead of %APPDATA%\\Shelfrip. Delete it to use the same settings as an installed Shelfrip.\r\n")
SHIP = ["README.md", "LICENSE.txt", "THIRD-PARTY-NOTICES.txt"]


def version() -> str:
    return re.search(r'APP_VERSION = "([^"]+)"', (HERE / "paths.py").read_text(encoding="utf-8")).group(1)


def run(cmd: list[str]) -> None:
    print(">", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    subprocess.run(cmd, check=True)


def find_iscc() -> str | None:
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base and Path(base, "Inno Setup 6", "ISCC.exe").is_file():
            return str(Path(base, "Inno Setup 6", "ISCC.exe"))
    return None


def main() -> int:
    ver = version()
    print(f"Building {NAME} {ver}")
    if "--skip-pip" not in sys.argv:
        run([sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"])
    dist, work = OUT / "dist", OUT / "work"
    for d in (dist, work):
        shutil.rmtree(d, ignore_errors=True)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--name", NAME,
           "--distpath", str(dist), "--workpath", str(work), "--specpath", str(work),
           "--paths", str(HERE), "--add-data", f"{HERE / 'review.html'}{os.pathsep}."]
    for m in MODULES:
        cmd += ["--hidden-import", m]
    icon = HERE / "icon.ico"
    if not icon.exists():
        sys.path.insert(0, str(HERE))
        import desktop  # draws the Shelfrip mark
        desktop.write_icon(icon)
    cmd += ["--icon", str(icon)]
    run(cmd + [str(HERE / "launcher.py")])

    app_dir = dist / NAME
    for f in SHIP:
        if (HERE / f).exists():
            shutil.copy2(HERE / f, app_dir / f)
    for tool in ("mkvtoolnix", "cyanrip"):  # empty folders (with how-to) for portable tool copies
        (app_dir / "tools" / tool).mkdir(parents=True, exist_ok=True)
        if (HERE / "tools" / tool / "README.txt").exists():
            shutil.copy2(HERE / "tools" / tool / "README.txt", app_dir / "tools" / tool / "README.txt")
    leftovers = [p.name for p in app_dir.glob("*.py")]
    if leftovers:  # the built-in copy must be the one that runs
        print(f"! unexpected .py files in the build: {leftovers}")
        return 1

    zip_base = OUT / f"{NAME} {ver} (portable)"
    shutil.make_archive(str(zip_base), "zip", dist, NAME)
    with zipfile.ZipFile(f"{zip_base}.zip", "a") as z:  # only the zip is portable, not the installer
        z.writestr(f"{NAME}/portable.txt", PORTABLE_NOTE)
    print(f"Portable copy: {zip_base}.zip")

    if "--no-installer" in sys.argv:
        return 0
    iscc = find_iscc()
    if not iscc:
        print("\nInno Setup 6 isn't installed, so only the portable zip was made.\n"
              "Install it from https://jrsoftware.org/isdl.php (or: winget install JRSoftware.InnoSetup),\n"
              "then run Build Installer.bat again to get the Setup exe.")
        return 0
    run([iscc, f"/DAppVersion={ver}", f"/DSourceDir={app_dir}", f"/DOutputDir={OUT}", str(HERE / "installer.iss")])
    print(f"\nInstaller: {OUT / f'{NAME} Setup {ver}.exe'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        print(f"\nThe build failed ({exc}). Copy the messages above to Claude.")
        sys.exit(1)
