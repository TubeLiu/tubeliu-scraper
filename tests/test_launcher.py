"""Exercise real launcher health verification; no device or game files are used."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from http.client import BadStatusLine
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import launch_workbench as launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runs = Path(self.temp.name)
        self.children = []
        self.real_popen = subprocess.Popen

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        self.temp.cleanup()

    def capture_child(self, command, **options):
        child = self.real_popen(command, **options)
        if isinstance(command, list) and any(str(item).endswith("workbench.py") for item in command):
            self.children.append(child)
        return child

    def start(self, **options):
        with mock.patch.object(launcher.subprocess, "Popen", side_effect=self.capture_child):
            return launcher.launch(self.runs, **options)

    def test_real_local_start_and_verified_reuse(self):
        with mock.patch.object(launcher.webbrowser, "open") as browser:
            first = self.start(port=0)
            launcher.write_connection(self.runs / launcher.RUNTIME_FOLDER, {**first, "scraper_password": "accidental-extra-field"})
            second = self.start(port=0)
            browser.assert_not_called()
        self.assertEqual(len(self.children), 1)
        self.assertEqual(first["pid"], second["pid"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["host"], "127.0.0.1")
        self.assertEqual(first["access_token"], "")
        self.assertEqual(first["lan_urls"], [])
        self.assertNotIn("scraper_password", second)
        self.assertNotIn("accidental-extra-field", (self.runs / launcher.RUNTIME_FOLDER / "connection.json").read_text(encoding="utf-8"))
        self.assertTrue(launcher.verified_existing(first, self.runs))
        with urlopen(first["local_url"] + "api/runs", timeout=2) as response:
            self.assertEqual(json.load(response), {"items": [], "total": 0})

    def test_real_lan_requires_token_and_preserves_only_workbench_data(self):
        with mock.patch.object(launcher, "ipv4_addresses", return_value=["192.168.2.10"]):
            with mock.patch.dict(os.environ, {"SCRAPER_ACCOUNT_PASSWORD": "do-not-persist-this"}):
                result = self.start(port=0, lan=True)
        self.assertGreaterEqual(len(result["access_token"]), 32)
        self.assertTrue(result["lan_urls"][0].startswith("http://192.168.2.10:"))
        with self.assertRaises(HTTPError) as raised:
            urlopen("http://127.0.0.1:" + str(result["port"]) + "/api/health", timeout=2)
        self.assertEqual(raised.exception.code, 401)
        self.assertTrue(launcher.verified_existing(result, self.runs))
        saved = (self.runs / launcher.RUNTIME_FOLDER / "connection.json").read_text(encoding="utf-8")
        self.assertNotIn("do-not-persist-this", saved)
        self.assertNotIn("SCRAPER_ACCOUNT_PASSWORD", saved)
        self.assertEqual(json.loads(saved)["pid"], result["pid"])

    def test_occupied_unknown_port_is_not_stopped(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unknown:
            unknown.bind(("127.0.0.1", 0))
            unknown.listen()
            occupied = unknown.getsockname()[1]
            if occupied == 65535:
                self.skipTest("No following port available")
            result = self.start(port=occupied)
            self.assertNotEqual(result["port"], occupied)
            with socket.create_connection(("127.0.0.1", occupied), timeout=1):
                pass

    def test_existing_configuration_change_does_not_kill_or_duplicate(self):
        existing = self.start(port=0)
        with self.assertRaisesRegex(ValueError, "不同端口或访问范围"):
            self.start(port=0, lan=True)
        self.assertEqual(len(self.children), 1)
        self.assertTrue(launcher.verified_existing(existing, self.runs))

    def test_identity_verification_rejects_wrong_pid_root_or_instance(self):
        value = {"service": launcher.SERVICE, "version": 1, "pid": 100, "root": str(self.runs), "host": "127.0.0.1", "port": 20000, "instance_id": "our-instance"}
        self.assertTrue(launcher.health_matches(value, self.runs, 20000, "our-instance", 100, False))
        for key, wrong in [("pid", 101), ("root", str(self.runs / "other")), ("instance_id", "different"), ("host", "0.0.0.0"), ("service", "unknown-service")]:
            changed = {**value, key: wrong}
            self.assertFalse(launcher.health_matches(changed, self.runs, 20000, "our-instance", 100, False))

    def test_unknown_connection_record_does_not_direct_requests_elsewhere(self):
        runtime = self.runs / launcher.RUNTIME_FOLDER
        runtime.mkdir()
        launcher.write_connection(runtime, {"service": "unknown-service", "local_url": "https://example.invalid/", "pid": 1})
        with mock.patch.object(launcher.webbrowser, "open") as browser:
            result = self.start(port=0)
            browser.assert_not_called()
        self.assertFalse(result["reused"])
        self.assertTrue(result["local_url"].startswith("http://127.0.0.1:"))

    def test_explicit_open_happens_only_after_verified_health(self):
        with mock.patch.object(launcher.webbrowser, "open") as browser:
            result = self.start(port=0, open_browser=True)
            browser.assert_called_once_with(result["local_url"], new=2)
        self.assertTrue(launcher.verified_existing(result, self.runs))

    def test_address_enumeration_excludes_non_lan_addresses(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 0)) for address in ["127.0.0.1", "0.0.0.0", "169.254.1.2", "224.0.0.1", "192.168.3.8", "10.0.1.2", "192.168.3.8"]]
        with mock.patch.object(launcher.socket, "getaddrinfo", return_value=answers):
            self.assertEqual(launcher.ipv4_addresses(), ["10.0.1.2", "192.168.3.8"])

    def test_malformed_unknown_http_response_is_not_a_verified_server(self):
        opener = mock.Mock()
        opener.open.side_effect = BadStatusLine("unknown service response")
        with mock.patch.object(launcher, "build_opener", return_value=opener):
            self.assertIsNone(launcher.health(20000, "workbench-only-token"))


if __name__ == "__main__":
    unittest.main()
