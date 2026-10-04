"""Identity gates use isolated signatures and synthetic ROMs, never real devices."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import esde
import esde_core as core
import identity
import screenscraper as provider


def verified_catalog(rom_path, system, file, metadata, key_path):
    fingerprint = identity.fingerprint_file(rom_path)
    candidate = {"provider": "screenscraper", "provider_game_id": "fixture-game-42",
                 "system": {"id": identity.SCREENSCRAPER_SYSTEMS[system]},
                 "name": metadata.get("name"), "description_zh": metadata.get("desc"),
                 **{field: metadata.get(field) for field in ("developer", "publisher", "genre", "players", "releasedate", "rating")},
                 "rom_evidence": {"romtaille": fingerprint["size"], "rommd5": fingerprint["md5"]},
                 "media_candidates": []}
    observed = {"provider": "screenscraper", "endpoint": "jeuInfos.php",
                "query": {"systemeid": identity.SCREENSCRAPER_SYSTEMS[system],
                          "romtaille": fingerprint["size"], "md5": fingerprint["md5"]},
                "candidates": [candidate]}
    source = identity.seal_provider_report(observed, key_path=key_path)
    receipt = identity.authorize_patch(source, system=system, file=file, rom_path=rom_path,
                                       metadata=metadata, key_path=key_path)
    return {"schema_version": 1, "entries": [{"system": system, "file": file,
                                               "rom_fingerprint": fingerprint, "receipt": receipt}]}


class IdentityPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.rom = self.base / "roms" / "nds"
        self.rom.mkdir(parents=True)
        self.file = "folder/fixture.nds"
        self.path = self.rom / self.file
        self.path.parent.mkdir()
        self.path.write_bytes(b"synthetic fixture ROM, never a commercial game")
        self.key = self.base / "isolated-test-key.json"
        def fixture_key(key_path=None, **kwargs):
            if key_path is None or Path(key_path) != self.key:
                raise AssertionError("A test must explicitly use its isolated identity key")
            return b"isolated-test-fixture-key-32-byte!"
        self.addCleanup(patch.stopall)
        patch.object(identity, "_key", side_effect=fixture_key).start()
        self.metadata = {"name": "测试游戏", "desc": "来源返回的准确中文简介"}
        self.patch = {"file": self.file, "metadata": self.metadata}
        self.catalog = verified_catalog(self.path, "nds", self.file, self.metadata, self.key)
        self.document = core.parse_document('<gameList><game><path>./' + self.file + '</path><playcount>7</playcount></game></gameList>')

    def normalize(self, *, catalog=None, patch_value=None):
        return core.normalize_document(self.document, self.rom, [self.file],
                                       [self.patch if patch_value is None else patch_value],
                                       system="nds", identity_catalog=self.catalog if catalog is None else catalog,
                                       identity_key_path=self.key)

    def test_default_core_api_rejects_unconfirmed_metadata(self):
        with self.assertRaisesRegex(ValueError, "Identity receipt required"):
            core.normalize_document(self.document, self.rom, [self.file], [self.patch])

    def test_exact_receipt_preserves_gameplay_history(self):
        output, report = self.normalize()
        self.assertEqual(output.findtext("gameList/game/name"), self.metadata["name"])
        self.assertEqual(output.findtext("gameList/game/playcount"), "7")
        self.assertEqual(report["identity_checks"][0]["provider_game_id"], "fixture-game-42")

    def test_exact_fact_cannot_leave_unapproved_nested_or_repeated_factual_values(self):
        for facts in ('<name><other>另一游戏</other></name>', '<name id="another-game">旧名称</name>', '<name>旧名称</name><name>第二名称</name>'):
            self.document = core.parse_document('<gameList><game><path>./' + self.file + '</path>' + facts + '</game></gameList>')
            with self.subTest(facts=facts), self.assertRaisesRegex(ValueError, "Ambiguous or nested metadata"):
                self.normalize()

    def test_zelda_metadata_cannot_be_substituted_for_another_rom(self):
        wrong = {"file": self.file, "metadata": {**self.metadata, "name": "另一款完全不同的游戏"}}
        with self.assertRaisesRegex(ValueError, "approved payload"):
            self.normalize(patch_value=wrong)

    def test_wrong_platform_game_id_and_catalog_key_are_rejected(self):
        for mutation in ("platform", "game_id", "key"):
            catalog = copy.deepcopy(self.catalog)
            if mutation == "platform":
                catalog["entries"][0]["system"] = "gba"
            elif mutation == "game_id":
                catalog["entries"][0]["receipt"]["payload"]["provider_game_id"] = "another-game"
            else:
                catalog["identity_key"] = str(self.key)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.normalize(catalog=catalog)

    def test_changed_actual_rom_rejects_old_receipt_even_with_new_catalog_hash(self):
        self.path.write_bytes(b"different actual ROM contents")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.normalize()
        catalog = copy.deepcopy(self.catalog)
        catalog["entries"][0]["rom_fingerprint"] = identity.fingerprint_file(self.path)
        with self.assertRaisesRegex(ValueError, "ROM bytes changed"):
            self.normalize(catalog=catalog)

    def test_path_only_normalization_does_not_promote_duplicate_metadata(self):
        duplicate = core.parse_document('<gameList><game><path>./' + self.file + '</path><playcount>7</playcount></game><game><path>'
                                        + self.path.as_posix() + '</path><name>可能误配的名称</name><desc>可能误配的简介</desc><image>wrong.png</image></game></gameList>')
        output, report = core.normalize_document(duplicate, self.rom, [self.file])
        self.assertIsNone(output.findtext("gameList/game/name"))
        self.assertIsNone(output.findtext("gameList/game/desc"))
        self.assertIsNone(output.findtext("gameList/game/image"))
        self.assertTrue(report["conflicts"])

    def write_cli_inputs(self):
        source = self.base / "source.xml"
        source.write_bytes(core.serialize_document(self.document))
        patch_file, catalog_file = self.base / "patch.json", self.base / "catalog.json"
        core.write_json(patch_file, [self.patch])
        core.write_json(catalog_file, self.catalog)
        return source, patch_file, catalog_file

    def prepare_args(self, output):
        source, patch_file, catalog_file = self.write_cli_inputs()
        return ["prepare", "--system", "nds", "--gamelist", str(source),
                "--system-rom-root", str(self.rom), "--patch", str(patch_file),
                "--identity-catalog", str(catalog_file), "--identity-key", str(self.key), "--out", str(output)]

    def run_cli(self, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return esde.main(args)

    def test_prepare_freezes_input_output_and_catalog_and_approvals(self):
        output = self.base / "prepared.xml"
        self.assertEqual(self.run_cli(self.prepare_args(output)), 0)
        proof = identity.verify_preparation(core.load_json(str(output) + ".identity.json"), key_path=self.key)
        self.assertEqual(proof["source_sha256"], core.sha256(Path(proof["source_path"]).read_bytes()))
        self.assertEqual(proof["output_sha256"], core.sha256(output.read_bytes()))
        self.assertEqual(proof["identity_catalog_sha256"], core.sha256(Path(proof["identity_catalog_path"]).read_bytes()))
        self.assertEqual(proof["entries"][0]["approved_metadata"], self.metadata)
        self.assertEqual(proof["binding"]["kind"], "local")
        self.assertEqual(self.run_cli(self.prepare_args(output)), 2, "Frozen identity proof must never be replaced")

    def test_prepare_missing_receipt_never_creates_output(self):
        output = self.base / "unapproved.xml"
        args = self.prepare_args(output)
        index = args.index("--identity-catalog")
        del args[index:index + 2]
        self.assertEqual(self.run_cli(args), 2)
        self.assertFalse(output.exists())
        self.assertFalse(Path(str(output) + ".identity.json").exists())

    def snapshot_fixture(self):
        snapshot = self.base / "snapshot"
        snapshot.mkdir()
        core.write_json(snapshot / "manifest.json", {"complete": True, "serial": "fixture-device", "remote_rom_root": "/fixture/Roms",
                                                     "remote_media_root": "/fixture/ES-DE/downloaded_media", "adb_executable": "/fixture/adb"})
        core.write_json(snapshot / "rom_inventory.json", [{"path": "/fixture/Roms/nds/" + self.file, "size": self.path.stat().st_size}])
        core.write_json(snapshot / "media_inventory.json", [])
        return snapshot

    def fake_adb(self, fingerprint=None):
        live = fingerprint or identity.fingerprint_file(self.path)
        reads = []
        class FakeAdb:
            def __init__(self, executable, serial):
                self.serial = serial
            def require_device(self):
                return {"serial": self.serial, "state": "device"}
            def fingerprint(self, remote, *, approved_root=None):
                if approved_root != "/fixture/Roms":
                    raise AssertionError("ROM reader must bind the explicit snapshot root")
                reads.append(remote)
                return live
        return FakeAdb, reads

    def test_android_prepare_measures_bytes_and_binds_device_root_and_inventory(self):
        snapshot = self.snapshot_fixture()
        output = self.base / "android-prepared.xml"
        args = self.prepare_args(output)
        index = args.index("--system-rom-root")
        args[index:index + 2] = ["--snapshot", str(snapshot)]
        args.extend(["--serial", "fixture-device", "--remote-rom-root", "/fixture/Roms"])
        fake, reads = self.fake_adb()
        with patch.object(esde, "resolve_selected_adb", return_value="/fixture/adb"), patch.object(esde, "Adb", fake):
            self.assertEqual(self.run_cli(args), 0)
        self.assertEqual(reads, ["/fixture/Roms/nds/" + self.file])
        proof = identity.verify_preparation(core.load_json(str(output) + ".identity.json"), key_path=self.key)
        self.assertEqual(proof["binding"]["serial"], "fixture-device")
        self.assertEqual(proof["binding"]["rom_inventory_sha256"], core.sha256((snapshot / "rom_inventory.json").read_bytes()))

    def test_android_caller_claim_cannot_replace_a_current_measurement(self):
        snapshot = self.snapshot_fixture()
        output = self.base / "changed-android.xml"
        args = self.prepare_args(output)
        index = args.index("--system-rom-root")
        args[index:index + 2] = ["--snapshot", str(snapshot)]
        args.extend(["--serial", "fixture-device", "--remote-rom-root", "/fixture/Roms"])
        changed = {**identity.fingerprint_file(self.path), "sha256": "0" * 64}
        fake, reads = self.fake_adb(changed)
        with patch.object(esde, "resolve_selected_adb", return_value="/fixture/adb"), patch.object(esde, "Adb", fake):
            self.assertEqual(self.run_cli(args), 2)
        self.assertTrue(reads)
        self.assertFalse(output.exists())

    def test_authorize_android_requires_selected_same_device_and_root(self):
        snapshot = self.snapshot_fixture()
        source, patch_file = self.base / "provider.json", self.base / "one-patch.json"
        core.write_json(source, {"identity_source": self.catalog["entries"][0]["receipt"]["payload"]["source"]})
        core.write_json(patch_file, self.patch)
        args = ["authorize-android", "--snapshot", str(snapshot), "--serial", "fixture-device",
                "--remote-rom-root", "/fixture/Roms", "--system", "nds", "--file", self.file,
                "--source", str(source), "--patch", str(patch_file), "--identity-key", str(self.key), "--out", str(self.base / "android-catalog.json")]
        fake, reads = self.fake_adb()
        with patch.object(esde, "resolve_selected_adb", return_value="/fixture/adb"), patch.object(esde, "Adb", fake):
            wrong = list(args)
            wrong[wrong.index("--serial") + 1] = "another-device"
            self.assertEqual(self.run_cli(wrong), 2)
            self.assertEqual(reads, [])
            wrong = list(args)
            wrong[wrong.index("--remote-rom-root") + 1] = "/another/Roms"
            self.assertEqual(self.run_cli(wrong), 2)
            self.assertEqual(reads, [])
            self.assertEqual(self.run_cli(args), 0)
        self.assertEqual(reads, ["/fixture/Roms/nds/" + self.file])
        self.assertEqual(core.load_json(self.base / "android-catalog.json")["entries"][0]["rom_fingerprint"], identity.fingerprint_file(self.path))

    def test_hash_android_outputs_only_actual_measurement_and_never_downloads_rom(self):
        snapshot = self.snapshot_fixture()
        output = self.base / "measured.json"
        args = ["hash-android", "--snapshot", str(snapshot), "--serial", "fixture-device",
                "--remote-rom-root", "/fixture/Roms", "--system", "nds", "--file", self.file, "--out", str(output)]
        fake, reads = self.fake_adb()
        with patch.object(esde, "resolve_selected_adb", return_value="/fixture/adb"), patch.object(esde, "Adb", fake):
            self.assertEqual(self.run_cli(args), 0)
            self.assertEqual(self.run_cli(args), 2, "Measured output must be an explicit new file")
        self.assertEqual(core.load_json(output), identity.fingerprint_file(self.path))
        self.assertEqual(reads, ["/fixture/Roms/nds/" + self.file] * 2)
        self.assertFalse((snapshot / "roms").exists())

    def test_actual_adb_stream_is_bounded_and_checks_parents_realpath_and_full_stat(self):
        data = b"rom"
        class FakeProcess:
            returncode = 0
            stdout = io.BytesIO(data)
            stderr = io.BytesIO()
            def communicate(self, timeout=None):
                return b"", b""
            def poll(self):
                return 0
            def kill(self):
                pass
        adb = esde.Adb("/fixture/adb", "fixture-device")
        marker = b"3:device:inode:2026-10-04 00:00:00.123456789 +0000:2026-10-04 00:00:00.123456789 +0000"
        with patch.object(adb, "shell_bytes", return_value=marker) as stat_reader, patch.object(esde.subprocess, "Popen", return_value=FakeProcess()) as process:
            measured = adb.fingerprint("/sdcard/Roms/nds/folder/fixture.nds", approved_root="/sdcard/Roms")
        self.assertEqual(measured, identity.fingerprint_stream(io.BytesIO(data)))
        script = stat_reader.call_args_list[0].args[0]
        self.assertIn("test ! -L /sdcard/Roms/nds", script)
        self.assertIn("test ! -L /sdcard/Roms/nds/folder", script)
        self.assertIn("readlink -f /sdcard/Roms", script)
        self.assertIn("%s:%d:%i:%y:%z", script)
        self.assertEqual(stat_reader.call_count, 2)
        self.assertEqual(process.call_args.args[0][1:3], ["-s", "fixture-device"])
        self.assertIn("exec-out", process.call_args.args[0])
        with self.assertRaisesRegex(esde.AdbError, "approved ROM root"):
            adb.fingerprint("/other/device/game.nds", approved_root="/sdcard/Roms")

    def test_adb_changed_stat_rejects_same_size_stream(self):
        class FakeProcess:
            returncode = 0
            stdout = io.BytesIO(b"rom")
            stderr = io.BytesIO()
            def communicate(self, timeout=None):
                return b"", b""
            def poll(self):
                return 0
            def kill(self):
                pass
        adb = esde.Adb("/fixture/adb", "fixture-device")
        with patch.object(adb, "shell_bytes", side_effect=[b"3:dev:inode:time.1:ctime", b"3:dev:inode:time.2:ctime"]), patch.object(esde.subprocess, "Popen", return_value=FakeProcess()):
            with self.assertRaisesRegex(esde.AdbError, "changed"):
                adb.fingerprint("/fixture/Roms/fixture.nds", approved_root="/fixture/Roms")


if __name__ == "__main__":
    unittest.main()
