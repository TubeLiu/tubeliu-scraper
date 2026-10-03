import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import traceback
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import media
import screenscraper as provider
from workbench_store import emit_update, get_events, get_state, init_run


class FakeResponse(io.BytesIO):
    def __init__(self, body, headers=None, url="https://api.screenscraper.fr/api2/jeuInfos.php"):
        super().__init__(body)
        self.headers = headers or {}
        self.url = url
    def geturl(self):
        return self.url


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.image = self.root / "real.png"
        Image, _, _ = media.pillow()
        Image.new("RGB", (64, 48), "blue").save(self.image)
    def tearDown(self):
        self.temporary.cleanup()

    def test_real_image_decode_corrupt_image_and_machine_receipt(self):
        report = media.image_info(self.image)
        self.assertEqual((report["width"], report["height"], report["technical_verified"]), (64, 48, True))
        broken = self.root / "broken.png"
        broken.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            media.image_info(broken)
        receipt = media.check_files([{ "path": str(self.image), "kind": "image"}])
        self.assertEqual((receipt["status"], receipt["checked_files"], receipt["checks"]["identity"]), ("pass", 1, False))
        self.assertTrue(receipt["visual_review_required"])
        failed = media.check_files([str(broken)])
        self.assertEqual(failed["status"], "fail")

    def test_video_limits_reject_bad_codec_duration_size_and_dimensions(self):
        probe = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 960, "height": 720}], "format": {"duration": "30", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}
        self.assertTrue(media.validate_probe(probe, 6000000)["technical_verified"])
        invalid = json.loads(json.dumps(probe))
        invalid["streams"][0].update(codec_name="hevc", height=1080)
        invalid["format"]["duration"] = "31"
        report = media.validate_probe(invalid, 6000001)
        self.assertEqual(set(report["errors"]), {"codec_h264", "dimensions", "duration", "bytes"})
        invalid["format"]["duration"] = "nan"
        self.assertFalse(media.validate_probe(invalid, 1)["technical_verified"])

    def test_missing_dependency_is_blocked_and_not_marked_complete(self):
        video = self.root / "unverified.mp4"
        video.write_bytes(b"placeholder")
        run = self.root / "run"
        with patch.dict("os.environ", {}, clear=True), patch("media.shutil.which", return_value=None), contextlib.redirect_stderr(io.StringIO()):
            code = media.main(["inspect", "--kind", "video", "--file", str(video), "--run", str(run)])
        self.assertEqual(code, 3)
        self.assertEqual(get_state(run)["run"]["status"], "blocked")

    def test_failed_manifest_and_missing_dependency_replace_previous_pass_receipt(self):
        receipt = self.root / "media_qa.json"
        good_manifest = self.root / "good.json"
        good_manifest.write_text(json.dumps({"files": [{"path": "real.png", "kind": "image"}]}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(media.main(["check", "--manifest", str(good_manifest), "--receipt", str(receipt)]), 0)
        self.assertEqual(json.loads(receipt.read_text())["status"], "pass")
        broken_manifest = self.root / "broken.json"
        broken_manifest.write_text("{invalid json", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(media.main(["check", "--manifest", str(broken_manifest), "--receipt", str(receipt)]), 2)
        failed = json.loads(receipt.read_text())
        self.assertEqual((failed["status"], failed["checked_files"], failed["checks"]["decode"]), ("fail", 0, False))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(media.main(["check", "--manifest", str(good_manifest), "--receipt", str(receipt)]), 0)
        video = self.root / "unknown.mp4"
        video.write_bytes(b"unverified video")
        video_manifest = self.root / "video.json"
        video_manifest.write_text(json.dumps({"files": [{"path": "unknown.mp4", "kind": "video"}]}), encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True), patch("media.shutil.which", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(media.main(["check", "--manifest", str(video_manifest), "--receipt", str(receipt)]), 3)
        self.assertEqual(json.loads(receipt.read_text())["status"], "blocked")

    def test_preview_label_identity_evidence_and_output_boundaries(self):
        frame = self.root / "frame.png"
        media.preview_frame(self.image, "", "screenshot_preview", frame)
        self.assertEqual(media.image_info(frame)["height"], 720)
        with self.assertRaises(ValueError):
            media.destination(self.root / "out", "../outside.mp4")
        with self.assertRaises(ValueError):
            media.publish(None, None, "videos", {"technical_verified": True}, identity_confirmed=True)
        report = media.publish(None, None, "videos", {"technical_verified": True}, video_kind="screenshot_preview")
        self.assertFalse(report["identity"]["confirmed"])
        self.assertEqual(report["video_kind"], "screenshot_preview")
        self.assertEqual(report["status"], "needs_identity_review")
        with contextlib.redirect_stderr(io.StringIO()):
            code = media.main(["inspect", "--kind", "video", "--file", str(self.image), "--video-kind", "gameplay_video"])
        self.assertEqual(code, 2)

    @unittest.skipUnless(os.environ.get("ESDE_TEST_FFMPEG") and os.environ.get("ESDE_TEST_FFPROBE"), "Set ESDE_TEST_FFMPEG/FFPROBE to exercise installed binaries")
    def test_real_ffmpeg_preview_transcode_ffprobe_and_decode(self):
        ffmpeg, ffprobe = os.environ["ESDE_TEST_FFMPEG"], os.environ["ESDE_TEST_FFPROBE"]
        original_hash = media.sha256(self.image)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = media.main(["preview", "--image", str(self.image), "--preview-kind", "screenshot_preview", "--out", str(self.root / "outputs"), "--relative", "preview.mp4", "--duration", "1", "--ffmpeg", ffmpeg, "--ffprobe", ffprobe])
        self.assertEqual(code, 0)
        preview = json.loads(output.getvalue())
        self.assertEqual(preview["video_kind"], "screenshot_preview")
        self.assertFalse(preview["identity"]["confirmed"])
        self.assertIn("no gameplay footage", preview["label"])
        transcoded = media.encode(preview["path"], self.root / "outputs", "transcoded.mp4", duration=1, ffmpeg=ffmpeg, ffprobe=ffprobe)
        self.assertEqual(transcoded["codec"], "h264")
        self.assertTrue(transcoded["technical_verified"])
        qa = media.check_files([{"path": transcoded["path"], "kind": "video"}], ffmpeg=ffmpeg, ffprobe=ffprobe)
        self.assertEqual(qa["status"], "pass")
        self.assertEqual(qa["files"][0]["verification"], "ffprobe_and_ffmpeg_frame_decode")
        self.assertEqual(media.sha256(self.image), original_hash)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = {"SCREENSCRAPER_DEVID": "fake-developer", "SCREENSCRAPER_DEVPASSWORD": "fake-developer-pass", "SCREENSCRAPER_SSID": "fake-user", "SCREENSCRAPER_SSPASSWORD": "fake-user-pass"}
    def tearDown(self):
        self.temporary.cleanup()

    def test_user_credentials_do_not_replace_required_developer_pair(self):
        with self.assertRaises(provider.Blocked):
            provider.credentials({"SCREENSCRAPER_SSID": "user", "SCREENSCRAPER_SSPASSWORD": "password"})
        with self.assertRaises(provider.Blocked):
            provider.credentials({"SCREENSCRAPER_DEVID": "developer", "SCREENSCRAPER_DEVPASSWORD": "password", "SCREENSCRAPER_SSID": "partial-user"})

    def test_missing_credentials_guidance_is_local_complete_and_can_be_skipped(self):
        with patch.object(provider, "default_open", side_effect=AssertionError("must not request network")), patch.object(provider.credential_store, "load_credentials", side_effect=AssertionError("explicit environment must not read saved credentials")):
            report = provider.credential_check({})
            with self.assertRaises(provider.MissingCredentials) as caught:
                provider.query(15, search_name="Game", env={})
        self.assertEqual(report["status"], "needs_credentials")
        self.assertEqual(set(report["missing_environment_variables"]), set(provider.DEVELOPER_FIELDS))
        self.assertEqual(report["developer_application_url"], "https://www.screenscraper.fr/forumsujets.php?frub=12&numpage=0")
        self.assertEqual(report["documentation_url"], "https://www.screenscraper.fr/webapi2.php")
        self.assertFalse(report["network_checked"])
        self.assertFalse(report["credential_verified"])
        self.assertTrue(report["can_skip"])
        self.assertFalse(report["blocks_entire_workflow"])
        self.assertIn("普通用户账号不能替代", report["prompt"])
        self.assertIn("批量匹配", report["limits_without_credentials"][0])
        self.assertIn("备份、校验并回写到设备", report["available_without_credentials"])
        self.assertIn("自动生成", report["workbench_access"])
        self.assertEqual(caught.exception.report, report)

    def test_ready_check_reports_configuration_only_and_never_values(self):
        with patch.object(provider, "default_open", side_effect=AssertionError("must not request network")):
            report = provider.credential_check(self.env)
        self.assertEqual(report["status"], "ready")
        self.assertTrue(report["ready_for_api"])
        self.assertTrue(report["developer_configured"])
        self.assertTrue(report["user_configured"])
        self.assertEqual(report["missing_environment_variables"], [])
        self.assertFalse(report["credential_verified"])
        for value in self.env.values():
            self.assertNotIn(value, json.dumps(report))
        partial = {**self.env}
        partial.pop("SCREENSCRAPER_SSPASSWORD")
        report = provider.credential_check(partial)
        self.assertEqual(report["status"], "needs_credentials")
        self.assertIn("SCREENSCRAPER_SSPASSWORD", report["missing_environment_variables"])

    def test_check_aliases_are_nonblocking_and_make_no_network_request(self):
        report = provider.credential_check({})
        for command in ("check", "status", "doctor"):
            with patch.object(provider, "credential_check", return_value=report), patch.object(provider, "default_open", side_effect=AssertionError("must not request network")), contextlib.redirect_stdout(io.StringIO()) as output:
                code = provider.main([command])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "needs_credentials")

    def test_missing_optional_provider_keeps_existing_run_status_and_records_guidance(self):
        run = self.root / "run"
        init_run(run)
        emit_update(run, phase="verify", status="completed", completed=1, total=1, message="Local resources verified")
        status_report = provider.credential_check({})
        commands = [
            ["query", "--system-id", "15", "--search-name", "Game", "--run", str(run)],
            ["download", "--url", "https://api.screenscraper.fr/api2/mediaJeu.php?devid=[REDACTED]&devpassword=[REDACTED]", "--out", str(self.root / "out"), "--relative", "game.png", "--run", str(run)],
        ]
        for command in commands:
            with patch.object(provider.credential_store, "resolve_credentials", return_value={}), patch.object(provider, "credential_check", return_value=status_report), patch.object(provider, "default_open", side_effect=AssertionError("must not request network")), contextlib.redirect_stderr(io.StringIO()) as error:
                code = provider.main(command)
            self.assertEqual(code, 3)
            state = get_state(run)
            self.assertEqual((state["run"]["status"], state["run"]["phase"]), ("completed", "verify"))
            self.assertEqual(json.loads(error.getvalue())["credential_guidance"]["status"], "needs_credentials")
        self.assertFalse((self.root / "out").exists())
        events = get_events(run)["items"]
        self.assertEqual(events[-1]["kind"], "provider_unavailable")
        self.assertFalse(events[-1]["payload"]["blocks_entire_workflow"])

    def test_configure_reads_stdin_not_arguments_and_outputs_no_values(self):
        report = provider.credential_check(self.env)
        with patch("sys.stdin", io.StringIO(json.dumps(self.env))), patch.object(provider.credential_store, "save_credentials") as save, patch.object(provider, "credential_check", return_value=report), contextlib.redirect_stdout(io.StringIO()) as output:
            code = provider.main(["configure", "--stdin"])
        self.assertEqual(code, 0)
        save.assert_called_once_with(self.env)
        result = json.loads(output.getvalue())
        self.assertIn("后续自动复用", result["message"])
        for value in self.env.values():
            self.assertNotIn(value, output.getvalue())
        with patch("sys.stdin", io.StringIO("invalid-secret-json")), patch.object(provider.credential_store, "save_credentials") as save, contextlib.redirect_stderr(io.StringIO()) as error:
            code = provider.main(["configure", "--stdin"])
        self.assertEqual(code, 2)
        save.assert_not_called()
        self.assertNotIn("invalid-secret-json", error.getvalue())

    def test_configure_requires_explicit_stdin_or_hidden_interactive_input(self):
        with patch("sys.stdin", io.StringIO("")), patch.object(provider.getpass, "getpass") as hidden_input, patch.object(provider.credential_store, "save_credentials") as save, contextlib.redirect_stderr(io.StringIO()) as error:
            code = provider.main(["configure"])
        self.assertEqual(code, 2)
        hidden_input.assert_not_called()
        save.assert_not_called()
        self.assertIn("configure --stdin", error.getvalue())
        report = provider.credential_check(self.env)
        with patch("sys.stdin.isatty", return_value=True), patch.object(provider.getpass, "getpass", side_effect=[self.env[field] for field in (*provider.DEVELOPER_FIELDS, *provider.USER_FIELDS)]), patch.object(provider.credential_store, "save_credentials") as save, patch.object(provider, "credential_check", return_value=report):
            provider.configure_credentials()
        save.assert_called_once_with(self.env)

    def test_forget_removes_local_store_and_warns_about_environment_override(self):
        report = provider.credential_check({})
        with patch.object(provider.credential_store, "forget_credentials") as forget, patch.object(provider, "credential_check", return_value=report), contextlib.redirect_stdout(io.StringIO()) as output:
            code = provider.main(["forget"])
        self.assertEqual(code, 0)
        forget.assert_called_once_with()
        self.assertIn("环境变量", json.loads(output.getvalue())["message"])

    def test_official_hash_name_size_params_and_candidate_chinese_evidence(self):
        captured = []
        game = {"id": "42", "nom": "Game", "systeme": {"id": "15"}, "noms": [{"region": "cn", "text": "测试游戏"}], "synopsis": [{"langue": "zh", "text": "中文说明"}], "medias": [{"type": "video", "url": "https://api.screenscraper.fr/api2/mediaVideoJeu.php?devid=secret-value&devpassword=secret-pass"}]}
        def fake_open(url, timeout):
            captured.append(parse_qs(urlsplit(url).query))
            return FakeResponse(json.dumps({"response": {"jeu": game}}).encode())
        result = provider.query(15, {"md5": "a" * 32, "romnom": "game.nds", "romtaille": 123, "romtype": "rom"}, env=self.env, opener=fake_open)
        self.assertEqual(captured[0]["devid"], ["fake-developer"])
        self.assertEqual(captured[0]["ssid"], ["fake-user"])
        self.assertEqual(captured[0]["romtaille"], ["123"])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["description_zh"], "中文说明")
        self.assertFalse(candidate["identity_confirmed"])
        self.assertTrue(candidate["review_required"])
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertNotIn("secret-pass", json.dumps(result))
        with self.assertRaises(ValueError):
            provider.query(15, {"romnom": "game.nds", "romtaille": 123}, env=self.env, opener=fake_open)

    def test_fake_http_download_bounded_atomic_and_provenance_redacted(self):
        def fake_open(url, timeout):
            return FakeResponse(b"fake media bytes", {"Content-Type": "image/png"}, url=url)
        url = "https://api.screenscraper.fr/api2/mediaJeu.php?devid=[REDACTED]&devpassword=[REDACTED]&ssid=[REDACTED]&sspassword=[REDACTED]&jeuid=42"
        result = provider.download(url, self.root / "out", "selected.png", env=self.env, opener=fake_open)
        self.assertEqual(result["sha256"], hashlib.sha256(b"fake media bytes").hexdigest())
        serialized = (self.root / "out" / "selected.png.provenance.json").read_text()
        self.assertNotIn("fake-developer", serialized)
        self.assertFalse(result["identity_confirmed"])
        self.assertFalse(result["technical_verified"])
        with self.assertRaises(ValueError):
            provider.download(url, self.root / "out", "../escape.png", env=self.env, opener=fake_open)
        with self.assertRaises(ValueError):
            provider.download(url, self.root / "out", "too-large.png", env=self.env, opener=fake_open, max_bytes=2)
        self.assertFalse((self.root / "out" / "too-large.png").exists())
        for unsafe in ("http://api.screenscraper.fr/media", "https://127.0.0.1/private", "https://screenscraper.fr.evil.test/media", "https://user:pass@api.screenscraper.fr/media"):
            with self.assertRaises(ValueError):
                provider.safe_url(unsafe)

    def test_response_limit_and_text_error_are_never_accepted_as_media(self):
        with self.assertRaises(ValueError):
            provider.read_bounded(FakeResponse(b"abc", {"Content-Length": "999"}), 3)
        def fake_open(url, timeout):
            return FakeResponse(b"NOMEDIA", {"Content-Type": "text/plain"}, url=url)
        with self.assertRaises(ValueError):
            provider.download("https://api.screenscraper.fr/media.png", self.root, "bad.png", opener=fake_open)
        self.assertFalse((self.root / "bad.png").exists())

    def test_provider_error_urls_and_invalid_json_do_not_escape(self):
        def unavailable(url, timeout):
            raise urllib.error.URLError(url)
        with self.assertRaises(provider.Blocked) as caught:
            provider.query(15, search_name="Game", env=self.env, opener=unavailable)
        self.assertNotIn("fake-developer", str(caught.exception))
        self.assertNotIn("fake-user", str(caught.exception))
        formatted = "".join(traceback.format_exception(caught.exception))
        for value in self.env.values():
            self.assertNotIn(value, formatted)
        self.assertNotIn("jeuRecherche.php?", formatted)
        with self.assertRaises(provider.Blocked) as caught:
            provider.download("https://api.screenscraper.fr/api2/mediaJeu.php?devid=[REDACTED]&devpassword=[REDACTED]&ssid=[REDACTED]&sspassword=[REDACTED]", self.root / "out", "unavailable.png", env=self.env, opener=unavailable)
        formatted = "".join(traceback.format_exception(caught.exception))
        for value in self.env.values():
            self.assertNotIn(value, formatted)
        self.assertNotIn("mediaJeu.php?", formatted)
        self.assertFalse((self.root / "out" / "unavailable.png").exists())
        # Unicode netloc validation itself can quote a username/password URL.
        with self.assertRaises(ValueError) as caught:
            provider.safe_url("https://fake-user:fake-user-pass@api.screenscraper.fr：443/media")
        formatted = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("fake-user", formatted)
        self.assertNotIn("fake-user-pass", formatted)
        def wrong_shape(url, timeout):
            return FakeResponse(b'[]', url=url)
        with self.assertRaises(provider.Blocked):
            provider.query(15, search_name="Game", env=self.env, opener=wrong_shape)


if __name__ == "__main__":
    unittest.main()
