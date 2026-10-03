"""Complete scan details and scoped, recoverable, read-only media preview cache."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import esde
import media
import preview_cache as cache
from esde_core import sha256, write_json
from workbench_store import emit_update, get_job, get_state, init_run, upsert_job

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGNUaAhgYGBgYgADAAs6APR/y1klAAAAAElFTkSuQmCC")


class ScanDetailsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.rom = self.base / "Roms" / "nds"
        self.rom.mkdir(parents=True)
        self.esde = self.base / "ES-DE"
        self.list = self.esde / "gamelists" / "nds" / "gamelist.xml"
        self.list.parent.mkdir(parents=True)
        self.file = "子目录/游戏 & 中文.nds"
        (self.rom / self.file).parent.mkdir()
        (self.rom / self.file).write_bytes(b"ROM bytes stay untouched")
        self.list.write_text('<gameList><game custom="preserve"><path>./子目录/游戏 &amp; 中文.nds</path>'
                             '<name>中文游戏</name><desc>扫描到的完整介绍\n第二段。</desc>'
                             '<developer>制作组</developer><publisher>发行商</publisher><genre>冒险</genre>'
                             '<players>2</players><releasedate>20010101T000000</releasedate><rating>0.75</rating>'
                             '<scrapername>matched-game</scrapername><playcount>7</playcount><playtime>95</playtime>'
                             '<lastplayed>20261003T121314</lastplayed><favorite>true</favorite>'
                             '<custom id="1"><nested a="yes">一</nested></custom><custom id="2">二</custom>'
                             '</game></gameList>', encoding="utf-8")
        self.png = self.esde / "downloaded_media" / "nds" / "covers" / "子目录" / "游戏 & 中文.png"
        self.png.parent.mkdir(parents=True)
        self.png.write_bytes(PNG)

    def audit(self, out):
        with contextlib.redirect_stdout(io.StringIO()):
            return esde.main(["audit", "--rom-root", str(self.rom.parent), "--esde-root", str(self.esde), "--out", str(out)])

    def test_local_scan_keeps_every_fact_private_node_and_real_preview(self):
        optional = self.esde / "downloaded_media" / "nds" / "backcovers" / "子目录" / "游戏 & 中文.png"
        optional.parent.mkdir(parents=True)
        optional.write_bytes(PNG)
        out = self.base / "run"
        self.assertEqual(self.audit(out), 0)
        job = get_job(out, "nds:" + self.file)
        details = job["details"]
        self.assertEqual(details["desc"], "扫描到的完整介绍\n第二段。")
        self.assertEqual(details["rating"], "0.75")
        self.assertEqual(details["scrapername"], "matched-game")
        self.assertEqual(details["history"], {"playcount": "7", "playtime": "95", "lastplayed": "20261003T121314"})
        self.assertEqual(details["protected"]["favorite"], "true")
        self.assertEqual(details["extra"]["attributes"], {"custom": "preserve"})
        fields = details["extra"]["fields"]
        self.assertEqual([field["attributes"]["id"] for field in fields], ["1", "2"])
        self.assertEqual(fields[0]["children"][0]["text"], "一")
        self.assertEqual(details["extra"]["original_paths"], ["./" + self.file])
        self.assertEqual(details["file"]["size"], len(b"ROM bytes stay untouched"))
        for kind in ("covers", "backcovers"):
            preview = job["media"][kind]["extras"]["preview"]
            self.assertTrue(preview["available"])
            self.assertEqual(preview["mime"], "image/png")
            self.assertNotIn(str(self.png), preview["url"])
        self.assertEqual((self.rom / self.file).read_bytes(), b"ROM bytes stay untouched")

    def test_snapshot_audit_registers_remote_without_connecting_or_fetching(self):
        snapshot = self.base / "snapshot"
        target = snapshot / "es-de" / "gamelists" / "nds" / "gamelist.xml"
        target.parent.mkdir(parents=True)
        data = self.list.read_bytes()
        target.write_bytes(data)
        write_json(snapshot / "manifest.json", {"complete": True, "serial": "explicit-tablet", "adb_executable": "explicit-adb",
                   "remote_esde_root": "/sdcard/ES-DE", "remote_rom_root": "/storage/card/Roms",
                   "remote_media_root": "/sdcard/ES-DE/downloaded_media",
                   "files": [{"local": "es-de/gamelists/nds/gamelist.xml", "sha256": sha256(data)}]})
        write_json(snapshot / "rom_inventory.json", [{"path": "/storage/card/Roms/nds/" + self.file, "size": 123456}])
        write_json(snapshot / "media_inventory.json", [{"path": "/sdcard/ES-DE/downloaded_media/nds/covers/子目录/游戏 & 中文.png", "size": len(PNG)}])
        out = self.base / "run"
        with patch.object(cache, "_require_device", side_effect=AssertionError("Audit must not connect")), \
             patch.object(cache, "_fetch", side_effect=AssertionError("Audit must not download media")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(esde.main(["audit", "--snapshot", str(snapshot), "--out", str(out)]), 0)
        job = get_job(out, "nds:" + self.file)
        self.assertEqual(job["details"]["file"]["size"], 123456)
        self.assertEqual(job["media"]["covers"]["extras"]["preview"]["cache_state"], "pending")
        self.assertFalse((out / "cache").exists())

    def test_prepared_media_keeps_original_android_reference_history_and_details(self):
        run = self.base / "run"
        init_run(run)
        android = "/sdcard/ES-DE/downloaded_media/nds/covers/游戏.png"
        upsert_job(run, "nds:game", status="done", media={"covers": {"present": True, "path": android}},
                   details={"desc": "已扫描资料", "history": {"playtime": "95"}, "extra": {"custom": "keep"}})
        report = media.image_info(self.png)
        report["path"] = str(self.png)
        published = media.publish(run, "nds:game", "covers", report)
        job = get_job(run, "nds:game")
        self.assertEqual(published["path"], str(self.png))
        self.assertEqual(job["media"]["covers"]["path"], android)
        self.assertTrue(job["media"]["covers"]["extras"]["preview"]["available"])
        self.assertEqual(job["details"]["history"]["playtime"], "95")
        self.assertEqual(job["details"]["extra"]["custom"], "keep")
        self.assertEqual(job["status"], "done")

    def test_secret_xml_values_are_redacted_in_detail_without_changing_source(self):
        original = self.list.read_text(encoding="utf-8")
        self.list.write_text(original.replace("</game>", '<password value="secret-attribute">secret-text</password><nestedPrivate token="secret-token">kept</nestedPrivate></game>'), encoding="utf-8")
        out = self.base / "redacted-run"
        self.assertEqual(self.audit(out), 0)
        serialized = json.dumps(get_job(out, "nds:" + self.file), ensure_ascii=False)
        for secret in ("secret-attribute", "secret-text", "secret-token"):
            self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertIn("secret-text", self.list.read_text(encoding="utf-8"))


class ReadOnlyCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / "run"
        init_run(self.run)
        emit_update(self.run, phase="complete", status="completed", phase_status="done")
        self.remote = "/sdcard/ES-DE/downloaded_media/nds/covers/游戏.png"
        self.profile = cache.bind_remote_profile(self.run, adb="explicit-adb", serial="explicit-tablet",
                                                media_root="/sdcard/ES-DE/downloaded_media",
                                                inventory=[{"path": self.remote, "size": len(PNG)}])
        self.asset = {"id": "registered_asset", "job_id": "nds:game", "kind": "covers", "profile_id": self.profile,
                      "remote_path": self.remote, "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}

    def fake_fetch(self, profile, remote, relative, size, path):
        self.assertEqual((profile["serial"], remote, size), ("explicit-tablet", self.remote, len(PNG)))
        path.write_bytes(PNG)

    def test_binding_and_validation_never_enumerate_devices(self):
        with patch.object(cache.subprocess, "run", side_effect=AssertionError("Read-only registration must not connect")):
            cache.validate_remote_media(self.run, self.asset)
        profile = json.loads((self.run / "preview-profiles" / (self.profile + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(profile["serial"], "explicit-tablet")

    def test_inventory_scope_rejects_rom_settings_traversal_svg_and_other_file(self):
        for remote in ("/storage/card/Roms/nds/game.nds", "/sdcard/ES-DE/es_settings.xml",
                       "/sdcard/ES-DE/downloaded_media/nds/covers/../../es_settings.xml",
                       "/sdcard/ES-DE/downloaded_media/nds/covers/game.svg",
                       "/sdcard/ES-DE/downloaded_media/nds/covers/another.png"):
            with self.assertRaises(cache.PreviewUnavailable, msg=remote):
                cache.validate_remote_media(self.run, {**self.asset, "remote_path": remote})
        with self.assertRaises(cache.PreviewUnavailable):
            cache.validate_remote_media(self.run, {**self.asset, "profile_id": "../escape"})

    def test_materialize_deduplicates_concurrent_reads_and_reuses_verified_cache_offline(self):
        calls = []
        def fetch(*args):
            calls.append(1)
            time.sleep(.02)
            return self.fake_fetch(*args)
        with patch.object(cache, "_require_device"), patch.object(cache, "_fetch", side_effect=fetch):
            with ThreadPoolExecutor(max_workers=5) as pool:
                paths = list(pool.map(lambda _: cache.materialize(self.run, self.asset), range(5)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(set(paths)), 1)
        with patch.object(cache, "_require_device", side_effect=AssertionError("Verified cache works offline")):
            self.assertEqual(cache.materialize(self.run, self.asset).read_bytes(), PNG)
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")

    def test_disconnect_then_retry_keeps_terminal_state_and_unknown_files(self):
        unrelated = self.run / "cache" / "media" / "unrelated.part"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(b"must retain")
        with patch.object(cache, "_require_device", side_effect=cache.PreviewUnavailable("device_disconnected", "Reconnect selected device")):
            with self.assertRaisesRegex(cache.PreviewUnavailable, "Reconnect"):
                cache.materialize(self.run, self.asset)
        self.assertEqual(unrelated.read_bytes(), b"must retain")
        with patch.object(cache, "_require_device"), patch.object(cache, "_fetch", side_effect=self.fake_fetch):
            self.assertEqual(cache.materialize(self.run, self.asset).read_bytes(), PNG)
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")

    def test_hash_failure_does_not_accept_partial_or_delete_unrelated_cache(self):
        with patch.object(cache, "_require_device"), patch.object(cache, "_fetch", side_effect=lambda *args: args[-1].write_bytes(PNG)):
            with self.assertRaises(cache.PreviewUnavailable) as error:
                cache.materialize(self.run, {**self.asset, "sha256": "0" * 64})
        self.assertEqual(error.exception.code, "media_hash_mismatch")
        self.assertFalse(list((self.run / "cache" / "media").glob("*.png")))
        self.assertFalse(list((self.run / "cache" / "media").glob("*.part")))

    def test_timeout_kills_only_preview_reader_and_discards_its_partial_file(self):
        released = threading.Event()
        class BlockingStream:
            def read(self, size):
                released.wait(10)
                return b""
            def close(self): released.set()
        class FakeProcess:
            stdout = BlockingStream()
            returncode = None
            def kill(self):
                self.returncode = -1
                released.set()
            def wait(self, timeout=None): return self.returncode
            def poll(self): return self.returncode
        profile_path = self.run / "preview-profiles" / (self.profile + ".json")
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["timeout"] = 1
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        with patch.object(cache, "_require_device"), patch.object(cache.subprocess, "Popen", return_value=FakeProcess()):
            with self.assertRaises(cache.PreviewUnavailable) as error:
                cache.materialize(self.run, self.asset)
        self.assertEqual(error.exception.code, "preview_timeout")
        self.assertFalse(list((self.run / "cache" / "media").glob("*.png")))
        self.assertFalse(list((self.run / "cache" / "media").glob("*.part")))
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")

    def test_unknown_historical_size_is_measured_on_demand_and_offline_reusable(self):
        profile = cache.bind_remote_profile(self.run, adb="explicit-adb", serial="explicit-tablet", media_root="/sdcard/ES-DE/downloaded_media",
                                            inventory=[{"path": self.remote, "size": None}])
        asset = {**self.asset, "profile_id": profile, "size": None, "sha256": ""}
        with patch.object(cache, "_require_device"), patch.object(cache, "_measure", return_value=len(PNG)) as measure, \
             patch.object(cache, "_fetch", side_effect=self.fake_fetch):
            output = cache.materialize(self.run, asset)
        measure.assert_called_once()
        # The HTTP registry records the newly measured bytes/hash. It must not
        # force a new ADB connection when the user views the same cached media.
        updated = {**asset, "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}
        with patch.object(cache, "_require_device", side_effect=AssertionError("Offline")):
            self.assertEqual(cache.materialize(self.run, updated), output)

    def test_explicit_serial_unauthorized_never_selects_first_attached_device(self):
        result = subprocess.CompletedProcess([], 0, b"List of devices attached\nother device\nexplicit-tablet unauthorized\n", b"")
        with patch.object(cache.subprocess, "run", return_value=result):
            with self.assertRaises(cache.PreviewUnavailable) as error:
                cache._require_device({"adb": "explicit-adb", "serial": "explicit-tablet"})
        self.assertEqual(error.exception.code, "device_unauthorized")

    def test_binary_stream_uses_exec_out_and_rejects_truncation_and_growth(self):
        class FakeProcess:
            def __init__(self, data):
                self.stdout = io.BytesIO(data)
                self.returncode = 0
            def wait(self, timeout=None): return self.returncode
            def poll(self): return self.returncode
            def kill(self): self.returncode = -1
        profile = {"adb": "explicit-adb", "serial": "explicit-tablet", "media_root": "/sdcard/ES-DE/downloaded_media", "timeout": 1}
        for data, succeeds in ((PNG, True), (PNG[:-1], False), (PNG + b"extra", False)):
            target = self.run / ("stream_" + str(len(data)) + ".part")
            with patch.object(cache.subprocess, "Popen", return_value=FakeProcess(data)) as popen:
                if succeeds:
                    cache._fetch(profile, self.remote, "nds/covers/游戏.png", len(PNG), target)
                    self.assertEqual(target.read_bytes(), PNG)
                else:
                    with self.assertRaises(cache.PreviewUnavailable):
                        cache._fetch(profile, self.remote, "nds/covers/游戏.png", len(PNG), target)
            command = popen.call_args.args[0]
            self.assertEqual(command[:4], ["explicit-adb", "-s", "explicit-tablet", "exec-out"])
            self.assertNotIn("exec-in", command)
            self.assertIn("cat", command[-1])
            self.assertEqual(command[-1].count("stat -c %s"), 2)


if __name__ == "__main__":
    unittest.main()
