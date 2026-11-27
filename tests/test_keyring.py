"""Password storage contracts; never contact the user's desktop keyring."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discs
TMP = discs.setup_env({})
import app
import linux_keyring as keyring
import paths
import refsubs

OLD = keyring.PREFIX + "a" * 32
NEW = keyring.PREFIX + "b" * 32


class Backend(unittest.TestCase):
    def test_password_is_stdin_only_and_whitespace_roundtrips(self):
        password = " space ü\n"
        with patch.object(keyring.shutil, "which", return_value="/usr/bin/secret-tool"), \
                patch.object(keyring.subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, b"", b""),
                    subprocess.CompletedProcess([], 0, password.encode(), b"")]) as run:
            reference = keyring.store(password)
        self.assertTrue(keyring.is_reference(reference))
        self.assertEqual(run.call_args_list[0].kwargs["input"], password.encode())
        for call in run.call_args_list:
            self.assertNotIn(password, str(call.args))
            self.assertNotIn("shell", call.kwargs)

    def test_missing_locked_and_timed_out_keyring_never_falls_back(self):
        with patch.object(keyring.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Install secret-tool"):
                keyring.store("secret")
        for failure in (subprocess.CompletedProcess([], 1, b"secret", b"secret"),
                        subprocess.TimeoutExpired("secret", 30), OSError("secret")):
            with patch.object(keyring.shutil, "which", return_value="secret-tool"), \
                    patch.object(keyring.subprocess, "run", **(
                        {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure})):
                with self.assertRaises(ValueError) as caught:
                    keyring.lookup(OLD)
                self.assertNotIn("secret", str(caught.exception))

    def test_readback_failure_removes_only_new_item(self):
        with patch.object(keyring, "_run", side_effect=["", "wrong", ""]) as run:
            with self.assertRaisesRegex(ValueError, "did not return"):
                keyring.store("secret")
        reference = run.call_args_list[0].args[1]
        self.assertEqual(run.call_args_list[-1].args, ("clear", reference))

    def test_invalid_references_never_invoke_helper(self):
        with patch.object(keyring.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "Invalid password reference"):
                keyring.lookup("secret-service:--help")
            keyring.cleanup("invalid-reference")
        run.assert_not_called()

    def test_failed_cleanup_is_reported_without_undoing_save(self):
        with patch.object(keyring, "_run", side_effect=keyring.KeyringError("locked")):
            keyring.cleanup(OLD)
        self.assertIn("could not be removed", keyring.status()["warning"])
        with patch.object(keyring, "_run", return_value=""):
            keyring.cleanup(OLD)
        self.assertEqual(keyring.WARNING, "")

    def test_polling_does_not_unlock_keyring_but_actual_use_reports_failure(self):
        cfg = {"opensubtitles_username": "user", "opensubtitles_password": OLD}
        with patch.object(keyring, "lookup", side_effect=keyring.KeyringError("Unlock the keyring")) as lookup:
            self.assertEqual(refsubs.OpenSubtitles(cfg, resolve_password=False).ready(), "")
            lookup.assert_not_called()
            self.assertEqual(refsubs.OpenSubtitles(cfg).ready(), "Unlock the keyring")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux settings integration")
class Settings(unittest.TestCase):
    def setUp(self):
        paths.write_config({"opensubtitles_password": OLD})

    def saved(self):
        return json.loads(paths.CONFIG_PATH.read_text())["opensubtitles_password"]

    def test_save_commits_reference_before_deleting_previous_item(self):
        def cleanup(reference):
            self.assertEqual(reference, OLD)
            self.assertEqual(self.saved(), NEW)
        with patch.object(keyring, "store", return_value=NEW) as store, \
                patch.object(keyring, "cleanup", side_effect=cleanup) as clear:
            app._save_settings({"opensubtitles_password": " secret ü "})
        store.assert_called_once_with(" secret ü ")
        clear.assert_called_once_with(OLD)
        self.assertNotIn("secret ü", paths.CONFIG_PATH.read_text())

    def test_failed_config_commit_rolls_back_new_item_keeps_old(self):
        with patch.object(keyring, "store", return_value=NEW), \
                patch.object(paths, "write_config", side_effect=OSError("disk full")), \
                patch.object(keyring, "cleanup") as clear:
            with self.assertRaisesRegex(ValueError, "Couldn't save"):
                app._save_settings({"opensubtitles_password": "secret"})
        self.assertEqual(self.saved(), OLD)
        clear.assert_called_once_with(NEW)

    def test_unavailable_keyring_keeps_saved_settings(self):
        with patch.object(keyring, "store", side_effect=keyring.KeyringError("locked")), \
                patch.object(keyring, "cleanup") as clear:
            with self.assertRaisesRegex(ValueError, "locked"):
                app._save_settings({"opensubtitles_password": "secret"})
        self.assertEqual(self.saved(), OLD)
        clear.assert_not_called()

    def test_settings_polling_never_reads_the_password(self):
        paths.write_config({"opensubtitles_password": OLD, "opensubtitles_username": "user",
                            "subtitle_check": True, "reference_subtitles": True})
        with patch.object(keyring, "lookup", side_effect=AssertionError("must not unlock")) as lookup:
            view = app.settings_view()
        lookup.assert_not_called()
        row = next(r for r in view["settings"] if r["key"] == "opensubtitles_password")
        self.assertIs(row["value"], True)
        self.assertNotIn(OLD, json.dumps(view))

    def test_empty_keeps_password_and_null_removes_it(self):
        with patch.object(keyring, "store") as store, patch.object(keyring, "cleanup") as clear:
            app._save_settings({"opensubtitles_password": ""})
            self.assertEqual(self.saved(), OLD)
            clear.assert_not_called()
            app._save_settings({"opensubtitles_password": None})
            self.assertEqual(self.saved(), "")
        store.assert_not_called()
        clear.assert_called_once_with(OLD)

    def test_linux_passwords_require_keyring_references(self):
        with patch.object(keyring.subprocess, "run") as run:
            self.assertEqual(refsubs.reveal(""), "")
            for stored in ("plaintext-password", "dpapi:windows-password"):
                with self.assertRaisesRegex(ValueError, "Invalid password reference"):
                    refsubs.reveal(stored)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
