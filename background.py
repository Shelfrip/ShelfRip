"""Start a detached Linux instance and receive its actual bound port over a pipe."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time


def launch(command: list[str], log: Path) -> dict:
    log.parent.mkdir(parents=True, exist_ok=True)
    reader, writer = os.pipe()
    try:
        with os.fdopen(os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "ab") as output:
            os.fchmod(output.fileno(), 0o600)
            proc = subprocess.Popen([*command, "--background", f"--ready-fd={writer}"],
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                    pass_fds=(writer,), start_new_session=True,
                                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
    except BaseException:
        os.close(reader)
        raise
    finally:
        os.close(writer)
    try:
        deadline, data = time.monotonic() + 30, b""
        while b"\n" not in data:
            if not select.select([reader], [], [], max(0, deadline - time.monotonic()))[0]:
                raise OSError(f"Shelfrip did not finish starting. See {log}.")
            chunk = os.read(reader, 4096)
            if not chunk:
                raise OSError(f"Shelfrip could not start. See {log}.")
            data += chunk
        result = json.loads(data.split(b"\n", 1)[0])
        if result.get("existing"):
            proc.wait(timeout=3)
        return {**result, "pid": proc.pid if not result.get("existing") else None}
    except BaseException:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    finally:
        os.close(reader)


def ready(port: int, existing: bool = False) -> None:
    for arg in list(sys.argv[1:]):
        if arg.startswith("--ready-fd="):
            fd = int(arg.split("=", 1)[1])
            try:
                os.write(fd, (json.dumps({"port": port, "existing": existing}) + "\n").encode())
            finally:
                os.close(fd)
                sys.argv.remove(arg)  # don't retain a stale descriptor across exec/restart
