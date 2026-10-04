"""Regression coverage for real-file identity, protected history, and Android bytes."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import esde
import esde_core as core
import identity
from test_identity_prepare import verified_catalog


class XmlRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.rom = self.base / "roms" / "nds"
        self.rom.mkdir(parents=True)
        self.file = "系列/中文游戏 (改版).nds"
        (self.rom / self.file).parent.mkdir()
        (self.rom / self.file).write_bytes(b"rom")

    def document(self, games):
        return core.parse_document('<?xml version="1.0"?><alternativeEmulator><label>保留</label></alternativeEmulator><gameList>' + games + '</gameList><additional><value>保留</value></additional>')

    def test_absolute_multitop_nonascii_and_private_history(self):
        absolute = (self.rom / self.file).as_posix()
        doc = self.document('<game a="preserve"><path>' + absolute + '</path><name>中文游戏</name><desc>中文简介</desc><playcount>99</playcount><playtime>1000</playtime><customField><value>秘密配置保留</value></customField></game><game><path>./' + self.file + '</path><playcount>8</playcount><playtime>111</playtime><lastplayed>20261003T100000</lastplayed><favorite>true</favorite></game>')
        normalized, report = core.normalize_document(doc, self.rom, [self.file])
        games = normalized.find("gameList").findall("game")
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].findtext("path"), "./" + self.file)
        self.assertIsNone(games[0].findtext("name"), "A duplicate's metadata must not silently fill the authoritative history node")
        self.assertEqual(games[0].findtext("playcount"), "8")
        self.assertEqual(games[0].findtext("playtime"), "111")
        self.assertEqual(games[0].findtext("customField/value"), "秘密配置保留")
        self.assertEqual(games[0].attrib["a"], "preserve")
        self.assertTrue(report["conflicts"])
        self.assertEqual(len(core.parse_document(core.serialize_document(normalized))), 3)
        self.assertEqual(core.preserved_snapshot(doc, self.rom, [self.file]), core.preserved_snapshot(normalized, self.rom, [self.file]))

    def test_latest_relative_history_wins_never_summed(self):
        doc = self.document('<game><path>./' + self.file + '</path><playcount>7</playcount><lastplayed>20250901T000000</lastplayed></game><game><path>./' + self.file + '</path><playcount>9</playcount><lastplayed>20261003T000000</lastplayed></game><game><path>./' + self.file + '</path><playcount>2</playcount></game>')
        output, report = core.normalize_document(doc, self.rom, [self.file])
        self.assertEqual(output.find("gameList/game/playcount").text, "9")
        self.assertEqual(report["merged_duplicate_nodes"], 2)

    def test_alias_cannot_replace_second_actual_file(self):
        other = "系列/中文游戏 (Disc 2).nds"
        (self.rom / other).write_bytes(b"rom2")
        doc = self.document('<game><path>./' + self.file + '</path><name>第一碟</name></game><game><path>./' + self.file + '</path><name>误共享路径的第二碟</name></game>')
        output, report = core.normalize_document(doc, self.rom, [self.file, other])
        paths = [node.findtext("path") for node in output.find("gameList").findall("game")]
        self.assertEqual(set(paths), {"./" + self.file, "./" + other})
        self.assertEqual(len(paths), 2)
        self.assertEqual(report["created"], 1)
        second = next(node for node in output.find("gameList").findall("game") if node.findtext("path") == "./" + other)
        self.assertIsNone(second.findtext("name"))

    def test_ps3_root_case_and_no_other_root_guessing(self):
        actual = ["中文游戏.PS3"]
        self.assertEqual(core.canonical_reference("/mnt/Roms/PS3/中文游戏.PS3", "/mnt/Roms/ps3", actual), actual[0])
        self.assertIsNone(core.canonical_reference("/other_device/Roms/ps3/中文游戏.PS3", "/mnt/Roms/ps3", actual))
        self.assertIsNone(core.canonical_reference("../../escape.nds", self.rom, [self.file]))

    def test_inventory_ignores_media_assets_and_symlink_dirs(self):
        for folder in ("media", "assets", "videos"):
            (self.rom / folder).mkdir()
            (self.rom / folder / "false_game.nds").write_bytes(b"not a game")
        self.assertEqual(core.actual_files(self.rom, {".nds"}), [self.file])

    def test_patch_only_factual_fields_and_preserves_history(self):
        doc = self.document('<game><path>./' + self.file + '</path><playcount>7</playcount><lastplayed>20261003T000000</lastplayed><userField>private</userField></game>')
        metadata = {"name": "补充中文", "desc": "简介"}
        key = self.base / "isolated-test-key.json"
        with patch.object(identity, "_key", return_value=b"isolated-fixture-key-material-32!"):
            catalog = verified_catalog(self.rom / self.file, "nds", self.file, metadata, key)
            output, _ = core.normalize_document(doc, self.rom, [self.file], [{"file": self.file, "metadata": metadata}],
                                                 system="nds", identity_catalog=catalog, identity_key_path=key)
        game = output.find("gameList/game")
        self.assertEqual(game.findtext("playcount"), "7")
        self.assertEqual(game.findtext("userField"), "private")
        with self.assertRaises(ValueError):
            core.normalize_document(doc, self.rom, [self.file], [{"file": self.file, "metadata": {"playcount": "100"}}])
        with self.assertRaises(ValueError):
            core.normalize_document(doc, self.rom, [self.file], [{"file": "other.nds", "metadata": {"name": "误配"}}])

    def test_repeated_private_elements_are_preserved(self):
        doc = self.document('<game><path>./' + self.file + '</path><custom id="1">one</custom><custom id="2">two</custom></game>')
        output, _ = core.normalize_document(doc, self.rom, [self.file])
        values = output.find("gameList/game").findall("custom")
        self.assertEqual([node.text for node in values], ["one", "two"])
        self.assertEqual(core.preserved_snapshot(doc, self.rom, [self.file]), core.preserved_snapshot(output, self.rom, [self.file]))


class AuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.rom = self.base / "Roms" / "nds"
        self.esde = self.base / "ES-DE"
        self.rom.mkdir(parents=True)
        self.esde.mkdir()
        (self.rom / "中文.nds").write_bytes(b"rom")
        self.list = self.esde / "gamelists" / "nds" / "gamelist.xml"
        self.list.parent.mkdir(parents=True)
        self.list.write_text('<gameList><game><path>./中文.nds</path><name>中文名</name><desc>中文简介</desc><developer>厂商</developer><publisher>发行商</publisher><genre>动作</genre><players>1</players><releasedate>20010101T000000</releasedate><playtime>88</playtime></game></gameList>', encoding="utf-8")

    def run_command(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            return esde.main(args)

    def args(self, command, out):
        return [command, "--rom-root", str(self.rom.parent), "--esde-root", str(self.esde), "--out", str(out)]

    def media(self, zero=None):
        for kind in core.CORE_MEDIA:
            file = self.esde / "downloaded_media" / "nds" / kind / ("中文.mp4" if kind == "videos" else "中文.png")
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"" if kind == zero else b"nonzero")

    def test_missing_and_zero_byte_media_are_not_complete(self):
        out = self.base / "audit"
        self.assertEqual(self.run_command(self.args("audit", out)), 0)
        games = core.load_json(out / "audit_games.json")
        self.assertEqual(games[0]["missing_media"], list(core.CORE_MEDIA))
        self.media(zero="videos")
        self.assertEqual(self.run_command(self.args("verify-local", self.base / "verify")), 1)
        rows = core.load_json(self.base / "verify" / "audit_games.json")
        self.assertEqual(rows[0]["missing_media"], ["videos"])

    def test_unique_chinese_metadata_preserved_verify(self):
        self.media()
        before = self.base / "before"
        self.assertEqual(self.run_command(self.args("audit", before)), 0)
        verify = self.base / "verify"
        self.assertEqual(self.run_command(self.args("verify-local", verify) + ["--preserved", str(before / "preserved_fields.json")]), 0)
        self.list.write_text(self.list.read_text(encoding="utf-8").replace("<playtime>88</playtime>", "<playtime>89</playtime>"), encoding="utf-8")
        self.assertEqual(self.run_command(self.args("verify-local", verify) + ["--preserved", str(before / "preserved_fields.json")]), 1)

    def test_audit_updates_real_workbench_rows(self):
        import workbench_store
        out = self.base / "audit"
        self.run_command(self.args("audit", out))
        state = workbench_store.get_state(out)
        self.assertEqual(state["summary"]["total"], 1)
        jobs = workbench_store.get_jobs(out)["items"]
        self.assertEqual(jobs[0]["file"], "中文.nds")
        self.assertEqual(jobs[0]["missing"], list(core.CORE_MEDIA))
        phase = next(p for p in state["phases"] if p["phase"] == "audit")
        self.assertEqual((phase["completed"], phase["total"], phase["status"]), (1, 1, "done"))

    def test_audit_complete_snapshot_without_rom_download(self):
        snapshot = self.base / "snapshot"
        (snapshot / "es-de" / "gamelists" / "nds").mkdir(parents=True)
        target = snapshot / "es-de" / "gamelists" / "nds" / "gamelist.xml"
        data = self.list.read_bytes().replace(b"./", b"/storage/card/Roms/nds/")
        target.write_bytes(data)
        core.write_json(snapshot / "manifest.json", {"complete": True, "remote_rom_root": "/storage/card/Roms", "remote_media_root": "/sdcard/ES-DE/downloaded_media", "files": [{"local": "es-de/gamelists/nds/gamelist.xml", "sha256": core.sha256(data)}]})
        core.write_json(snapshot / "rom_inventory.json", [{"path": "/storage/card/Roms/nds/中文.nds", "size": 1024}, {"path": "/storage/card/Roms/nds/assets/false.nds", "size": 50}])
        core.write_json(snapshot / "media_inventory.json", [{"path": "/sdcard/ES-DE/downloaded_media/nds/" + kind + "/中文.png", "size": 100} for kind in core.CORE_MEDIA])
        out = self.base / "snapshot-audit"
        self.assertEqual(self.run_command(["audit", "--snapshot", str(snapshot), "--out", str(out)]), 0)
        rows = core.load_json(out / "audit_games.json")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["missing_media"], [])
        self.assertIn("noncanonical_reference", rows[0]["issues"])
        normalized = self.base / "normalized.xml"
        self.assertEqual(self.run_command(["normalize", "--gamelist", str(target), "--snapshot", str(snapshot), "--system", "nds", "--out", str(normalized)]), 0)
        self.assertEqual(core.parse_document(normalized.read_bytes()).find("gameList/game/path").text, "./中文.nds")

    def test_unknown_placeholders_are_explicit_not_invented(self):
        self.media()
        self.list.write_text(self.list.read_text(encoding="utf-8").replace("<developer>厂商</developer>", "<developer>未知</developer>"), encoding="utf-8")
        out = self.base / "audit"
        self.run_command(self.args("audit", out))
        self.assertEqual(core.load_json(out / "audit_games.json")[0]["unknown_fields"], ["developer"])

    def test_malformed_xml_reports_error_without_traceback(self):
        self.list.write_text("<gameList><game></gameList>", encoding="utf-8")
        self.assertEqual(self.run_command(self.args("audit", self.base / "audit")), 2)


class AdbSnapshotTests(unittest.TestCase):
    def test_inventory_binary_nul_preserves_nonascii_space_newline(self):
        adb = esde.Adb("unused", "selected")
        data = "42\n/storage/Roms/nds/中文 游戏.nds\0".encode("utf-8") + "12\n/storage/Roms/nds/换\n行.nds\0".encode("utf-8")
        with patch.object(adb, "shell_bytes", return_value=data):
            rows = adb.inventory("/storage/Roms")
        self.assertEqual(rows[0], {"path": "/storage/Roms/nds/中文 游戏.nds", "size": 42})
        self.assertEqual(rows[1]["path"], "/storage/Roms/nds/换\n行.nds")

    def test_authorization_explicit_device_required(self):
        adb = esde.Adb("unused", "chosen")
        with patch.object(adb, "devices", return_value=[{"serial": "other", "state": "device"}, {"serial": "chosen", "state": "unauthorized"}]):
            with self.assertRaisesRegex(esde.AdbError, "allow USB"):
                adb.require_device()
        with self.assertRaisesRegex(esde.AdbError, "explicit"):
            esde.Adb("unused").require_device()

    def test_snapshot_resumes_after_disconnect_without_device_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "snapshot"
            data = '<gameList><game><path>./中文.nds</path></game></gameList>'.encode("utf-8")
            class FakeAdb:
                failed = True
                def __init__(self, executable, serial):
                    self.serial = serial
                def require_device(self):
                    return {"serial": self.serial, "state": "device"}
                def inventory(self, root, required=True):
                    return [{"path": root + "/nds/gamelist.xml", "size": len(data)}] if root.endswith("gamelists") else [{"path": root + "/nds/中文.nds", "size": 100}] if root.endswith("Roms") else []
                def read(self, remote):
                    if FakeAdb.failed:
                        FakeAdb.failed = False
                        raise esde.AdbError("Selected device disconnected")
                    return data
            arguments = ["snapshot-android", "--serial", "selected", "--esde-root", "/sdcard/ES-DE", "--rom-root", "/storage/Roms", "--out", str(out)]
            with patch.object(esde, "resolve_selected_adb", return_value="/fixture/adb"), patch.object(esde, "Adb", FakeAdb), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(esde.main(arguments), 2)
                self.assertFalse(core.load_json(out / "manifest.json")["complete"])
                self.assertEqual(esde.main(arguments), 0)
            manifest = core.load_json(out / "manifest.json")
            self.assertTrue(manifest["complete"])
            self.assertEqual((out / "es-de" / "gamelists" / "nds" / "gamelist.xml").read_bytes(), data)
            self.assertFalse((out / "Roms").exists())
            self.assertFalse((out / "settings.xml").exists())


if __name__ == "__main__":
    unittest.main()
