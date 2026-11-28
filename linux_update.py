"""Verified release downloads for the user-local Linux installation."""
import hashlib
from pathlib import Path
import re
import tempfile
import urllib.request

import linux_install


def trusted_url(url: str, repo: str) -> bool:
    return url.startswith(f"https://github.com/{repo}/releases/download/") and "?" not in url and "#" not in url


def release_asset(asset: dict, version: str, repo: str) -> bool:
    return (asset.get("name") == f"Shelfrip-{version}-linux.tar.gz"
            and trusted_url(str(asset.get("browser_download_url") or ""), repo)
            and bool(re.fullmatch(r"sha256:[a-fA-F0-9]{64}", str(asset.get("digest") or ""))))


def install(update: dict, repo: str, progress, before_install=lambda: None) -> Path:
    url, version = update.get("installer", ""), update.get("latest", "")
    digest = update.get("digest", "")
    if not trusted_url(url, repo) or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
        raise ValueError("The release has no trusted Linux package checksum.")
    with tempfile.TemporaryDirectory(prefix="shelfrip-update-") as tmp:
        archive = Path(tmp) / "release.tar.gz"
        request = urllib.request.Request(url, headers={"User-Agent": "Shelfrip Linux updater"})
        hashed, size = hashlib.sha256(), 0
        with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > linux_install.MAX_PACKAGE:
                    raise ValueError("The release archive is too large.")
                hashed.update(chunk)
                output.write(chunk)
                progress(size)
        if (update.get("size") and size != update["size"]) or hashed.hexdigest() != digest[7:].lower():
            raise ValueError("Linux package checksum or size did not match. The installed version was kept.")
        before_install()
        return linux_install.install_archive(archive, version) / "app.py"
