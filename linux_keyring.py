"""Secret Service storage through libsecret's secret-tool (no Python dependencies)."""
from __future__ import annotations

import re
import shutil
import subprocess
import uuid

PREFIX = "secret-service:"
WARNING = ""


class KeyringError(ValueError):
    pass


def is_reference(value: str) -> bool:
    return bool(re.fullmatch(r"secret-service:[0-9a-f]{32}", value or ""))


def _run(action: str, reference: str, password: str | None = None) -> str:
    if not is_reference(reference):
        raise KeyringError("Invalid password reference. Save the password again.")
    tool = shutil.which("secret-tool")
    if not tool:
        raise KeyringError("Install secret-tool (libsecret tools) and enable a Secret Service keyring to save or read passwords.")
    args = [tool, action]
    if action == "store":
        args += ["--label=Shelfrip OpenSubtitles password"]
    args += ["application", "shelfrip", "credential", reference[len(PREFIX):]]
    try:
        result = subprocess.run(args, input=(password or "").encode("utf-8"),
                                capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise KeyringError("The desktop keyring did not respond. Unlock it and try again.") from None
    if result.returncode:
        # Backend output can include sensitive data; never put it in logs or API errors.
        raise KeyringError("The desktop keyring could not complete the request. Unlock it, or save the password again if the item was removed.")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise KeyringError("The keyring item could not be read. Save the password again.") from None


def store(password: str) -> str:
    reference = PREFIX + uuid.uuid4().hex
    try:
        _run("store", reference, password)
        if lookup(reference) != password:
            raise KeyringError("The keyring did not return the saved password. Your settings were not changed.")
    except KeyringError:
        cleanup(reference)
        raise
    return reference


def lookup(reference: str) -> str:
    return _run("lookup", reference)


def cleanup(reference: str) -> None:
    """Best-effort deletion after a commit/rollback; never mask the original error."""
    global WARNING
    if not is_reference(reference):
        return
    try:
        _run("clear", reference)
        WARNING = ""
    except KeyringError:
        WARNING = "An unused Shelfrip password item could not be removed. You can remove it in your desktop keyring manager."


def status() -> dict:
    return {"helper_available": bool(shutil.which("secret-tool")), "warning": WARNING}
