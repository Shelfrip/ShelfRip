"""Native macOS dialogs and notifications using system AppleScript components."""
from __future__ import annotations

from pathlib import Path
import subprocess


def _script(source, *args, timeout=30):
    # Arguments are data, never interpolated into executable AppleScript source.
    return subprocess.run(["/usr/bin/osascript", "-", *args], input=source,
                          capture_output=True, text=True, timeout=timeout)


def pick_path(kind, start, title):
    if kind not in ("folder", "file"):
        return None, "Choose a file or folder."
    initial = Path(start).expanduser().absolute() if start else Path.home()
    if not initial.is_dir():
        initial = initial.parent
    source = '''on run argv
    try
        set chosen to choose KIND with prompt (item 1 of argv) default location (POSIX file (item 2 of argv))
        return POSIX path of chosen
    on error number -128
        return ""
    end try
end run'''.replace("KIND", kind)
    try:
        result = _script(source, title or "Choose a " + kind, str(initial), timeout=600)
        if result.returncode:
            return None, "Couldn't open the macOS picker. Run in a graphical login session or type the path instead."
        return result.stdout.rstrip("\r\n"), ""
    except (OSError, subprocess.TimeoutExpired):
        return None, "The macOS picker did not respond. Type the path instead."


def notify(title, text):
    try:
        return _script('on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run',
                       title, text, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
