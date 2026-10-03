"""Real-media detail API, authenticated streaming, old-run migration and isolation."""
import base64
import hashlib
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from workbench import build_server
from workbench_store import connect, emit_update, get_events, get_job, get_jobs, get_state, init_run, upsert_job, upsert_jobs
from workbench_media import byte_range, open_registered_media, register_media, register_media_many, register_remote_media, register_remote_media_many, refresh_preview_availability

# Generated once from a 2x2 image and a genuine 16x16 H.264 MP4 containing one
# frame; tests need neither Pillow nor a video encoder installed at runtime.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGNUaAhgYGBgYgADAAs6APR/y1klAAAAAElFTkSuQmCC")
JPEG = base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDnaKKKwPmD/9k=")
MP4 = zlib.decompress(base64.b64decode("eJx9VM2LHEUUr9lNQggeIuyKhw2UGi+SmenumZ0kQxoiS3AOCnpIEBGb6qrq6WKqumqramZncvADcvAvyCWIN0HQsxcFD7l58CYueDAIoih6zUXHVz2zmdk1SVFV71fvs9571Y0QwoWfGeG0QmgDBQorIRMaK9ONEUJnC8s5yO4rRjzQT8+cns/v/XTj929+PRx8dffSj/jwpb/+nia9Lm5iqi3HcW8X4KAFrPYbb954rdnFr97aAxnjFAR72swkLzxOoqjTTKIkaJfem367fXBw0JoIxrUkVUvbYTv4bZVeSdDRxgtduT6mJCc0jbHlRdrBjOdS01Ea96N+hElF5MzxNJp2+tE0jjtY8bTkU+zGOaDL2LgZmMKeWZbGrQiMYMNKTDnLgscYLDJLqiFP4x6mpdWKZGAaY2+5lMIBujK9wqgHQPdVGsEVCLutK54m8aU4xgVxPjNuJEzQWDjYN5kuCsd92kywLy1YBEdS6xEp4ZCteE4KyleMCFe2jkGFIj7cQ1SeW0lACfi5HFsyy6hWhng4UyiRt0RU4AIULQk6hSWKOyhWnpkZYMHSBDBhxIQs8iwXJERiwvI6rwMuhqXPAWnDq2yoDUgXTAOmIz4D32myGy1hpkQVrk55xenYp90I18FDRS13JVhbmj3KNsgtTaktsMqhrCErOKRJpxXh/ZBLGrV6AE3wW1MyTXtXATjPTdrFwkCP4D1AC8EX2Yf2h2aiD+HJbvOP76Dt+b9/fv/+6d17ov3Kg5s/fPARQpunlNYTUJBqUjJ0bGz+Vq8GCnM1Gse1Tp6vo6eODZgtqMII8Lt+VMfc/L+3EPvpcR4b9/xyIXSRM++A7nDp/Mpi6be2bXymmCAAsGInc79er5v360OzZNIeScK3uK55K3ybA1IxyYNO4y1oTwFga6Jqp+vXvMgWsh0Gb2AtjWfGVuIFbjzrfC4Bf+m8Y2s6n4Qf0BNKEZI+jwZAB0/UODmen89h74DXvQZD5+YP0HNDoF8cvncndORU2M5eG9x9u4HOlH/88t2L3/7z8GEw3ILXuazo9s9Hzpyvq30UurHsxw4kQY/zH60tkN1eiDa+XvGoXtOPYOVj5kOX3lG8pmG8sN4R6KIlxsj17Jpi0faXP/c6OLwAf2pyVJfXyaToJa0YZpT8B2K9WNQ="))


class DetailBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "runs" / "historical"
        self.assets = self.root / "assets"
        self.assets.mkdir()
        (self.assets / "index.html").write_text("<title>Test</title>", encoding="utf-8")
        init_run(self.run, "真实历史")
        self.id = "dreamcast:中文 folder/game & title.chd"
        upsert_job(self.run, self.id, name="卡普空对SNK", system="dreamcast", status="done",
                   media={"covers": {"status": "historical_receipt", "path": "/storage/SD/ES-DE/downloaded_media/dreamcast/covers/game.png"}},
                   details={"desc": "扫描到的游戏简介", "publisher": "CAPCOM", "history": {"playcount": "7", "playtime": "95"}, "extra": {"private": "keep"}})
        emit_update(self.run, phase="historical", status="completed", completed=1, total=1)
        self.png = self.root / "real.png"
        self.jpg = self.root / "real.jpg"
        self.mp4 = self.root / "real.mp4"
        self.png.write_bytes(PNG)
        self.jpg.write_bytes(JPEG)
        self.mp4.write_bytes(MP4)
        self.server = build_server(self.run.parent, self.assets, port=0, access_token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, method="GET", token="test-token", headers=None):
        actual = dict(headers or {})
        if token is not None:
            actual["X-Workbench-Token"] = token
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        conn.request(method, path, headers=actual)
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        conn.close()
        return result

    def test_encoded_item_id_returns_complete_details_without_list_description_bloat(self):
        path = "/api/runs/historical/jobs/" + quote(self.id, safe="")
        status, _, body = self.request(path)
        self.assertEqual(status, 200)
        item = json.loads(body)["job"]
        self.assertEqual(item["id"], self.id)
        self.assertEqual(item["details"]["desc"], "扫描到的游戏简介")
        self.assertEqual(item["details"]["history"], {"playcount": "7", "playtime": "95"})
        self.assertNotIn("details", get_jobs(self.run)["items"][0])
        self.assertIn("details", get_jobs(self.run, include_details=True)["items"][0])
        self.assertEqual(self.request(path, token=None)[0], 401)
        self.assertEqual(self.request(path, token="wrong")[0], 401)
        self.assertEqual(self.request("/api/runs/historical/jobs/missing")[0], 404)

    def test_partial_metadata_keeps_original_history_and_redacts_all_credentials(self):
        upsert_jobs(self.run, [{"id": self.id, "details": {"developer": "真实制作组", "history": {"lastplayed": "20261003T121314"}, "password": "never-persist", "extra": {"ssid": "private-wifi", "source": "https://test.invalid/data?devpassword=never-url&game=3"}}}])
        upsert_job(self.run, self.id, status="done")
        item = get_job(self.run, self.id)
        self.assertEqual(item["details"]["history"]["playcount"], "7")
        self.assertEqual(item["details"]["extra"]["private"], "keep")
        with connect(self.run) as db:
            saved = "\n".join(str(row) for table in ("jobs", "events") for row in db.execute("SELECT * FROM " + table))
        for secret in ("never-persist", "private-wifi", "never-url"):
            self.assertNotIn(secret, saved)

    def test_registered_png_and_jpeg_show_real_bytes_and_keep_receipt_and_terminal_state(self):
        for kind, path, data, mime in (("covers", self.png, PNG, "image/png"), ("backcovers", self.jpg, JPEG, "image/jpeg")):
            preview = register_media(self.run, self.id, kind, path, origin="historical_verified_cache")
            status, headers, body = self.request(preview["url"])
            self.assertEqual((status, body, headers["Content-Type"]), (200, data, mime))
            self.assertEqual(preview["sha256"], hashlib.sha256(data).hexdigest())
            self.assertTrue(preview["cached"])
            self.assertEqual(self.request(preview["url"], token=None)[0], 401)
            self.assertEqual(self.request(preview["url"], token="wrong")[0], 401)
        item = get_job(self.run, self.id)
        self.assertEqual(item["media"]["covers"]["status"], "historical_receipt")
        self.assertTrue(item["media"]["covers"]["path"].startswith("/storage/SD/"))
        self.assertEqual(item["details"]["history"]["playtime"], "95")
        self.assertEqual(item["status"], "done")
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")
        self.assertNotIn(str(self.png), json.dumps(item))

    def test_real_mp4_full_partial_suffix_open_range_and_head(self):
        preview = register_media(self.run, self.id, "videos", self.mp4)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("Video must stream through a handle")):
            status, headers, body = self.request(preview["url"])
        self.assertEqual((status, body), (200, MP4))
        self.assertEqual(headers["Content-Type"], "video/mp4")
        for requested, expected, bounds in (("bytes=0-23", MP4[:24], "0-23"), ("bytes=40-", MP4[40:], "40-1519"), ("bytes=-16", MP4[-16:], "1504-1519")):
            status, headers, body = self.request(preview["url"], headers={"Range": requested})
            self.assertEqual((status, body), (206, expected))
            self.assertEqual(headers["Content-Range"], "bytes " + bounds + "/1520")
            self.assertEqual(int(headers["Content-Length"]), len(expected))
        status, headers, body = self.request(preview["url"], method="HEAD")
        self.assertEqual((status, body, int(headers["Content-Length"])), (200, b"", len(MP4)))
        status, headers, body = self.request(preview["url"], method="HEAD", headers={"Range": "bytes=0-23"})
        self.assertEqual((status, body, int(headers["Content-Length"])), (206, b"", 24))

    def test_unsatisfiable_and_multiple_ranges_return_416(self):
        preview = register_media(self.run, self.id, "videos", self.mp4)
        for value in ("bytes=99999-", "bytes=32-2", "bytes=-0", "bytes=0-1,4-5", "bytes=-", "items=0-1"):
            status, headers, _ = self.request(preview["url"], headers={"Range": value})
            self.assertEqual(status, 416, value)
            self.assertEqual(headers["Content-Range"], "bytes */1520")
        self.assertEqual(byte_range("bytes=-9999", 5), (0, 4))

    def test_registry_is_required_and_requests_never_open_arbitrary_paths(self):
        self.assertEqual(self.request("/api/runs/historical/media/" + "a" * 48)[0], 404)
        for suffix in (quote(str(self.png), safe=""), "../real.png", "%2e%2e%2freal.png", "state.sqlite", "file://test"):
            self.assertEqual(self.request("/api/runs/historical/media/" + suffix)[0], 404)
        invalid = self.root / "credentials.jpg"
        invalid.write_text("password=this-must-not-be-served", encoding="utf-8")
        with self.assertRaises(ValueError):
            register_media(self.run, self.id, "covers", invalid)
        svg = self.root / "unsafe.svg"
        svg.write_text("<svg><script>alert(1)</script></svg>")
        with self.assertRaises(ValueError):
            register_media(self.run, self.id, "covers", svg)
        with self.assertRaises(ValueError):
            register_media(self.run, self.id, "covers", self.png, sha256="0" * 64)

    def test_missing_or_replaced_registered_media_is_not_misreported_as_previewable(self):
        preview = register_media(self.run, self.id, "covers", self.png)
        self.png.unlink()
        self.assertEqual(self.request(preview["url"])[0], 404)
        job = refresh_preview_availability(self.run, get_job(self.run, self.id))
        self.assertFalse(job["media"]["covers"]["extras"]["preview"]["available"])
        self.assertEqual(job["media"]["covers"]["status"], "historical_receipt")
        self.png.write_bytes(PNG)
        self.assertEqual(self.request(preview["url"])[0], 404)
        register_media(self.run, self.id, "covers", self.png)
        self.assertEqual(self.request(preview["url"])[0], 200)

    def test_batch_registration_is_atomic_can_leave_jobs_unchanged_and_reuses_file_hash(self):
        import workbench_media
        upsert_job(self.run, "second", status="done")
        before = get_job(self.run, self.id)
        with patch.dict(workbench_media._HASH_CACHE, clear=True):
            with patch.object(workbench_media.hashlib, "sha256", wraps=hashlib.sha256) as hashing:
                previews = register_media_many(self.run, [{"job_id": self.id, "kind": "covers", "path": self.png}, {"job_id": "second", "kind": "covers", "path": self.png}], update_jobs=False)
            self.assertEqual(hashing.call_count, 3)  # one content hash plus two IDs
        self.assertEqual(get_job(self.run, self.id), before)
        self.assertEqual(self.request(previews[0]["url"])[0], 200)
        count = len(get_events(self.run, limit=500)["items"])
        with self.assertRaises(FileNotFoundError):
            register_media_many(self.run, [{"job_id": self.id, "kind": "screenshots", "path": self.png}, {"job_id": "absent", "kind": "covers", "path": self.png}])
        self.assertEqual(len(get_events(self.run, limit=500)["items"]), count)
        with self.assertRaises(ValueError):
            register_media_many(self.run, [{}] * 501)

    def test_reparse_redirect_is_rejected_even_after_registration(self):
        import workbench_media
        preview = register_media(self.run, self.id, "covers", self.png)
        real_lstat = Path.lstat
        def marked(path, *args, **kwargs):
            current = real_lstat(path, *args, **kwargs)
            if path == self.png:
                class Redirect:
                    st_mode = current.st_mode
                    st_file_attributes = 0x400
                return Redirect()
            return current
        with patch.object(Path, "lstat", marked):
            with self.assertRaises(ValueError):
                register_media(self.run, self.id, "covers", self.png)
            with self.assertRaises(FileNotFoundError):
                open_registered_media(self.run, preview["asset_id"])

    def test_existing_database_migrates_without_resetting_jobs_events_or_history(self):
        old = self.root / "runs" / "old-format"
        old.mkdir()
        database = sqlite3.connect(old / "state.sqlite")
        database.executescript("""CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE phases(phase TEXT PRIMARY KEY,status TEXT,message TEXT,completed INTEGER,total INTEGER,updated_at TEXT);
          CREATE TABLE jobs(id TEXT PRIMARY KEY,system TEXT,file TEXT,name TEXT,status TEXT,missing TEXT,unknown_fields TEXT,sources TEXT,media TEXT,video_kind TEXT,updated_at TEXT);
          CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,message TEXT,payload TEXT,created_at TEXT);""")
        for key, value in {"id": "old-format", "title": "旧任务", "created_at": "2026-01-01", "updated_at": "2026-01-01", "status": "completed", "phase": "verify"}.items():
            database.execute("INSERT INTO meta VALUES(?,?)", (key, json.dumps(value)))
        database.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("old.nds", "nds", "old.nds", "旧游戏", "done", "[]", "[]", "[]", "{}", "", "2026-01-01"))
        database.execute("INSERT INTO events(kind,message,payload,created_at) VALUES(?,?,?,?)", ("historical", "old-event", "{}", "2026-01-01"))
        database.commit(); database.close()
        self.assertEqual(get_job(old, "old.nds")["details"], {})
        upsert_job(old, "old.nds", details={"history": {"playcount": "9"}})
        self.assertEqual(get_job(old, "old.nds")["status"], "done")
        self.assertEqual(get_state(old)["run"]["status"], "completed")
        self.assertEqual(get_events(old)["items"][0]["message"], "old-event")
        with connect(old) as migrated:
            self.assertIn("details", {row[1] for row in migrated.execute("PRAGMA table_info(jobs)")})
            self.assertIn("remote_path", {row[1] for row in migrated.execute("PRAGMA table_info(media_assets)")})

    def test_registered_remote_preview_is_lazy_and_same_asset_id_becomes_cached(self):
        from preview_cache import bind_remote_profile
        remote = "/storage/SD/ES-DE/downloaded_media/dreamcast/covers/game.png"
        profile = bind_remote_profile(self.run, adb="explicit-fake-adb", serial="explicit-test-device",
                                      media_root="/storage/SD/ES-DE/downloaded_media",
                                      inventory=[{"path": remote, "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}])
        preview = register_remote_media(self.run, self.id, "covers", remote, profile, size=len(PNG))
        self.assertTrue(preview["available"])
        self.assertFalse(preview["cached"])
        self.assertTrue(preview["needs_device"])
        with patch("preview_cache.materialize", return_value=self.png) as loader:
            status, _, body = self.request("/api/runs/historical/jobs/" + quote(self.id, safe=""))
            self.assertEqual(status, 200)
            self.assertEqual(loader.call_count, 0)
            self.assertEqual(json.loads(body)["job"]["media"]["covers"]["extras"]["preview"]["cache_state"], "pending")
            self.assertEqual(self.request(preview["url"])[2], PNG)
            self.assertEqual(self.request(preview["url"])[2], PNG)
            self.assertEqual(loader.call_count, 1)
        current = get_job(self.run, self.id)["media"]["covers"]["extras"]["preview"]
        self.assertEqual(current["asset_id"], preview["asset_id"])
        self.assertEqual(current["cache_state"], "ready")
        self.assertEqual(current["size"], len(PNG))
        self.assertEqual(current["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertTrue(current["cached"])
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")

    def test_remote_disconnection_returns_clear_503_without_selecting_any_other_device(self):
        from preview_cache import bind_remote_profile, PreviewUnavailable
        remote = "/storage/SD/ES-DE/downloaded_media/dreamcast/covers/game.png"
        profile = bind_remote_profile(self.run, adb="explicit-fake-adb", serial="explicit-test-device",
                                      media_root="/storage/SD/ES-DE/downloaded_media", inventory=[{"path": remote, "size": None}])
        previews = register_remote_media_many(self.run, [{"job_id": self.id, "kind": "covers", "remote_path": remote, "profile_id": profile, "size": None}])
        self.assertIsNone(previews[0]["size"])
        with patch("preview_cache.materialize", side_effect=PreviewUnavailable("device_disconnected", "已指定的设备未连接，请重新连接后重试")):
            status, _, body = self.request(previews[0]["url"], headers={"Range": "bytes=0-0"})
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["code"], "device_disconnected")
            self.assertIn("未连接", json.loads(body)["error"])
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")
        with self.assertRaises(PreviewUnavailable):
            register_remote_media(self.run, self.id, "covers", "/storage/SD/ES-DE/downloaded_media/dreamcast/covers/not-in-inventory.png", profile)
        self.assertEqual(get_job(self.run, self.id)["media"]["covers"]["extras"]["preview"]["cache_state"], "pending")

    def test_cross_suffix_raster_is_served_by_true_mime_without_renaming_source(self):
        png_named_jpg = self.root / "scanned-wrong-extension.jpg"
        jpeg_named_png = self.root / "other-wrong-extension.png"
        png_named_jpg.write_bytes(PNG)
        jpeg_named_png.write_bytes(JPEG)
        for source, data, mime in ((png_named_jpg, PNG, "image/png"), (jpeg_named_png, JPEG, "image/jpeg")):
            preview = register_media(self.run, self.id, "covers", source)
            status, headers, body = self.request(preview["url"])
            self.assertEqual((status, headers["Content-Type"], body), (200, mime, data))
            self.assertEqual(source.read_bytes(), data)
        image_named_video = self.root / "must-not-be-a-video.mp4"
        image_named_video.write_bytes(PNG)
        with self.assertRaises(ValueError):
            register_media(self.run, self.id, "videos", image_named_video)
        disguised_html = self.root / "must-not-be-raster.jpg"
        disguised_html.write_bytes(b"<html><script>alert(1)</script></html>")
        with self.assertRaises(ValueError):
            register_media(self.run, self.id, "covers", disguised_html)

    def test_transient_startup_io_error_reopens_once_but_persistent_errors_surface(self):
        import workbench_store
        real_connect = sqlite3.connect
        opened, remaining = [], [1]
        class StartupProxy:
            def __init__(self, handle):
                self.handle, self.closed = handle, False
            @property
            def row_factory(self):
                return self.handle.row_factory
            @row_factory.setter
            def row_factory(self, value):
                self.handle.row_factory = value
            def execute(self, sql, *args):
                if sql == "SELECT 1 FROM sqlite_master LIMIT 1" and remaining[0]:
                    remaining[0] -= 1
                    raise sqlite3.OperationalError("disk I/O error")
                return self.handle.execute(sql, *args)
            def __getattr__(self, name):
                return getattr(self.handle, name)
            def __enter__(self):
                self.handle.__enter__()
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def close(self):
                self.closed = True
                self.handle.close()
        def factory(*args, **kwargs):
            proxy = StartupProxy(real_connect(*args, **kwargs))
            opened.append(proxy)
            return proxy
        with patch.object(workbench_store.sqlite3, "connect", side_effect=factory) as connections, patch.object(workbench_store.time, "sleep") as pause:
            self.assertEqual(get_job(self.run, self.id)["status"], "done")
            self.assertEqual(connections.call_count, 2)
            self.assertTrue(all(db.closed for db in opened))
            pause.assert_called_once_with(0.15)
        remaining[0] = 2
        opened.clear()
        with patch.object(workbench_store.sqlite3, "connect", side_effect=factory) as connections, patch.object(workbench_store.time, "sleep"):
            with self.assertRaises(sqlite3.OperationalError):
                get_job(self.run, self.id)
            self.assertEqual(connections.call_count, 2)
            self.assertTrue(all(db.closed for db in opened))

    def test_hot_read_connections_do_not_repeat_schema_ddl(self):
        import workbench_store
        statements = []
        real_connect = sqlite3.connect
        def observed(*args, **kwargs):
            db = real_connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch.object(workbench_store.sqlite3, "connect", side_effect=observed):
            self.assertEqual(get_job(self.run, self.id)["status"], "done")
            self.assertEqual(get_state(self.run)["run"]["status"], "completed")
        for statement in statements:
            self.assertFalse(statement.strip().upper().startswith(("CREATE", "ALTER", "PRAGMA TABLE_INFO")), statement)

    def test_all_same_kind_scanned_candidates_survive_separate_registration_batches(self):
        another = self.root / "second-screenshot.png"
        another.write_bytes(PNG)
        upsert_job(self.run, self.id, media={"screenshots": [{"path": str(self.png), "size": len(PNG), "scan_note": "first-original"},
                                                            {"path": str(another), "size": len(PNG), "scan_note": "second-original"}]})
        first = register_media(self.run, self.id, "screenshots", self.png)
        second = register_media(self.run, self.id, "screenshots", another)
        item = get_job(self.run, self.id)["media"]["screenshots"]
        self.assertEqual(item["extras"]["preview"]["asset_id"], first["asset_id"])
        self.assertEqual(len(item["extras"]["candidates"]), 2)
        self.assertEqual([candidate["scan_note"] for candidate in item["extras"]["candidates"]], ["first-original", "second-original"])
        self.assertEqual([candidate["preview"]["asset_id"] for candidate in item["extras"]["candidates"]], [first["asset_id"], second["asset_id"]])
        for candidate in item["extras"]["candidates"]:
            self.assertEqual(self.request(candidate["preview"]["url"])[2], PNG)
        register_media(self.run, self.id, "screenshots", another)
        self.assertEqual(len(get_job(self.run, self.id)["media"]["screenshots"]["extras"]["candidates"]), 2)
        another.unlink()
        refreshed = refresh_preview_availability(self.run, get_job(self.run, self.id))["media"]["screenshots"]
        self.assertTrue(refreshed["extras"]["preview"]["available"])
        self.assertFalse(refreshed["extras"]["candidates"][1]["preview"]["available"])

    def test_caching_secondary_remote_candidate_preserves_primary_asset_and_all_paths(self):
        from preview_cache import bind_remote_profile
        first_path = "/storage/SD/ES-DE/downloaded_media/dreamcast/screenshots/first.png"
        second_path = "/storage/SD/ES-DE/downloaded_media/dreamcast/screenshots/second.png"
        paths = [first_path, second_path]
        profile = bind_remote_profile(self.run, adb="fake-adb", serial="explicit-test-device", media_root="/storage/SD/ES-DE/downloaded_media",
                                      inventory=[{"path": path, "size": len(PNG)} for path in paths])
        upsert_job(self.run, self.id, media={"screenshots": [{"path": path, "size": len(PNG)} for path in paths]})
        previews = register_remote_media_many(self.run, [{"job_id": self.id, "kind": "screenshots", "remote_path": path, "profile_id": profile, "size": len(PNG)} for path in paths])
        with patch("preview_cache.materialize", return_value=self.png):
            self.assertEqual(self.request(previews[1]["url"])[2], PNG)
        item = get_job(self.run, self.id)["media"]["screenshots"]
        self.assertEqual(item["extras"]["preview"]["asset_id"], previews[0]["asset_id"])
        self.assertFalse(item["extras"]["preview"]["cached"])
        self.assertTrue(item["extras"]["candidates"][1]["preview"]["cached"])
        self.assertEqual([candidate["path"] for candidate in item["extras"]["candidates"]], paths)
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
