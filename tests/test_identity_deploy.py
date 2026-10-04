"""Identity-to-write boundary tests with isolated keys and synthetic ROM bytes."""
from __future__ import annotations
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deploy
import identity
from esde_core import write_json, sha256


def approved_entry(rom, system, file, metadata=None, media=None):
    """Simulate the trusted HTTP response boundary; never contact a provider."""
    fingerprint = identity.fingerprint_file(rom)
    media = media or []
    candidates = []
    approved_media = []
    for index, item in enumerate(media):
        source = Path(item["local"])
        kind = item["relative"].split("/")[2]
        url = "https://screenscraper.fr/medias/fixture-" + str(index) + ".bin"
        candidates.append({"type": kind, "url": url})
        observed = identity.seal_download({"provider": "screenscraper", "source_url": url, "sha256": deploy.file_hash(source), "bytes": source.stat().st_size})
        approved_media.append({"type": kind, "relative": item["relative"].removeprefix("downloaded_media/"), "source_url": url, "path": str(source), "download_receipt": observed})
    metadata = metadata or {}
    candidate = {"provider": "screenscraper", "provider_game_id": "synthetic-" + file, "system": {"id": identity.SCREENSCRAPER_SYSTEMS[system]}, "name": metadata.get("name"), "description_zh": metadata.get("desc"), "rom_evidence": [{"size": fingerprint["size"], "md5": fingerprint["md5"], "sha1": fingerprint["sha1"], "crc": fingerprint["crc"]}], "media_candidates": candidates}
    candidate.update({key: value for key, value in metadata.items() if key not in ("name", "desc")})
    report = identity.seal_provider_report({"provider": "screenscraper", "endpoint": "jeuInfos.php", "query": {"systemeid": identity.SCREENSCRAPER_SYSTEMS[system], "romtaille": fingerprint["size"], "md5": fingerprint["md5"], "sha1": fingerprint["sha1"], "crc": fingerprint["crc"]}, "candidates": [candidate]})
    receipt = identity.authorize_patch(report, system=system, file=file, rom_path=rom, metadata=metadata, media=approved_media)
    return {"system": system, "file": file, "rom_fingerprint": fingerprint, "receipt": receipt}


class IdentityDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {identity.KEY_ENV: str(self.root / "private" / "identity.json")}).start()
        self.target = self.root / "ES-DE"
        self.target.mkdir()
        self.rom_root = self.root / "Roms"
        self.rom = self.rom_root / "nds" / "Mario.nds"
        self.rom.parent.mkdir(parents=True)
        self.rom.write_bytes(b"synthetic Mario ROM, not real game data")
        self.relative = "gamelists/nds/gamelist.xml"
        self.original = b'<gameList><game><path>./Mario.nds</path><name>Mario</name><playcount>4</playcount><private>keep</private></game></gameList>'
        self.target_xml = self.target / self.relative
        self.target_xml.parent.mkdir(parents=True)
        self.target_xml.write_bytes(self.original)
        self.source = self.root / "prepared.xml"
        self.source.write_bytes(self.original.replace(b"<name>Mario</name>", b"<name>Super Mario</name>"))
        self.manifest = self.root / "manifest.json"
        self.catalog = self.root / "catalog.json"
        self.run = self.root / "run"
        self.entry = approved_entry(self.rom, "nds", "Mario.nds", {"name": "Super Mario"})
        write_json(self.catalog, {"schema_version": 1, "entries": [self.entry]})
        write_json(self.manifest, {"files": [{"local": str(self.source), "relative": self.relative}], "identity_catalog": str(self.catalog)})

    def command(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return deploy.main(list(args))

    def plan(self):
        return self.command("plan", "--manifest", str(self.manifest), "--target", "local", "--esde-root", str(self.target), "--rom-root", str(self.rom_root), "--out", str(self.run))

    def test_signed_correct_identity_installs_but_wrong_title_does_not(self):
        self.source.write_bytes(self.source.read_bytes().replace(b"Super Mario", b"Zelda"))
        self.assertEqual(self.plan(), 2)
        self.assertEqual(self.target_xml.read_bytes(), self.original)
        self.assertFalse((self.run / "deployment_plan.json").exists())
        self.source.write_bytes(self.original.replace(b"<name>Mario</name>", b"<name>Super Mario</name>"))
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.command("apply", "--run", str(self.run)), 0)
        self.assertIn(b"Super Mario", self.target_xml.read_bytes())

    def test_missing_or_forged_receipt_blocks_before_library_write(self):
        write_json(self.manifest, {"files": [{"local": str(self.source), "relative": self.relative}]})
        self.assertEqual(self.plan(), 2)
        write_json(self.manifest, {"files": [{"local": str(self.source), "relative": self.relative}], "identity_catalog": str(self.catalog)})
        self.entry["receipt"]["payload"]["metadata"]["name"] = "Zelda"
        self.source.write_bytes(self.original.replace(b"<name>Mario</name>", b"<name>Zelda</name>"))
        write_json(self.catalog, {"schema_version": 1, "entries": [self.entry]})
        self.assertEqual(self.plan(), 2)
        self.assertEqual(self.target_xml.read_bytes(), self.original)

    def test_rom_or_catalog_changed_after_plan_blocks_all_apply(self):
        self.assertEqual(self.plan(), 0)
        self.rom.write_bytes(b"different ROM bytes")
        self.assertEqual(self.command("apply", "--run", str(self.run)), 2)
        self.assertEqual(self.target_xml.read_bytes(), self.original)
        self.rom.write_bytes(b"synthetic Mario ROM, not real game data")
        self.catalog.write_bytes(self.catalog.read_bytes() + b" ")
        self.assertEqual(self.command("apply", "--run", str(self.run)), 2)

    def test_editing_plan_and_adjacent_sha_cannot_remove_identity_gate(self):
        self.assertEqual(self.plan(), 0)
        path = self.run / "deployment_plan.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["identity_gate"] = {"status": "pass", "mode": "structure_or_theme_only", "games": [], "media": [], "key_path": None}
        write_json(path, data)
        (self.run / "deployment_plan.sha256").write_text(deploy.file_hash(path), encoding="ascii")
        self.assertEqual(self.command("apply", "--run", str(self.run)), 2)
        self.assertEqual(self.target_xml.read_bytes(), self.original)

    def test_rollback_restores_original_after_identity_or_rom_is_unavailable(self):
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.command("apply", "--run", str(self.run)), 0)
        self.rom.unlink()
        self.catalog.unlink()
        self.assertEqual(self.command("rollback", "--run", str(self.run)), 0)
        self.assertEqual(self.target_xml.read_bytes(), self.original)

    def test_path_format_only_repair_needs_no_game_identity(self):
        self.source.write_bytes(self.original.replace(b"./Mario.nds", b"Mario.nds"))
        write_json(self.manifest, {"files": [{"local": str(self.source), "relative": self.relative}]})
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.command("apply", "--run", str(self.run)), 0)

    def test_wrong_cover_bytes_or_target_cannot_borrow_a_correct_receipt(self):
        cover = self.root / "cover.png"
        cover.write_bytes(b"synthetic correct Mario cover")
        relative = "downloaded_media/nds/covers/Mario.png"
        entry = approved_entry(self.rom, "nds", "Mario.nds", media=[{"local": str(cover), "relative": relative}])
        write_json(self.catalog, {"schema_version": 1, "entries": [entry]})
        cover.write_bytes(b"synthetic wrong Zelda cover")
        write_json(self.manifest, {"files": [{"local": str(cover), "relative": relative}], "identity_catalog": str(self.catalog)})
        self.assertEqual(self.plan(), 2)
        self.assertFalse((self.target / relative).exists())

    def test_history_cannot_be_cleared_with_a_metadata_identity_receipt(self):
        self.source.write_bytes(self.source.read_bytes().replace(b"<playcount>4</playcount>", b""))
        self.assertEqual(self.plan(), 2)
        self.assertEqual(self.target_xml.read_bytes(), self.original)

    def test_unique_case_alias_is_a_structural_repair_using_actual_inventory(self):
        original = self.original.replace(b"./Mario.nds", b"./MARIO.NDS")
        self.target_xml.write_bytes(original)
        self.source.write_bytes(self.original)
        write_json(self.manifest, {"files": [{"local": str(self.source), "relative": self.relative}]})
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.command("apply", "--run", str(self.run)), 0)
        self.assertEqual(self.target_xml.read_bytes(), self.original)

    def test_duplicate_normalization_preserves_authoritative_facts_and_private_data(self):
        from esde_core import parse_document, normalize_document, serialize_document
        original = b'<gameList><game><path>Mario.nds</path><name>Zelda stale duplicate</name><playcount>2</playcount></game><game><path>./Mario.nds</path><name>Mario</name><playcount>4</playcount><private><value>keep</value></private><private>repeat</private></game></gameList>'
        self.target_xml.write_bytes(original)
        document, _ = normalize_document(parse_document(original), self.rom_root / "nds", ["Mario.nds"], [])
        self.source.write_bytes(serialize_document(document))
        write_json(self.manifest, {"files": [{"local": str(self.source), "relative": self.relative}]})
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.command("apply", "--run", str(self.run)), 0)
        result = self.target_xml.read_bytes()
        self.assertNotIn(b"Zelda", result)
        self.assertIn(b"<playcount>4</playcount>", result)
        self.assertEqual(result.count(b"<private>"), 2)

    def test_operator_boolean_and_note_do_not_authorize_game_media(self):
        import media
        report = media.publish(None, None, "covers", {"technical_verified": True}, identity_confirmed=True, identity_note="Looks correct")
        self.assertFalse(report["identity"]["confirmed"])
        self.assertFalse(report["deployment_authorized"])
        self.assertFalse(report["review_annotation"]["authorizes_deployment"])

    def test_android_fingerprint_reads_only_selected_exact_file_and_rejects_stat_changes(self):
        digest = "a" * 64
        scripts = []
        class FakeAdb:
            serial = "explicit-selected-fixture"
            def shell_bytes(self, script):
                scripts.append(script)
                return ("2:3:17:1700:stamp\n" + digest + "\n17\n2:3:17:1700:stamp\n").encode("ascii")
        target = type("FakeTarget", (), {"adb": FakeAdb()})()
        result = deploy.rom_fingerprint({"type": "android", "rom_root": "/storage/emulated/0/ROMs"}, target, "nds", "Mario.nds")
        self.assertEqual(result, {"sha256": digest, "size": 17})
        self.assertIn("set -eu", scripts[0])
        self.assertIn("test ! -L", scripts[0])
        self.assertIn("realpath", scripts[0])
        self.assertIn("sha256sum /storage/emulated/0/ROMs/nds/Mario.nds", scripts[0])
        with patch.object(target.adb, "shell_bytes", return_value=("2:3:17:1700:before\n" + digest + "\n17\n2:3:17:1700:after\n").encode("ascii")):
            with self.assertRaises(deploy.DeploymentError):
                deploy.rom_fingerprint({"type": "android", "rom_root": "/storage/emulated/0/ROMs"}, target, "nds", "Mario.nds")

    def test_two_actual_rom_extensions_cannot_share_an_approved_media_stem(self):
        cover = self.root / "cover.png"
        cover.write_bytes(b"synthetic Mario cover")
        relative = "downloaded_media/nds/covers/Mario.png"
        entry = approved_entry(self.rom, "nds", "Mario.nds", media=[{"local": str(cover), "relative": relative}])
        write_json(self.catalog, {"schema_version": 1, "entries": [entry]})
        self.rom.with_name("mario.zip").write_bytes(b"different synthetic game with shared stem and case")
        write_json(self.manifest, {"files": [{"local": str(cover), "relative": relative}], "identity_catalog": str(self.catalog)})
        self.assertEqual(self.plan(), 2)
        self.assertFalse((self.target / relative).exists())
        requests = []
        class FakeAdb:
            def inventory(self, root):
                requests.append(root)
                return [{"path": root + "/Mario.nds", "size": 1}, {"path": root + "/mario.zip", "size": 2}]
        target = type("FakeTarget", (), {"adb": FakeAdb()})()
        plan = {"target": {"type": "android", "rom_root": "/storage/emulated/0/ROMs"}, "files": [{"scope": "esde", "relative": relative, "local": str(cover), "original": {"exists": False, "sha256": None}, "desired_sha256": deploy.file_hash(cover), "size": cover.stat().st_size}]}
        with self.assertRaisesRegex(deploy.DeploymentError, "shared"):
            deploy.build_identity_gate(self.run, plan, target, self.catalog)
        self.assertEqual(requests, ["/storage/emulated/0/ROMs/nds"])

    def test_theme_scope_cannot_write_game_metadata_or_media(self):
        for relative, root in (("gamelists/nds/gamelist.xml", self.target), ("nds/gamelist.xml", self.target / "gamelists"), ("Mario.png", self.target / "downloaded_media" / "nds" / "covers"), ("theme.xml", self.root / "themes")):
            with self.subTest(relative=relative):
                root.mkdir(parents=True, exist_ok=True)
                write_json(self.manifest, {"files": [{"scope": "theme", "local": str(self.source), "relative": relative}]})
                self.assertEqual(self.command("plan", "--manifest", str(self.manifest), "--target", "local", "--esde-root", str(self.target), "--theme-root", str(root), "--out", str(self.run)), 2)
        self.assertEqual(self.target_xml.read_bytes(), self.original)

    def test_independent_theme_xml_still_installs_without_game_identity(self):
        theme = self.root / "themes" / "selected"
        theme.mkdir(parents=True)
        self.source.write_bytes(b"<theme><formatVersion>7</formatVersion></theme>")
        write_json(self.manifest, {"files": [{"scope": "theme", "local": str(self.source), "relative": "theme.xml"}]})
        self.assertEqual(self.command("plan", "--manifest", str(self.manifest), "--target", "local", "--esde-root", str(self.target), "--theme-root", str(theme), "--out", str(self.run)), 0)
        self.assertEqual(self.command("apply", "--run", str(self.run)), 0)
        self.assertEqual((theme / "theme.xml").read_bytes(), self.source.read_bytes())


if __name__ == "__main__":
    unittest.main()
