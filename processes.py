"""Own Linux rip processes even if the application exits abruptly.

A small supervisor watches a pipe held only by Shelfrip. EOF (including SIGKILL
of Shelfrip) stops the child's process group. This avoids preexec_fn in a threaded
application and does not depend on an init system or third-party Python modules.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

_lock = threading.Lock()
_children: dict[subprocess.Popen, int | None] = {}
_stopping = False


def spawn(args, **kwargs) -> subprocess.Popen:
    if not sys.platform.startswith("linux"):
        return subprocess.Popen(args, **kwargs)
    with _lock:
        if _stopping:
            raise OSError("Shelfrip is shutting down; no new rip can start.")
        reader, writer = os.pipe()
        try:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(reader), *args],
                                    pass_fds=(reader,), start_new_session=True, **kwargs)
        except BaseException:
            os.close(writer)
            raise
        finally:
            os.close(reader)
        _children[proc] = writer

    def reap():
        proc.wait()
        with _lock:
            fd = _children.pop(proc, None)
            if fd is not None:
                os.close(fd)

    threading.Thread(target=reap, daemon=True, name="rip-process-reaper").start()
    return proc


def shutdown() -> None:
    """Prevent new rips and wait for all supervised process groups to stop."""
    global _stopping
    with _lock:
        _stopping = True
        children = list(_children)
        for proc, fd in _children.items():
            if fd is not None:
                os.close(fd)
                _children[proc] = None
    deadline = time.monotonic() + 5
    for proc in children:
        try:
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            # The closed pipe stays closed: the supervisor will still stop the
            # group once a temporarily uninterruptible device operation returns.
            pass


def _supervise(reader: int, args: list[str]) -> int:
    import select

    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True  # signal handlers must not acquire threading locks

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if select.select([reader], [], [], 0)[0]:
        return 130  # parent already gone; don't start work
    try:
        child = subprocess.Popen(args, start_new_session=True)
    except OSError as exc:
        print(f"Could not start {args[0]}: {exc}", file=sys.stderr, flush=True)
        return 127

    def send(sig):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass

    try:
        while child.poll() is None:
            if stopping or select.select([reader], [], [], 0.1)[0]:
                send(signal.SIGTERM)
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                return 130
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    finally:
        # Include descendants that ignored TERM or outlived the main executable.
        send(signal.SIGKILL)
        child.wait()
        os.close(reader)


if __name__ == "__main__":
    raise SystemExit(_supervise(int(sys.argv[1]), sys.argv[2:]))
