"""Private-store checks use invented values and disposable local directories."""
import base64
import ctypes
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import screenscraper_credentials as credentials

DEVELOPER = {"SCREENSCRAPER_DEVID": "invented-developer-id", "SCREENSCRAPER_DEVPASSWORD": "invented-developer-password"}
USER = {"SCREENSCRAPER_SSID": "invented-account", "SCREENSCRAPER_SSPASSWORD": "invented-user-password"}


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "private" / "screenscraper.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_roundtrip_update_forget_and_safe_summaries(self):
        values = {**DEVELOPER, **USER}
        report = credentials.save_credentials(values, self.path)
        self.assertEqual(credentials.load_credentials(self.path), values)
        self.assertEqual((report["configured"], report["user_configured"], report["storage"]), (True, True, "saved"))
        for secret in values.values():
            self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn("path", report)
        replacement = {**DEVELOPER, "SCREENSCRAPER_DEVPASSWORD": "different-invented-password"}
        credentials.save_credentials(replacement, self.path)
        self.assertEqual(credentials.load_credentials(self.path), {**replacement, **USER})
        self.assertTrue(credentials.credential_status({}, self.path)["user_configured"])
        self.assertTrue(credentials.forget_credentials(self.path)["forgotten"])
        self.assertFalse(credentials.forget_credentials(self.path)["forgotten"])
        self.assertEqual(credentials.load_credentials(self.path), {})

    def test_account_only_store_is_persistent_but_not_api_ready_and_updates_preserve_pairs(self):
        saved = credentials.save_credentials(USER, self.path)
        self.assertTrue(saved["configured"])
        self.assertTrue(saved["user_configured"])
        self.assertFalse(saved["developer_configured"])
        self.assertFalse(saved["ready"])
        self.assertEqual(credentials.load_credentials(self.path), USER)
        self.assertEqual(credentials.resolve_credentials({}, self.path), USER)
        self.assertEqual(credentials.credential_status({}, self.path)["missing"], list(credentials.DEVELOPER_FIELDS))
        credentials.save_credentials(DEVELOPER, self.path)
        self.assertEqual(credentials.load_credentials(self.path), {**DEVELOPER, **USER})
        updated_user = {"SCREENSCRAPER_SSID": "different-invented-account", "SCREENSCRAPER_SSPASSWORD": "different-invented-user-password"}
        updated = credentials.save_credentials(updated_user, self.path)
        self.assertTrue(updated["ready"])
        self.assertEqual(credentials.load_credentials(self.path), {**DEVELOPER, **updated_user})
        for secret in (*DEVELOPER.values(), *USER.values(), *updated_user.values()):
            self.assertNotIn(secret, json.dumps(saved) + json.dumps(updated))

    def test_cli_account_configuration_survives_new_process_without_claiming_api_ready(self):
        environment = {key: value for key, value in os.environ.items() if key not in credentials.FIELDS}
        environment[credentials.PATH_ENV] = str(self.path)
        script = SCRIPTS / "screenscraper.py"
        values = {**DEVELOPER, **USER}

        def call(*arguments, input_text=None):
            result = subprocess.run(
                [sys.executable, "-B", str(script), *arguments],
                input=input_text, text=True, capture_output=True,
                env=environment, cwd=self.temporary.name, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for value in values.values():
                self.assertNotIn(value, result.stdout + result.stderr)
            return json.loads(result.stdout)

        saved = call("configure", "--user-only", "--stdin", input_text=json.dumps(USER))
        self.assertEqual(saved["status"], "needs_developer_credentials")
        self.assertTrue(saved["user_configured"])
        self.assertFalse(saved["ready_for_api"])
        self.assertIn("账号密码已保存", saved["message"])
        fresh = call("check")
        self.assertEqual(fresh["status"], "needs_developer_credentials")
        self.assertEqual(fresh["storage"], "saved")
        self.assertFalse(fresh["ready_for_api"])
        enabled = call("configure", "--stdin", input_text=json.dumps(DEVELOPER))
        self.assertTrue(enabled["ready_for_api"])
        self.assertTrue(enabled["user_configured"])
        call("forget")
        self.assertFalse(self.path.exists())

    def test_partial_update_does_not_discard_an_unreadable_existing_store(self):
        credentials.save_credentials(DEVELOPER, self.path)
        self.path.write_text("unreadable-private-store", encoding="utf-8")
        before = self.path.read_bytes()
        for pair in (USER, DEVELOPER):
            with self.subTest(fields=list(pair)):
                with self.assertRaises(credentials.CredentialError) as error:
                    credentials.save_credentials(pair, self.path)
                self.assertEqual(error.exception.code, "invalid_store")
                self.assertEqual(self.path.read_bytes(), before)
        complete = {**DEVELOPER, **USER}
        credentials.save_credentials(complete, self.path)
        self.assertEqual(credentials.load_credentials(self.path), complete)

    def test_cli_configuration_survives_new_process_and_can_be_cleared(self):
        environment = dict(os.environ)
        for field in credentials.FIELDS:
            environment.pop(field, None)
        environment[credentials.PATH_ENV] = str(self.path)
        script = SCRIPTS / "screenscraper.py"
        values = {**DEVELOPER, **USER}

        def call(*arguments, input_text=None):
            result = subprocess.run(
                [sys.executable, "-B", str(script), *arguments],
                input=input_text, text=True, capture_output=True,
                env=environment, cwd=self.temporary.name, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for value in values.values():
                self.assertNotIn(value, result.stdout + result.stderr)
            return json.loads(result.stdout)

        saved = call("configure", "--stdin", input_text=json.dumps(values))
        self.assertTrue(saved["ready_for_api"])
        self.assertEqual(saved["storage"], "saved")
        fresh = call("check")
        self.assertTrue(fresh["ready_for_api"])
        self.assertTrue(fresh["user_configured"])
        self.assertEqual(fresh["storage"], "saved")
        self.assertFalse(fresh["credential_verified"])
        self.assertFalse(fresh["network_checked"])
        call("forget")
        cleared = call("check")
        self.assertFalse(cleared["ready_for_api"])
        self.assertTrue(cleared["can_skip"])
        self.assertFalse(self.path.exists())

    def test_windows_file_is_ciphertext_for_current_user(self):
        if os.name != "nt":
            self.skipTest("Current-user DPAPI is Windows-specific")
        values = {**DEVELOPER, **USER}
        credentials.save_credentials(values, self.path)
        text = self.path.read_text(encoding="utf-8")
        for secret in values.values():
            self.assertNotIn(secret, text)
        envelope = json.loads(text)
        self.assertEqual(envelope["protection"], "current_user_dpapi")
        self.assertNotIn("credentials", envelope)
        self.assertNotIn(b"SCREENSCRAPER_DEVPASSWORD", base64.b64decode(envelope["encrypted"]))
        credentials._private_windows(self.path)
        credentials._private_windows(self.path.parent, directory=True)
        self.assertEqual(credentials.load_credentials(self.path), values)

    def test_invalid_values_do_not_replace_the_existing_store(self):
        credentials.save_credentials(DEVELOPER, self.path)
        original = self.path.read_bytes()
        invalid = ({}, {"SCREENSCRAPER_DEVID": "only-one"},
                   {"SCREENSCRAPER_SSID": "unpaired-account"},
                   {"SCREENSCRAPER_SSPASSWORD": "unpaired-password"},
                   {**DEVELOPER, "SCREENSCRAPER_DEVPASSWORD": " \t\n"},
                   {**DEVELOPER, "SCREENSCRAPER_SSID": "unpaired-user"},
                   {**DEVELOPER, "SCREENSCRAPER_SSID": "", "SCREENSCRAPER_SSPASSWORD": ""},
                   {**DEVELOPER, "SCREENSCRAPER_SSPASSWORD": None},
                   {**DEVELOPER, "WORKBENCH_TOKEN": "not-a-scraper-secret"})
        for values in invalid:
            with self.subTest(fields=list(values)):
                with self.assertRaises(credentials.CredentialError) as error:
                    credentials.save_credentials(values, self.path)
                self.assertEqual(self.path.read_bytes(), original)
                for value in values.values():
                    if isinstance(value, str) and value.strip():
                        self.assertNotIn(value, str(error.exception))
        self.assertEqual(credentials.load_credentials(self.path), DEVELOPER)

    def test_explicit_empty_environment_never_reads_default_store(self):
        with patch.object(credentials, "load_credentials", side_effect=AssertionError("must not read user's real store")):
            self.assertEqual(credentials.resolve_credentials({}), {})
            self.assertFalse(credentials.credential_status({})["configured"])
            self.assertEqual(credentials.resolve_credentials(DEVELOPER), DEVELOPER)

    def test_pair_overrides_are_complete_and_never_mix_sources(self):
        credentials.save_credentials({**DEVELOPER, **USER}, self.path)
        overriding = {"SCREENSCRAPER_DEVID": "another-id", "SCREENSCRAPER_DEVPASSWORD": "another-password"}
        self.assertEqual(credentials.resolve_credentials(overriding, self.path), {**overriding, **USER})
        for fields, missing in (({"SCREENSCRAPER_DEVID": "single-override"}, "SCREENSCRAPER_DEVPASSWORD"),
                                ({"SCREENSCRAPER_SSPASSWORD": "single-override"}, "SCREENSCRAPER_SSID")):
            with self.subTest(fields=list(fields)):
                with self.assertRaises(credentials.CredentialError) as error:
                    credentials.resolve_credentials(fields, self.path)
                self.assertEqual(error.exception.code, "incomplete_pair")
                self.assertIn(missing, error.exception.missing)
                status = credentials.credential_status(fields, self.path)
                self.assertFalse(status["ready"])
                self.assertEqual(status["error"], "incomplete_pair")
                self.assertNotIn("single-override", json.dumps(status))

    def test_process_environment_and_custom_store_are_used_by_default(self):
        credentials.save_credentials(DEVELOPER, self.path)
        with patch.dict(os.environ, {credentials.PATH_ENV: str(self.path)}, clear=True):
            self.assertEqual(credentials.resolve_credentials(), DEVELOPER)
            self.assertEqual(credentials.credential_status()["storage"], "saved")
        with patch.dict(os.environ, {credentials.PATH_ENV: str(self.path), **DEVELOPER}, clear=True):
            self.assertEqual(credentials.credential_status()["storage"], "environment")

    def test_default_paths_are_per_user_and_not_a_skill_or_run_directory(self):
        expected_base = Path(self.temporary.name) / "config"
        if os.name == "nt":
            fields = {"LOCALAPPDATA": str(expected_base)}
        else:
            fields = {"XDG_CONFIG_HOME": str(expected_base)}
        self.assertEqual(credentials.default_path(fields), expected_base / "es-de-resource-workbench" / "credentials" / "screenscraper.json")
        self.assertEqual(credentials.default_path({credentials.PATH_ENV: str(self.path)}), self.path)

    def test_corrupt_store_and_decryption_failure_never_report_ready(self):
        credentials.save_credentials(DEVELOPER, self.path)
        self.path.write_text("invalid JSON with invented-password", encoding="utf-8")
        status = credentials.credential_status({}, self.path)
        self.assertFalse(status["ready"])
        self.assertEqual(status["error"], "invalid_store")
        self.assertNotIn("invented-password", json.dumps(status))
        credentials.save_credentials({**DEVELOPER, **USER}, self.path)
        if os.name == "nt":
            with patch.object(credentials, "_dpapi", side_effect=credentials.CredentialError("Cannot decrypt credentials for the current Windows user", code="decrypt_failed")):
                status = credentials.credential_status({}, self.path)
            self.assertFalse(status["ready"])
            self.assertEqual(status["error"], "decrypt_failed")

    def test_atomic_replace_failure_keeps_old_credentials(self):
        credentials.save_credentials(DEVELOPER, self.path)
        before = self.path.read_bytes()
        with patch.object(credentials.os, "replace", side_effect=OSError("error may contain a private path")):
            with self.assertRaises(credentials.CredentialError) as error:
                credentials.save_credentials({**DEVELOPER, **USER}, self.path)
        self.assertEqual(error.exception.code, "save_failed")
        self.assertNotIn("private path", str(error.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".screenscraper-*.tmp")), [])

    def test_complete_environment_recovers_from_corrupt_saved_store(self):
        credentials.save_credentials({**DEVELOPER, **USER}, self.path)
        self.path.write_text("corrupt saved credentials", encoding="utf-8")
        self.assertEqual(credentials.resolve_credentials(DEVELOPER, self.path), DEVELOPER)
        status = credentials.credential_status(DEVELOPER, self.path)
        self.assertTrue(status["ready"])
        self.assertEqual((status["storage"], status["protection"], status["user_configured"]), ("environment", "none", False))
        with self.assertRaises(credentials.CredentialError) as error:
            credentials.resolve_credentials({"SCREENSCRAPER_DEVID": "unpaired"}, self.path)
        self.assertEqual(error.exception.code, "incomplete_pair")
        with patch.object(credentials, "load_credentials", side_effect=AssertionError("complete override must not read saved secrets")):
            self.assertEqual(credentials.resolve_credentials({**DEVELOPER, **USER}, self.path), {**DEVELOPER, **USER})

    def test_symlink_or_junction_path_is_rejected(self):
        real = Path(self.temporary.name) / "real"
        real.mkdir()
        link = Path(self.temporary.name) / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            # Windows installations without symlink privilege still test the
            # junction/reparse attribute rejection on an existing directory.
            original = Path.lstat
            def reparse(component):
                details = original(component)
                if component == real:
                    return SimpleNamespace(st_mode=details.st_mode, st_file_attributes=0x400)
                return details
            with patch.object(Path, "lstat", reparse):
                with self.assertRaises(credentials.CredentialError) as error:
                    credentials.save_credentials(DEVELOPER, real / "credentials.json")
        else:
            with self.assertRaises(credentials.CredentialError) as error:
                credentials.save_credentials(DEVELOPER, link / "credentials.json")
        self.assertEqual(error.exception.code, "unsafe_path")
        self.assertEqual(list(real.iterdir()), [])

    def test_hardlinked_store_is_rejected(self):
        credentials.save_credentials(DEVELOPER, self.path)
        linked = self.path.parent / "copy.json"
        try:
            os.link(self.path, linked)
        except OSError:
            self.skipTest("Filesystem does not support hard links")
        for function in (credentials.load_credentials, credentials.forget_credentials):
            with self.assertRaises(credentials.CredentialError) as error:
                function(self.path)
            self.assertEqual(error.exception.code, "unsafe_path")

    def test_posix_protected_file_modes_and_foreign_owner(self):
        # Exercise POSIX permission policy on Windows too, without changing the
        # process platform or touching any real user's configuration directory.
        fake = Mock()
        fake.lstat.side_effect = [SimpleNamespace(st_uid=42, st_mode=stat.S_IFREG | 0o644),
                                 SimpleNamespace(st_uid=42, st_mode=stat.S_IFREG | 0o600)]
        with patch.object(credentials.os, "geteuid", return_value=42, create=True):
            credentials._private_posix(fake, set_permissions=True)
        fake.chmod.assert_called_once_with(0o600)
        for uid, mode in ((43, 0o600), (42, 0o644)):
            fake = Mock()
            fake.lstat.return_value = SimpleNamespace(st_uid=uid, st_mode=stat.S_IFREG | mode)
            with patch.object(credentials.os, "geteuid", return_value=42, create=True), self.assertRaises(credentials.CredentialError) as error:
                credentials._private_posix(fake)
            self.assertEqual(error.exception.code, "unsafe_permissions")
        fake = Mock()
        fake.lstat.return_value = SimpleNamespace(st_uid=42, st_mode=stat.S_IFDIR | 0o700)
        with patch.object(credentials.os, "geteuid", return_value=42, create=True):
            credentials._private_posix(fake, directory=True)

    def test_windows_dpapi_flag_never_uses_local_machine(self):
        if os.name != "nt":
            self.skipTest("Windows DPAPI contract")
        from ctypes import wintypes
        calls = []
        def fail_without_allocating(*args):
            calls.append(args[5])
            return False
        function = Mock(side_effect=fail_without_allocating)
        api = (None, Mock(), SimpleNamespace(CryptProtectData=function, CryptUnprotectData=function), wintypes)
        with patch.object(credentials, "_windows_api", return_value=api):
            for decrypt in (False, True):
                with self.assertRaises(credentials.CredentialError):
                    credentials._dpapi(b"invented-plaintext", decrypt=decrypt)
        self.assertEqual(calls, [1, 1])
        self.assertTrue(all(not value & 4 for value in calls))

    def test_posix_persistence_is_honestly_marked_protected_file(self):
        if os.name == "nt":
            self.skipTest("Native POSIX file checks run on POSIX")
        summary = credentials.save_credentials(DEVELOPER, self.path)
        self.assertEqual(summary["protection"], "protected_file")
        envelope = json.loads(self.path.read_text())
        self.assertEqual(envelope["credentials"], DEVELOPER)
        self.assertNotIn("encrypted", envelope)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.path.chmod(0o644)
        self.assertFalse(credentials.credential_status({}, self.path)["ready"])


if __name__ == "__main__":
    unittest.main()
