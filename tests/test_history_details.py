import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from attach_history import attach
from workbench_store import init_run, upsert_job, get_job, get_state
from workbench_media import open_registered_media


PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c49444154789c636000020000040001f61738550000000049454e44ae426082")


class HistoricalDetailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run = self.root / "history"
        init_run(self.run, "History", {"mode": "historical_receipt"})
        upsert_job(self.run, "nds:游戏.nds", system="nds", file="游戏.nds", name="游戏", status="done", media={"covers": {"present": True}}, unknown=["players"])
        self.metadata = self.save("metadata.json", [{"system": "nds", "file": "游戏.nds", "name": "游戏", "desc": "完整介绍", "path": "./游戏.nds", "publisher": "发行商", "preserved": {"playcount": "8"}, "date_precision": "year"}])
        self.media = self.root / "actual.jpg"
        self.media.write_bytes(PNG)
        self.cache = self.save("cache.json", {"/sdcard/ES-DE/downloaded_media/nds/covers/游戏.jpg": {"local": str(self.media), "bytes": len(PNG)}})

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_full_metadata_real_cross_suffix_image_and_history_are_visible(self):
        result = attach(self.run, self.metadata, cache_index=self.cache)
        self.assertEqual(result["local_previews"], 1)
        job = get_job(self.run, "nds:游戏.nds")
        self.assertEqual(job["details"]["desc"], "完整介绍")
        self.assertEqual(job["details"]["history"]["playcount"], "8")
        self.assertEqual(job["details"]["date_precision"], "year")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["unknown"], ["players"])
        preview = job["media"]["covers"]["extras"]["preview"]
        row, handle = open_registered_media(self.run, preview["asset_id"])
        with handle:
            self.assertEqual(handle.read(), PNG)
        self.assertEqual(row["mime"], "image/png")

    def test_missing_local_file_is_not_invented_and_remains_scanned(self):
        self.media.unlink()
        result = attach(self.run, self.metadata, cache_index=self.cache)
        self.assertEqual(result["local_previews"], 0)
        preview = get_job(self.run, "nds:游戏.nds")["media"]["covers"]["extras"]["preview"]
        self.assertFalse(preview["available"])
        self.assertIn("未缓存", preview["reason"])

    def test_optional_media_and_multiple_candidates_not_hidden(self):
        entries = json.loads(self.cache.read_text(encoding="utf-8"))
        entries["/sdcard/ES-DE/downloaded_media/nds/covers/游戏.png"] = {"local": str(self.media), "bytes": len(PNG)}
        entries["/sdcard/ES-DE/downloaded_media/nds/backcovers/游戏.jpg"] = {"local": str(self.media), "bytes": len(PNG)}
        self.cache.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        attach(self.run, self.metadata, cache_index=self.cache)
        media = get_job(self.run, "nds:游戏.nds")["media"]
        self.assertEqual(len(media["covers"]["candidates"]), 2)
        self.assertTrue(media["backcovers"]["extras"]["preview"]["available"])

    def test_extra_xml_and_all_raw_fields_with_credential_redaction(self):
        xml = self.root / "gamelist.xml"
        xml.write_text('<gameList><game id="keep"><path>./游戏.nds</path><name>游戏</name><desc>完整介绍</desc><playtime>60</playtime><private unit="seconds">preserve</private><password>secret-fixture</password></game></gameList>', encoding="utf-8")
        attach(self.run, self.metadata, cache_index=self.cache, gamelists=xml, system="nds")
        details = get_job(self.run, "nds:游戏.nds")["details"]
        self.assertEqual(details["attributes"]["id"], "keep")
        self.assertEqual(details["extra"]["private"][0]["value"], "preserve")
        self.assertEqual(details["history"]["playtime"], "60")
        self.assertNotIn("secret-fixture", json.dumps(details))
        self.assertIn("secret-fixture", xml.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
