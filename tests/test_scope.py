import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import esde
import esde_core
import launch_workbench


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.roms = self.root / "roms"
        (self.roms / "nds").mkdir(parents=True)
        (self.roms / "nds/game.nds").write_bytes(b"fixture ROM")
        self.esde = self.root / "es-de"
        self.xml = self.esde / "gamelists/nds/gamelist.xml"
        self.xml.parent.mkdir(parents=True)
        self.write_xml()
        for kind in esde_core.CORE_MEDIA:
            media = self.esde / "downloaded_media/nds" / kind / "game.png"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"nonzero fixture; content QA not implied")
        self.run = self.root / "run"

    def tearDown(self):
        self.temp.cleanup()

    def write_xml(self, playtime=8, metadata=True):
        fields = "<name>测试游戏</name><desc>测试资料。</desc><developer>未知</developer><publisher/><genre>动作</genre><players/><releasedate/>" if metadata else "<name>English</name>"
        self.xml.write_text(f'<gameList><game><path>./game.nds</path>{fields}<playtime>{playtime}</playtime><private unit="x"><nested>keep</nested></private></game></gameList>', encoding="utf-8")

    def command(self, name, *extra):
        return esde.main([name, "--rom-root", str(self.roms), "--esde-root", str(self.esde), "--out", str(self.run), *map(str, extra)])

    def test_unknown_facts_are_reported_without_guessing_or_blocking(self):
        self.assertEqual(self.command("verify-local"), 0)
        rows = json.loads((self.run / "audit_games.json").read_text(encoding="utf-8"))
        self.assertEqual(set(rows[0]["unknown_fields"]), {"developer", "publisher", "players", "releasedate"})
        self.assertEqual(rows[0]["publisher"], "")
        self.assertEqual(rows[0]["issues"], [])

    def test_narrow_path_scope_does_not_claim_or_require_media_qa(self):
        self.write_xml(metadata=False)
        self.assertEqual(self.command("verify-local"), 1)
        self.assertEqual(self.command("verify-local", "--checks", "structure"), 0)
        result = json.loads((self.run / "verification_result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["checks"], ["structure"])
        self.assertEqual(result["media_check"], "not_requested")

    def test_same_run_preserved_baseline_detects_changed_history(self):
        self.assertEqual(self.command("audit"), 0)
        baseline = self.run / "preserved_fields.json"
        original_sha = hashlib.sha256(baseline.read_bytes()).hexdigest()
        self.write_xml(playtime=99)
        self.assertEqual(self.command("verify-local", "--preserved", baseline), 1)
        self.assertEqual(hashlib.sha256(baseline.read_bytes()).hexdigest(), original_sha)
        result = json.loads((self.run / "verification_result.json").read_text(encoding="utf-8"))
        self.assertTrue(result["preserved_field_differences"])
        self.assertTrue((self.run / "verified_preserved_fields.json").is_file())

    def test_failed_repeat_invalidates_earlier_pass_receipt(self):
        self.assertEqual(self.command("verify-local"), 0)
        self.xml.write_text("<broken", encoding="utf-8")
        self.assertEqual(self.command("verify-local"), 2)
        result = json.loads((self.run / "verification_result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "error")

    def test_requested_non_chinese_language_is_respected(self):
        fields = {"name": "English Game", "desc": "A test game.", "developer": "Company", "publisher": "Publisher", "genre": "Action", "players": "1", "releasedate": "20000101T000000"}
        self.assertTrue(esde_core.metadata_issues(fields, "zh"))
        self.assertEqual(esde_core.metadata_issues(fields, "any"), [])

    def test_synthetic_vpn_ip_not_advertised_as_same_wifi(self):
        answers = [(2, 1, 6, "", (ip, 0)) for ip in ("192.168.1.20", "198.18.0.1", "127.0.0.1", "169.254.2.3", "224.0.0.1")]
        with mock.patch.object(launch_workbench.socket, "getaddrinfo", return_value=answers):
            self.assertEqual(launch_workbench.ipv4_addresses(), ["192.168.1.20"])


if __name__ == "__main__":
    unittest.main()
