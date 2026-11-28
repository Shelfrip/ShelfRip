"""Build the architecture-independent Linux archive: python3 build_linux.py [output-dir]."""
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile

MODULES = ("app review identify rip_all split music plex extras discdb catalog crosscheck naming paths "
           "community desktop progress health tidy dupes trouble importer ripfiles subcheck epmatch refsubs "
           "processes background linux_support linux_tray linux_keyring linux_window linux_install linux_update").split()


def build(source: Path, output: Path) -> Path:
    version = re.search(r'^APP_VERSION = "([^"]+)"', (source / "paths.py").read_text(), re.M)[1]
    files = {name + ".py": (source / (name + ".py")).read_bytes() for name in MODULES}
    for name in ("review.html", "README.md", "LICENSE.txt", "THIRD-PARTY-NOTICES.txt", "docs/linux.md"):
        if (source / name).is_file():
            files[name] = (source / name).read_bytes()
    manifest = {"version": version, "files": {n: hashlib.sha256(b).hexdigest() for n, b in sorted(files.items())}}
    files["linux-package.json"] = json.dumps(manifest, sort_keys=True, indent=2).encode()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"Shelfrip-{version}-linux.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo("shelfrip/" + name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    target.with_suffix(target.suffix + ".sha256").write_text(hashlib.sha256(target.read_bytes()).hexdigest() + "  " + target.name + "\n")
    return target


if __name__ == "__main__":
    print(build(Path(__file__).resolve().parent, Path(sys.argv[1]) if len(sys.argv) > 1 else Path("installer")))
