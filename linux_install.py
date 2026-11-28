"""User-local Linux installation and XDG desktop integration (standard library only)."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sys
import tarfile
import tempfile

DESKTOP_ID = "org.shelfrip.Shelfrip.desktop"
MANIFEST = "linux-package.json"
MAX_PACKAGE = 64 * 1024 * 1024
ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28"><path fill="#f2b33d" d="M3 4h5v18H3z"/><path fill="#6c9bff" d="M10 7h5v15h-5z"/><path fill="#4cc38a" d="M17 5h5v17h-5z"/><path fill="#c9d1dc" d="M2 23h24v2.5H2z"/></svg>'
_running_lock = None


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def install_root() -> Path:
    return data_home() / "shelfrip"


def executable() -> Path:
    return Path.home() / ".local/bin/shelfrip"


def atomic_text(path: Path, value: str, mode=0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".shelfrip-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
            os.fchmod(f.fileno(), mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def exec_arg(value: str) -> str:
    """Desktop-entry quoting is distinct from shell quoting (two escape layers)."""
    if any(c in value for c in "\n\r\0"):
        raise ValueError("Desktop command paths cannot contain line breaks or NUL.")
    value = value.replace("%", "%%")
    quoted = "".join("\\" + c if c in '\\"`$' else c for c in value)
    return '"' + quoted.replace("\\", "\\\\") + '"'


def entry(command: list[str], icon: Path, *, autostart=False) -> str:
    return ("[Desktop Entry]\nType=Application\nName=Shelfrip\nComment=Rip and organise your discs\n"
            + "Exec=" + " ".join(exec_arg(s) for s in command) + "\n"
            + "Icon=" + str(icon).replace("\\", "\\\\").replace("\n", "\\n") + "\n"
            + "Terminal=false\nCategories=AudioVideo;\nStartupNotify=true\nStartupWMClass=Shelfrip\n"
            + ("X-GNOME-Autostart-enabled=true\n" if autostart else ""))


def login_enabled() -> bool:
    path = config_home() / "autostart" / DESKTOP_ID
    try:
        text = path.read_text()
        return "Hidden=true" not in text and "X-GNOME-Autostart-enabled=false" not in text
    except OSError:
        return False


def set_login(enabled: bool) -> None:
    target = config_home() / "autostart" / DESKTOP_ID
    if not enabled:
        target.unlink(missing_ok=True)
        return
    if not (install_root() / "current/app.py").is_file() or not executable().is_file():
        raise ValueError("Install Shelfrip's Linux package before enabling start at login.")
    atomic_text(target, entry([str(executable()), "--background"], install_root() / "icon.svg", autostart=True))


def installed(app_dir: Path) -> bool:
    try:
        root = install_root().resolve()
        actual = app_dir.resolve()
        return actual.parent == root / "versions" and (actual / MANIFEST).is_file()
    except OSError:
        return False


def hold_running_lock() -> None:
    """Prevent uninstalling modules beneath an installed, running application."""
    import fcntl
    global _running_lock
    _running_lock = (install_root() / ".running.lock").open("a")
    fcntl.flock(_running_lock, fcntl.LOCK_SH)


def verify_tree(source: Path) -> dict:
    manifest = json.loads((source / MANIFEST).read_text(encoding="utf-8"))
    version = manifest.get("version", "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*", version):
        raise ValueError("Invalid Linux package version.")
    files = manifest.get("files")
    if not isinstance(files, dict) or not {"app.py", "paths.py", "review.html", "linux_install.py"} <= files.keys():
        raise ValueError("Incomplete Linux package.")
    for name, digest in files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or str(path) != name:
            raise ValueError("Invalid package path.")
        file = source / name
        if file.is_symlink() or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Package integrity check failed: {name}")
        if name.endswith(".py"):
            compile(file.read_bytes(), name, "exec")
    declared = re.search(r'^APP_VERSION = "([^"]+)"', (source / "paths.py").read_text(), re.M)
    if not declared or declared[1] != version:
        raise ValueError("Package version does not match its application.")
    return manifest


@contextlib.contextmanager
def install_lock():
    import fcntl
    root = install_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def install(source: Path) -> Path:
    """Validate/copy into an immutable version, then atomically select it.

    Old versions are retained, so a running app's imports cannot change mid-run.
    User settings and media are outside the installation tree.
    """
    source = source.resolve()
    manifest = verify_tree(source)
    root = install_root()
    token = hashlib.sha256((source / MANIFEST).read_bytes()).hexdigest()[:12]
    target = root / "versions" / f"{manifest['version']}-{token}"
    with install_lock():
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            with tempfile.TemporaryDirectory(prefix=".stage-", dir=target.parent) as tmp:
                stage = Path(tmp) / "app"
                stage.mkdir()
                for name in [*manifest["files"], MANIFEST]:
                    dst = stage / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / name, dst)
                    dst.chmod(0o644)
                verify_tree(stage)
                stage.rename(target)
        else:
            verify_tree(target)
        # Stable paths let desktop entries and login entries survive every update.
        launcher = ("#!/bin/sh\n# Shelfrip managed launcher\nexec " + shlex.quote(sys.executable)
                    + " " + shlex.quote(str(root / "current/app.py")) + ' --desktop-launch "$@"\n')
        atomic_text(executable(), launcher, 0o755)
        atomic_text(root / "icon.svg", ICON)
        atomic_text(data_home() / "applications" / DESKTOP_ID,
                    entry([str(executable())], root / "icon.svg"))
        link = root / ".current-next"
        link.unlink(missing_ok=True)
        link.symlink_to(target.relative_to(root), target_is_directory=True)
        link.replace(root / "current")
    return target


def install_archive(archive: Path, expected_version: str) -> Path:
    """Extract only regular files under one package root; never follow archive links."""
    with tempfile.TemporaryDirectory(prefix="shelfrip-package-") as tmp:
        source = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            seen, total = set(), 0
            for member in tar:
                total += member.size
                if len(seen) >= 1000 or total > MAX_PACKAGE:
                    raise ValueError("Linux package is too large.")
                name = PurePosixPath(member.name)
                if (not member.isfile() or name.is_absolute() or ".." in name.parts
                        or "\\" in member.name or str(name) != member.name or len(name.parts) < 2 or name.parts[0] != "shelfrip"
                        or member.name in seen):
                    raise ValueError("Unsafe Linux package entry.")
                seen.add(member.name)
                destination = source.joinpath(*name.parts[1:])
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, destination.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        manifest = verify_tree(source)
        if manifest["version"] != expected_version:
            raise ValueError("Downloaded package has the wrong version.")
        return install(source)


def uninstall() -> None:
    """Remove our launchers and installed code; retain settings, keyring and media."""
    import fcntl
    with install_lock(), (install_root() / ".running.lock").open("a") as running:
        try:
            fcntl.flock(running, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Quit Shelfrip before uninstalling it.") from None
        set_login(False)
        (data_home() / "applications" / DESKTOP_ID).unlink(missing_ok=True)
        if executable().exists() and "# Shelfrip managed launcher" in executable().read_text():
            executable().unlink()
        root = install_root()
        (root / "current").unlink(missing_ok=True)
        (root / "icon.svg").unlink(missing_ok=True)
        shutil.rmtree(root / "versions", ignore_errors=True)


def main():
    if not sys.platform.startswith("linux") or sys.version_info < (3, 10):
        raise SystemExit("The Linux package needs Linux and Python 3.10 or later.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autostart", action="store_true", help="start in the background at graphical login")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    if args.uninstall:
        uninstall()
        print("Shelfrip removed. Your settings and media were kept.")
    else:
        install(Path(__file__).resolve().parent)
        if args.autostart:
            set_login(True)
        print(f"Installed. Open Shelfrip from your app menu or run {executable()}.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
