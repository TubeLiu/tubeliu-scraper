import json
import contextlib
import io
import os
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from finish import finish
import deploy
import identity
from test_identity_deploy import approved_entry
from import_history import import_all
from workbench_store import emit_update, get_jobs, get_state, init_run, upsert_job


class ReceiptsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.key_patch = patch.dict(os.environ, {identity.KEY_ENV: str(self.root / "private" / "key.json")})
        self.key_patch.start()
        init_run(self.run, "Receipt test")

    def tearDown(self):
        self.key_patch.stop()
        self.temp.cleanup()

    def save(self, name, data):
        path = self.run / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def valid_deployment(self):
        target = self.root / "ES-DE"
        xml = target / "gamelists" / "nds" / "gamelist.xml"
        xml.parent.mkdir(parents=True)
        xml.write_bytes(b"<gameList />")
        source = self.run / "prepared.xml"
        source.write_bytes(b"<gameList />")
        manifest = self.save("manifest.json", {"files": [{"local": str(source), "relative": "gamelists/nds/gamelist.xml"}]})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(deploy.main(["plan", "--target", "local", "--esde-root", str(target), "--manifest", str(manifest), "--out", str(self.run)]), 0)
            self.assertEqual(deploy.main(["verify", "--run", str(self.run)]), 0)
        return self.run / "deployment_verification.json"

    def test_hash_pass_without_visual_cannot_complete_deploy(self):
        structure = self.save("structure.json", {"status": "pass"})
        deployment = self.save("deployment.json", {"status": "pass"})
        with self.assertRaises(ValueError):
            finish(self.run, "deploy", structure=structure, deployment=deployment)
        self.assertEqual(get_state(self.run)["run"]["status"], "blocked")
        self.assertFalse((self.run / "completion.json").exists())

    def test_visual_evidence_and_unknowns_preserved(self):
        structure = self.save("structure.json", {"status": "pass"})
        deployment = self.valid_deployment()
        (self.run / "screen.png").write_bytes(b"actual test evidence placeholder")
        visual = self.save("visual.json", {"status": "pass", "checked_at": "2026-01-01", "checks": {"metadata_display": True}, "evidence": ["screen.png"]})
        upsert_job(self.run, "nds:file.nds", unknown=["publisher"])
        with self.assertRaises(ValueError):
            finish(self.run, "deploy", structure=structure, deployment=deployment, visual=visual)
        proof = finish(self.run, "deploy", structure=structure, deployment=deployment, visual=visual, report_unknowns=True)
        self.assertEqual(proof["unknown_game_records"], 1)
        self.assertEqual(get_jobs(self.run)["items"][0]["unknown"], ["publisher"])
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")

    def test_visual_true_and_screenshot_cannot_replace_signed_identity_gate(self):
        structure = self.save("structure.json", {"status": "pass"})
        deployment = self.valid_deployment()
        data = json.loads(deployment.read_text(encoding="utf-8"))
        data.pop("identity_verification")
        deployment.write_text(json.dumps(data), encoding="utf-8")
        (self.run / "screen.png").write_bytes(b"synthetic evidence")
        visual = self.save("visual.json", {"status": "pass", "checked_at": "2026-01-01", "checks": {"identity": True}, "evidence": ["screen.png"]})
        with self.assertRaises(ValueError):
            finish(self.run, "deploy", structure=structure, deployment=deployment, visual=visual)
        self.assertFalse((self.run / "completion.json").exists())

    def test_metadata_prepare_requires_signed_proof_and_rechecks_staged_bytes(self):
        structure = self.save("structure.json", {"status": "pass", "checks": ["structure", "metadata"]})
        with self.assertRaises(ValueError):
            finish(self.run, "prepare", structure=structure)
        rom_root = self.root / "Roms" / "nds"
        rom_root.mkdir(parents=True)
        rom = rom_root / "Mario.nds"
        rom.write_bytes(b"synthetic Mario ROM")
        entry = approved_entry(rom, "nds", "Mario.nds", {"name": "Super Mario"})
        source = self.run / "source.xml"
        output = self.run / "prepared.xml"
        source.write_bytes(b"<gameList><game><path>./Mario.nds</path><name>Mario</name></game></gameList>")
        output.write_bytes(source.read_bytes().replace(b"<name>Mario</name>", b"<name>Super Mario</name>"))
        catalog = self.save("catalog.json", {"schema_version": 1, "entries": [entry]})
        payload = {"schema_version": 1, "system": "nds", "source_path": str(source), "output_path": str(output), "identity_catalog_path": str(catalog), "source_sha256": deploy.file_hash(source), "output_sha256": deploy.file_hash(output), "identity_catalog_sha256": deploy.file_hash(catalog), "rom_root": str(rom_root), "binding": {"kind": "local", "system_rom_root": str(rom_root)}, "entries": [{"system": "nds", "file": "Mario.nds", "rom_fingerprint": entry["rom_fingerprint"], "approved_metadata": {"name": "Super Mario"}, "receipt": entry["receipt"]}]}
        proof = self.save("preparation.json", identity.seal_preparation(payload))
        finish(self.run, "prepare", structure=structure, identity_preparation=proof)
        output.write_bytes(output.read_bytes().replace(b"Super Mario", b"Zelda"))
        with self.assertRaises(ValueError):
            finish(self.run, "prepare", structure=structure, identity_preparation=proof)
        self.assertEqual(get_state(self.run)["run"]["status"], "blocked")
        self.assertFalse((self.run / "completion.json").exists())

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is required for real image QA fixtures")
    def test_prepared_media_requires_fresh_bytes_and_run_owned_qa_paths(self):
        import media as media_tools
        structure = self.save("structure.json", {"status": "pass", "checks": ["structure", "metadata"]})
        rom_root = self.root / "Roms" / "nds"
        rom_root.mkdir(parents=True)
        rom = rom_root / "Mario.nds"
        rom.write_bytes(b"synthetic Mario ROM")
        cover = self.run / "Mario.png"
        from PIL import Image
        Image.new("RGB", (8, 8), "red").save(cover)
        relative = "downloaded_media/nds/covers/Mario.png"
        entry = approved_entry(rom, "nds", "Mario.nds", {"name": "Super Mario"}, media=[{"local": str(cover), "relative": relative}])
        source = self.run / "source.xml"
        output = self.run / "prepared.xml"
        source.write_bytes(b"<gameList><game><path>./Mario.nds</path><name>Mario</name></game></gameList>")
        output.write_bytes(source.read_bytes().replace(b"<name>Mario</name>", b"<name>Super Mario</name>"))
        catalog = self.save("catalog.json", {"schema_version": 1, "entries": [entry]})
        payload = {"schema_version": 1, "system": "nds", "source_path": str(source), "output_path": str(output), "identity_catalog_path": str(catalog), "source_sha256": deploy.file_hash(source), "output_sha256": deploy.file_hash(output), "identity_catalog_sha256": deploy.file_hash(catalog), "rom_root": str(rom_root), "binding": {"kind": "local", "system_rom_root": str(rom_root)}, "entries": [{"system": "nds", "file": "Mario.nds", "rom_fingerprint": entry["rom_fingerprint"], "approved_metadata": {"name": "Super Mario"}, "receipt": entry["receipt"]}]}
        proof = self.save("preparation.json", identity.seal_preparation(payload))
        qa_data = media_tools.check_files([{"path": str(cover), "kind": "image"}])
        qa = self.save("media.json", qa_data)
        finish(self.run, "prepare", structure=structure, media=qa, require_media=True, identity_preparation=proof)
        original = cover.read_bytes()
        Image.new("RGB", (8, 8), "blue").save(cover)
        with self.assertRaisesRegex(ValueError, "changed"):
            finish(self.run, "prepare", structure=structure, media=qa, require_media=True, identity_preparation=proof)
        # Even replacing the unsigned QA hashes with the new bytes does not
        # make those bytes the provider-approved cover for this ROM.
        qa = self.save("media.json", media_tools.check_files([{"path": str(cover), "kind": "image"}]))
        with self.assertRaisesRegex(ValueError, "exact approved"):
            finish(self.run, "prepare", structure=structure, media=qa, require_media=True, identity_preparation=proof)
        cover.write_bytes(original)
        wrong_kind = media_tools.check_files([{"path": str(cover), "kind": "image"}])
        wrong_kind["files"][0]["kind"] = "video"
        qa = self.save("media.json", wrong_kind)
        with self.assertRaisesRegex(ValueError, "kind/bytes"):
            finish(self.run, "prepare", structure=structure, media=qa, require_media=True, identity_preparation=proof)
        outside = self.root / "outside.png"
        outside.write_bytes(original)
        qa = self.save("media.json", media_tools.check_files([{"path": str(outside), "kind": "image"}]))
        with self.assertRaisesRegex(ValueError, "inside this run"):
            finish(self.run, "prepare", structure=structure, media=qa, require_media=True, identity_preparation=proof)
        self.assertFalse((self.run / "completion.json").exists())

    def test_changed_media_requires_media_receipt(self):
        structure = self.save("structure.json", {"status": "pass"})
        with self.assertRaises(ValueError):
            finish(self.run, "prepare", structure=structure, require_media=True)
        media = self.save("media.json", {"status": "blocked"})
        with self.assertRaises(ValueError):
            finish(self.run, "prepare", structure=structure, media=media, require_media=True)

    def test_audit_can_report_missing_without_installing(self):
        audit = self.save("audit.json", {"status": "needs_review", "actual_rom_files": 1})
        upsert_job(self.run, "nds:file.nds", missing=["covers"], status="pending")
        finish(self.run, "audit", audit=audit)
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")
        self.assertEqual(get_jobs(self.run)["items"][0]["status"], "pending")

    def test_history_import_requires_full_receipt_and_remains_historical(self):
        metadata = self.save("metadata.json", [{"system": "nds", "file": "game.nds", "path": "./game.nds", "name": "游戏", "unknown_fields": ["publisher"]}])
        deployment = self.save("deploy.json", {"games": [{"system": "nds", "file": "game.nds", "video_kind": "screenshot_preview"}]})
        verification = self.save("verify.json", {"status": "pass", "unique_actual_rom_references_checked": True, "backup_hashes_checked": True, "metadata_records_checked": 1, "complete_six_media_files": 1, "missing_media": []})
        result = import_all(metadata, deployment, verification, self.run, "History")
        self.assertEqual(result["mode"], "historical_receipt")
        job = get_jobs(self.run)["items"][0]
        self.assertEqual(job["video_kind"], "screenshot_preview")
        self.assertEqual(job["unknown"], ["publisher"])
        self.assertTrue(get_state(self.run)["run"]["historical"]["no_live_device_rescan"] if "historical" in get_state(self.run)["run"] else get_state(self.run)["run"]["details"]["historical"]["no_live_device_rescan"])

    def test_bad_historical_mapping_rejected(self):
        metadata = self.save("metadata.json", [{"system": "nds", "file": "game.nds", "path": "./another.nds"}])
        deployment = self.save("deploy.json", {"games": [{"system": "nds", "file": "game.nds"}]})
        verification = self.save("verify.json", {"status": "pass", "unique_actual_rom_references_checked": True, "backup_hashes_checked": True, "metadata_records_checked": 1, "complete_six_media_files": 1, "missing_media": []})
        with self.assertRaises(ValueError):
            import_all(metadata, deployment, verification, self.run, "History")

    def test_receipt_from_other_run_cannot_seal_this_run(self):
        outside = self.root / "other-run-pass.json"
        outside.write_text('{"status":"pass"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            finish(self.run, "prepare", structure=outside)
        self.assertEqual(get_state(self.run)["run"]["status"], "blocked")

    def test_completed_scope_closes_waiting_phase_without_filling_unknowns(self):
        structure = self.save("structure.json", {"status": "pass", "checks": ["structure"]})
        emit_update(self.run, phase="awaiting_scoped_completion", status="running", phase_status="pending")
        finish(self.run, "prepare", structure=structure)
        waiting = next(p for p in get_state(self.run)["phases"] if p["phase"] == "awaiting_scoped_completion")
        self.assertEqual(waiting["status"], "done")


if __name__ == "__main__":
    unittest.main()
