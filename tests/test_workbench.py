"""Exercise persistence, real counts, redaction, HTTP isolation and live SSE delivery."""
import http.client
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from workbench import build_server, is_loopback
from workbench_store import append_event, emit_update, get_events, get_jobs, get_state, init_run, redact, upsert_job, upsert_jobs


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.run = Path(self.temporary.name) / "run-01"
        init_run(self.run, "工作台隔离测试", connection={"status": "connected", "device_serial": "test-device"})

    def tearDown(self):
        self.temporary.cleanup()

    def test_real_counts_per_phase_and_terminal_state_persist_in_new_process(self):
        emit_update(self.run, phase="scan", status="running", completed=2, total=7)
        emit_update(self.run, phase="scan", phase_status="done", completed=7)
        emit_update(self.run, phase="download", status="error", completed=1, total=3, message="Download failed")
        state = get_state(self.run)
        self.assertEqual(state["run"]["status"], "error")
        self.assertEqual([(p["phase"], p["completed"], p["total"]) for p in state["phases"]], [("scan", 7, 7), ("download", 1, 3)])
        result = subprocess.run([sys.executable, str(SCRIPTS / "workbench.py"), "state", "--run", str(self.run)], capture_output=True, text=True, encoding="utf-8", check=True)
        restored = json.loads(result.stdout)
        self.assertEqual(restored, state)
        init_run(self.run, "Attempted reset")
        self.assertEqual(get_state(self.run), state)
        emit_update(self.run, phase="verify", status="completed", completed=3, total=3)
        self.assertEqual(get_state(self.run)["run"]["status"], "completed")

    def test_invalid_counts_cannot_commit_or_replace_terminal_state(self):
        emit_update(self.run, phase="scan", status="blocked", completed=1, total=2)
        before = get_state(self.run)
        with self.assertRaises(ValueError):
            emit_update(self.run, status="running", completed=3)
        with self.assertRaises(ValueError):
            emit_update(self.run, total=-1)
        self.assertEqual(get_state(self.run), before)

    def test_partial_job_updates_keep_evidence_and_summary(self):
        upsert_job(self.run, "nds/a.nds", system="nds", name="Game A", status="running", unknown=["developer"], missing=["covers"], media={"screenshots": {"status": "verified"}}, sources=[{"url": "https://example.test/game"}], video_kind="screenshot_preview")
        upsert_job(self.run, "nds/a.nds", status="done", missing=[])
        upsert_job(self.run, "gba/b.gba", system="gba", status="error", video_kind="gameplay_video")
        job = get_jobs(self.run, system="nds")["items"][0]
        self.assertEqual(job["media"]["screenshots"]["status"], "verified")
        self.assertEqual(job["unknown"], ["developer"])
        self.assertEqual(job["missing"], [])
        summary = get_state(self.run)["summary"]
        self.assertEqual((summary["total"], summary["done"], summary["error"], summary["unknown"], summary["missing_media"]), (2, 1, 1, 1, 0))
        self.assertEqual(summary["video_kinds"], {"screenshot_preview": 1, "gameplay_video": 1})
        upsert_job(self.run, "third", status="completed", issues=["missing_name"])
        upsert_job(self.run, "fourth", status="failed")
        self.assertEqual(get_jobs(self.run, status="done")["total"], 2)
        self.assertEqual(get_jobs(self.run, status="error")["total"], 2)
        self.assertEqual(get_state(self.run)["summary"]["issues"], 1)

    def test_credentials_removed_before_sqlite_and_config_persistence(self):
        init_run(Path(self.temporary.name) / "private", title="password=hunter2", connection={"token": "private-init"})
        emit_update(self.run, phase="source", message="ssid=private-ssid", connection={"status": "connected", "SSPASSWORD": "network-secret", "nested": {"api_key": "key-secret"}})
        source = "https://alice:basic-secret@example.test/get?devid=private-id&devpassword=dev-secret&%74oken=private-token&game=42"
        upsert_job(self.run, "safe-id", sources=[{"url": source, "password": "private-password"}])
        append_event(self.run, "source", "password=inline-secret", {"authorization": "Bearer auth-secret"})
        db = sqlite3.connect(self.run / "state.sqlite")
        try:
            serialized = "\n".join(str(row) for table in ("meta", "jobs", "events") for row in db.execute("SELECT * FROM " + table))
        finally:
            db.close()
        for secret in ("private-ssid", "network-secret", "key-secret", "basic-secret", "private-id", "dev-secret", "private-token", "private-password", "inline-secret", "auth-secret"):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("hunter2", (Path(self.temporary.name) / "private" / "config.json").read_text(encoding="utf-8"))
        self.assertIn("game=42", serialized)
        self.assertEqual(redact({"ssid": "x", "devid": "y", "safe": ["token=z"]}), {"ssid": "[REDACTED]", "devid": "[REDACTED]", "safe": ["token=[REDACTED]"]})
        for message in ('ssid="Private Wifi Name"', '{"password":"a long private password"}', 'Authorization: Bearer sensitive-token'):
            output = redact(message)
            for secret in ("Private", "long", "sensitive-token", "Bearer"):
                self.assertNotIn(secret, output)

    def test_concurrent_writers_and_filtered_pagination(self):
        def add(index):
            upsert_job(self.run, "job-" + str(index).zfill(2), system="nds" if index % 2 else "gba", name="Game %" if index == 5 else "Game " + str(index), status="done")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add, range(24)))
        result = get_jobs(self.run, system="nds", status="done", page=2, limit=5)
        self.assertEqual((result["total"], len(result["items"]), result["page"]), (12, 5, 2))
        self.assertEqual(get_jobs(self.run, search="%")["total"], 1)
        self.assertEqual(get_state(self.run)["summary"]["total"], 24)
        self.assertEqual(get_events(self.run, limit=500)["last_id"], 25)
        self.assertEqual([x["id"] for x in get_events(self.run, tail=True, limit=2)["items"]], [24, 25])

    def test_batch_validation_matches_single_and_rolls_back_failed_batch(self):
        ids = upsert_jobs(self.run, [{"id": "a", "status": "done", "sources": [{"password": "private"}]}, {"id": "b", "status": "error"}])
        self.assertEqual(ids, [2, 3])
        self.assertNotIn("private", json.dumps(get_jobs(self.run)))
        before = get_state(self.run)
        with self.assertRaises(ValueError):
            upsert_jobs(self.run, [{"id": "new", "status": "pending"}, {"id": "invalid", "status": "invented"}])
        self.assertEqual(get_state(self.run), before)
        with self.assertRaises(ValueError):
            upsert_jobs(self.run, [{"id": str(index)} for index in range(201)])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "runs"
        self.run = self.root / "run-01"
        self.assets = Path(self.temporary.name) / "assets"
        self.assets.mkdir()
        (self.assets / "index.html").write_text("<!doctype html><title>Workbench</title>", encoding="utf-8")
        (self.assets / "style.css").write_text("body{color:black}", encoding="utf-8")
        init_run(self.run)
        self.server = None
        self.start()

    def start(self, token=None, root=None):
        self.server = build_server(root or self.root, self.assets, host="127.0.0.1", port=0, access_token=token, heartbeat_interval=.1, poll_interval=.02)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.server = None

    def tearDown(self):
        self.stop()
        self.temporary.cleanup()

    def request(self, path, headers=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        status, body = response.status, response.read()
        connection.close()
        return status, body

    def test_api_jobs_events_and_no_files_outside_assets_or_root(self):
        upsert_job(self.run, "one", name="测试", system="nds", status="done")
        status, body = self.request("/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["items"][0]["id"], "run-01")
        status, body = self.request("/api/runs/run-01/jobs?system=nds&page=1&limit=1")
        self.assertEqual((status, json.loads(body)["total"]), (200, 1))
        status, body = self.request("/api/runs/run-01/events?tail=1&limit=1")
        self.assertEqual((status, json.loads(body)["items"][0]["kind"]), (200, "job"))
        for path in ("/../runs/run-01/state.sqlite", "/%2e%2e/runs/run-01/state.sqlite", "/api/runs/%2e%2e", "/api/runs/run-01/config.json", "/api/runs/run-01/jobs/other", "/state.sqlite"):
            self.assertEqual(self.request(path)[0], 404, path)
        self.assertEqual(self.request("/api/runs/run-01/jobs?page=bad")[0], 400)
        self.assertEqual(self.request("/api/runs", method="POST")[0], 405)
        self.assertEqual(self.request("/api/runs", {"Host": "attacker.example"})[0], 403)

    def test_lan_access_token_covers_index_static_and_every_api(self):
        self.stop()
        self.start(token="local-test-token")
        for path in ("/", "/style.css", "/api/runs", "/api/runs/run-01", "/api/runs/run-01/jobs", "/api/runs/run-01/events", "/api/runs/run-01/stream"):
            self.assertEqual(self.request(path)[0], 401, path)
        self.assertEqual(self.request("/?token=wrong")[0], 401)
        self.assertEqual(self.request("/?token=local-test-token")[0], 200)
        self.assertEqual(self.request("/style.css", {"X-Workbench-Token": "local-test-token"})[0], 200)
        self.assertEqual(self.request("/api/runs?token=local-test-token")[0], 200)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        connection.request("GET", "/?token=local-test-token")
        response = connection.getresponse()
        cookie = response.getheader("Set-Cookie")
        response.read()
        connection.close()
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.request("/", {"Cookie": cookie.split(";", 1)[0]})[0], 200)
        health_status, health_body = self.request("/api/health", {"Cookie": cookie.split(";", 1)[0]})
        self.assertEqual((health_status, json.loads(health_body)["service"]), (200, "es-de-resource-workbench"))
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertTrue(is_loopback("::1"))
        self.assertFalse(is_loopback("0.0.0.0"))
        lan = build_server(self.root, self.assets, host="0.0.0.0", port=0)
        try:
            self.assertGreaterEqual(len(lan.access_token), 40)
        finally:
            lan.server_close()

    def test_sse_snapshot_live_error_and_completion_with_resume(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        connection.request("GET", "/api/runs/run-01/stream?after=1")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")

        def read_event():
            lines = []
            while True:
                line = response.fp.readline().decode("utf-8").rstrip("\r\n")
                if not line:
                    if lines:
                        return lines
                elif not line.startswith(":"):
                    lines.append(line)

        snapshot = read_event()
        self.assertIn("event: snapshot", snapshot)
        event_id = emit_update(self.run, phase="download", status="error", message="Network unavailable", completed=0, total=2)
        update = read_event()
        self.assertIn("event: update", update)
        self.assertIn("id: " + str(event_id), update)
        payload = json.loads(next(line[6:] for line in update if line.startswith("data: ")))
        self.assertEqual(payload["payload"]["status"], "error")
        terminal_id = emit_update(self.run, phase="verify", status="completed", completed=2, total=2)
        terminal = read_event()
        self.assertIn("id: " + str(terminal_id), terminal)
        self.assertEqual(json.loads(next(line[6:] for line in terminal if line.startswith("data: ")))["payload"]["status"], "completed")
        response.close()
        connection.close()
        status, body = self.request("/api/runs/run-01/events?after=" + str(event_id))
        self.assertEqual((status, json.loads(body)["last_id"]), (200, terminal_id))

    def test_restart_and_single_run_root(self):
        emit_update(self.run, phase="verify", status="completed", completed=4, total=4)
        self.stop()
        self.start(root=self.run)
        status, body = self.request("/api/runs")
        self.assertEqual((status, json.loads(body)["total"]), (200, 1))
        status, body = self.request("/api/runs/run-01")
        self.assertEqual((status, json.loads(body)["run"]["status"]), (200, "completed"))
        self.assertEqual(self.request("/api/runs/other")[0], 404)


if __name__ == "__main__":
    unittest.main()
