import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from finish import finish
from import_history import import_all
from workbench_store import emit_update, get_jobs, get_state, init_run, upsert_job


class ReceiptsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        init_run(self.run, "Receipt test")

    def tearDown(self):
        self.temp.cleanup()

    def save(self, name, data):
        path = self.run / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_hash_pass_without_visual_cannot_complete_deploy(self):
        structure = self.save("structure.json", {"status": "pass"})
        deployment = self.save("deployment.json", {"status": "pass"})
        with self.assertRaises(ValueError):
            finish(self.run, "deploy", structure=structure, deployment=deployment)
        self.assertEqual(get_state(self.run)["run"]["status"], "blocked")
        self.assertFalse((self.run / "completion.json").exists())

    def test_visual_evidence_and_unknowns_preserved(self):
        structure = self.save("structure.json", {"status": "pass"})
        deployment = self.save("deployment.json", {"status": "pass"})
        (self.run / "screen.png").write_bytes(b"actual test evidence placeholder")
        visual = self.save("visual.json", {"status": "pass", "checked_at": "2026-01-01", "checks": {"metadata_display": True}, "evidence": ["screen.png"]})
        upsert_job(self.run, "nds:file.nds", unknown=["publisher"])
        with self.assertRaises(ValueError):
            finish(self.run, "deploy", structure=structure, deployment=deployment, visual=visual)
        proof = finish(self.run, "deploy", structure=structure, deployment=deployment, visual=visual, report_unknowns=True)
        self.assertEqual(proof["unknown_game_records"], 1)
        self.assertEqual(get_jobs(self.run)["items"][0]["unknown"], ["publisher"])
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")

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
