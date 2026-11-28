"""Exercise real subprocess trees: cancellation, shutdown and abrupt parent death."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import processes


def alive(pid):
    if sys.platform == "darwin":
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
        return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@unittest.skipUnless(sys.platform.startswith("linux") or sys.platform == "darwin", "POSIX process supervision")
class Ownership(unittest.TestCase):
    def test_output_input_and_exit_code(self):
        proc = processes.spawn([sys.executable, "-c", "import sys; print(input().upper()); sys.exit(7)"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate("hello\n", timeout=5)
        self.assertEqual((proc.returncode, out, err), (7, "HELLO\n", ""))

    def test_missing_command_reports_failure(self):
        proc = processes.spawn(["/no/such/shelfrip-test-program"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 127)
        self.assertIn("Could not start", err)

    def tree(self, action):
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready.json"
            # Both processes ignore TERM, so successful cleanup requires escalation.
            grandchild = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"
            child = ("import os,sys,subprocess,signal,time,json; from pathlib import Path; "
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                     f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
                     f"Path({str(ready)!r}).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(120)")
            parent_code = ("import processes,sys,time; "
                           f"p=processes.spawn([sys.executable,'-c',{child!r}]); "
                           "print(p.pid,flush=True); "
                           "input(); " + action)
            parent = subprocess.Popen([sys.executable, "-c", parent_code], cwd=ROOT,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pids = []
            supervisor = int(parent.stdout.readline())
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "test child never started")
                pids = json.loads(ready.read_text())
                if action == "time.sleep(120)":
                    parent.kill()  # no Python cleanup, signal handler or finally block
                else:
                    parent.stdin.write("go\n")
                    parent.stdin.flush()
                parent.wait(timeout=8)
                deadline = time.monotonic() + 5
                while any(alive(pid) for pid in [supervisor, *pids]) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(any(alive(pid) for pid in [supervisor, *pids]), "rip tree survived its owner")
                if action != "time.sleep(120)":
                    self.assertEqual(parent.returncode, 0, parent.stderr.read())
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=5)
                for pid in [supervisor, *pids]:
                    if alive(pid):
                        os.kill(pid, signal.SIGKILL)
                for stream in (parent.stdin, parent.stdout, parent.stderr):
                    stream.close()

    def test_parent_sigkill_stops_entire_tree(self):
        self.tree("time.sleep(120)")

    def test_cancel_stops_entire_tree(self):
        self.tree("p.terminate(); p.wait(timeout=5)")

    def test_shutdown_stops_tree_and_rejects_new_work(self):
        self.tree("processes.shutdown(); exec(\"try:\\n processes.spawn([sys.executable, '-c', 'pass'])\\nexcept OSError:\\n pass\\nelse:\\n raise AssertionError('spawn allowed during shutdown')\")")

    def test_application_signals_stop_owned_ripper(self):
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "config.json").write_text(json.dumps({"check_updates": False, "shared_tmdb": False,
                    "share_discs": False, "use_community": False, "auto_rip": False, "output_root": ""}))
                ready = root / "child.pid"
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                child = ("import os,time,signal; from pathlib import Path; "
                         "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                         f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(120)")
                code = ("import app,processes,sys; "
                        f"app.rip_all.PROCS['test-drive']=processes.spawn([sys.executable,'-c',{child!r}]); app.main()")
                parent = subprocess.Popen([sys.executable, "-c", code, "--background", f"--port={port}"],
                                          cwd=ROOT, env={**os.environ, "SHELFRIP_DATA": tmp},
                                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                pid = None
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1):
                                if ready.exists():
                                    break
                        except OSError:
                            pass
                        time.sleep(0.02)
                    else:
                        self.fail("application never became ready")
                    pid = int(ready.read_text())
                    parent.send_signal(sig)
                    parent.wait(timeout=8)
                    self.assertEqual(parent.returncode, 0, parent.stderr.read())
                    self.assertFalse(alive(pid), "application signal left its ripper alive")
                finally:
                    if parent.poll() is None:
                        parent.kill()
                        parent.wait(timeout=5)
                    if pid and alive(pid):
                        os.kill(pid, signal.SIGKILL)
                    parent.stderr.close()


if __name__ == "__main__":
    unittest.main()
