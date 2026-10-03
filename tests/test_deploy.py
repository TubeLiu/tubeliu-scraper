"""Finite deployment lifecycle fixtures; all Android calls are fake."""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deploy
import workbench_store
from esde_core import load_json, write_json, sha256


class LocalDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.target = self.base / "ES-DE"
        self.target.mkdir()
        self.sources = self.base / "prepared"
        self.sources.mkdir()
        self.run = self.base / "run"
        self.list_relative = "gamelists/nds/gamelist.xml"
        self.list_target = self.target / self.list_relative
        self.list_target.parent.mkdir(parents=True)
        self.original = '<gameList><game><path>./中文.nds</path><name>原始</name><playcount>8</playcount></game></gameList>'.encode("utf-8")
        self.desired = self.original.replace("原始".encode("utf-8"), "中文名".encode("utf-8"))
        self.list_target.write_bytes(self.original)
        self.list_source = self.sources / "gamelist.xml"
        self.list_source.write_bytes(self.desired)
        self.video_source = self.sources / "video.mp4"
        self.video_source.write_bytes(b"video fixture bytes")
        self.video_relative = "downloaded_media/nds/videos/子目录/中文 游戏.mp4"
        self.manifest = self.base / "source_manifest.json"
        self.entries = [{"local": str(self.list_source), "relative": self.list_relative}, {"local": str(self.video_source), "relative": self.video_relative}]
        write_json(self.manifest, {"files": self.entries})
        self.rom = self.base / "Roms" / "nds" / "中文.nds"
        self.rom.parent.mkdir(parents=True)
        self.rom.write_bytes(b"untouched ROM")

    def command(self, arguments):
        with contextlib.redirect_stdout(io.StringIO()):
            return deploy.main(arguments)

    def plan(self):
        return self.command(["plan", "--manifest", str(self.manifest), "--target", "local", "--esde-root", str(self.target), "--out", str(self.run)])

    def action(self, action):
        return self.command([action, "--run", str(self.run)])

    def test_plan_apply_verify_rollback_preserves_backups_and_rom(self):
        self.assertEqual(self.plan(), 0)
        plan_bytes = (self.run / "deployment_plan.json").read_bytes()
        backup = self.run / "deployment-backups" / "esde" / self.list_relative
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(self.action("apply"), 0)
        self.assertEqual(self.list_target.read_bytes(), self.desired)
        self.assertEqual((self.target / self.video_relative).read_bytes(), self.video_source.read_bytes())
        self.assertEqual(self.action("verify"), 0)
        self.assertEqual(load_json(self.run / "deployment_state.json")["status"], "awaiting_visual_qa")
        state = workbench_store.get_state(self.run)
        self.assertEqual(state["run"]["status"], "running")
        self.assertEqual(state["run"]["phase"], "awaiting_visual_qa")
        self.assertEqual(self.action("rollback"), 0)
        self.assertEqual(self.list_target.read_bytes(), self.original)
        self.assertFalse((self.target / self.video_relative).exists())
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual((self.run / "deployment_plan.json").read_bytes(), plan_bytes)
        self.assertEqual(self.rom.read_bytes(), b"untouched ROM")

    def test_changed_target_blocks_all_changes_before_install(self):
        self.assertEqual(self.plan(), 0)
        self.list_target.write_bytes(b"later user edit")
        self.assertEqual(self.action("apply"), 2)
        self.assertEqual(self.list_target.read_bytes(), b"later user edit")
        self.assertFalse((self.target / self.video_relative).exists())

    def test_changed_backup_or_source_blocks_apply(self):
        self.assertEqual(self.plan(), 0)
        self.list_source.write_bytes(self.desired + b"changed")
        self.assertEqual(self.action("apply"), 2)
        self.list_source.write_bytes(self.desired)
        backup = self.run / "deployment-backups" / "esde" / self.list_relative
        backup.write_bytes(b"bad backup")
        self.assertEqual(self.action("apply"), 2)
        self.assertEqual(self.list_target.read_bytes(), self.original)

    def test_later_user_edit_blocks_rollback_without_overwrite(self):
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.action("apply"), 0)
        self.list_target.write_bytes(b"user changed installed metadata")
        self.assertEqual(self.action("rollback"), 2)
        self.assertEqual(self.list_target.read_bytes(), b"user changed installed metadata")
        self.assertTrue((self.target / self.video_relative).exists())

    def test_interruption_resume_accepts_original_or_desired_only(self):
        self.assertEqual(self.plan(), 0)
        original_put = deploy.LocalTarget.put
        calls = []
        def interrupted(target, item, *args):
            if calls:
                raise deploy.AdbError("fake disconnect")
            calls.append(item["relative"])
            return original_put(target, item, *args)
        with patch.object(deploy.LocalTarget, "put", interrupted):
            self.assertEqual(self.action("apply"), 2)
        self.assertEqual(self.list_target.read_bytes(), self.desired)
        self.assertFalse((self.target / self.video_relative).exists())
        self.assertEqual(self.action("apply"), 0)
        self.assertEqual(self.action("verify"), 0)
        self.assertEqual((self.run / "deployment-backups" / "esde" / self.list_relative).read_bytes(), self.original)

    def test_plan_and_hash_baselines_are_immutable(self):
        self.assertEqual(self.plan(), 0)
        plan = self.run / "deployment_plan.json"
        before = plan.read_bytes()
        self.assertEqual(self.plan(), 2)
        self.assertEqual(plan.read_bytes(), before)
        plan.write_bytes(before + b" ")
        self.assertEqual(self.action("apply"), 2)
        self.assertEqual(self.list_target.read_bytes(), self.original)

    def test_exact_whitelist_rejects_rom_traversal_and_alias_paths(self):
        for relative in ("Roms/nds/foo.nds", "../outside.xml", "gamelists/nds/../../Roms/x.xml", "/absolute.xml", "downloaded_media/nds/wrong/foo.png", "gamelists/nds/settings.xml"):
            with self.subTest(relative=relative):
                with self.assertRaises(deploy.DeploymentError):
                    deploy.validate_relative(relative)
        write_json(self.manifest, {"files": [self.entries[0], self.entries[0]]})
        self.assertEqual(self.plan(), 2)
        self.assertFalse((self.run / "deployment_plan.json").exists())

    def test_verification_does_not_pass_before_install(self):
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.action("verify"), 2)
        result = load_json(self.run / "deployment_verification.json")
        self.assertEqual(result["status"], "needs_work")
        self.assertEqual(result["visual_qa"], "pending")

    def test_failed_later_attempt_invalidates_old_pass_receipt(self):
        self.assertEqual(self.plan(), 0)
        self.assertEqual(self.action("apply"), 0)
        self.assertEqual(self.action("verify"), 0)
        self.assertEqual(load_json(self.run / "deployment_verification.json")["status"], "pass")
        self.list_target.write_bytes(b"new user edit")
        self.assertEqual(self.action("verify"), 2)
        self.assertEqual(load_json(self.run / "deployment_verification.json")["status"], "pending")

    def test_explicit_theme_root_and_scope_overlap_are_checked(self):
        theme = self.target / "downloaded_media"
        theme.mkdir()
        picture = self.sources / "cover.png"
        picture.write_bytes(b"image")
        write_json(self.manifest, {"files": [{"local": str(picture), "relative": "downloaded_media/nds/covers/image.png"}, {"local": str(picture), "scope": "theme", "relative": "nds/covers/image.png"}]})
        self.assertEqual(self.command(["plan", "--manifest", str(self.manifest), "--target", "local", "--esde-root", str(self.target), "--theme-root", str(theme), "--out", str(self.run)]), 2)


class FakeAndroidLifecycleTests(unittest.TestCase):
    def test_fake_android_disconnect_resume_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            desired = b"fake video desired"
            original = b"fake video original"
            source = base / "video.mp4"
            source.write_bytes(desired)
            manifest = base / "manifest.json"
            relative = "downloaded_media/nds/videos/中文.mp4"
            write_json(manifest, {"files": [{"local": str(source), "relative": relative}]})
            run = base / "run"
            class FakeTarget:
                roots = {"esde": "/sdcard/ES-DE"}
                bytes = original
                disconnected = True
                def path(self, item):
                    return "/sdcard/ES-DE/" + item["relative"]
                def describe(self, item):
                    return {"exists": self.bytes is not None, "sha256": sha256(self.bytes) if self.bytes is not None else None}
                def backup(self, item, path):
                    Path(path).write_bytes(self.bytes)
                def assert_writable(self, item):
                    pass
                def put(self, item, path, expected_current, expected_source):
                    self.assert_current(expected_current)
                    self.__class__.bytes = Path(path).read_bytes()
                    if self.disconnected:
                        self.__class__.disconnected = False
                        raise deploy.AdbError("fake Android disconnect after atomic move")
                def assert_current(self, expected):
                    if sha256(self.bytes) != expected:
                        raise deploy.DeploymentError("wrong preflight hash")
                def remove(self, item, expected_current):
                    self.assert_current(expected_current)
                    self.__class__.bytes = None
                def restore_attributes(self, item):
                    pass
            def command(args):
                with contextlib.redirect_stdout(io.StringIO()):
                    return deploy.main(args)
            with patch.object(deploy, "create_target", return_value=FakeTarget()):
                self.assertEqual(command(["plan", "--manifest", str(manifest), "--target", "android", "--esde-root", "/sdcard/ES-DE", "--serial", "fake-selected", "--out", str(run)]), 0)
                self.assertEqual(command(["apply", "--run", str(run)]), 2)
                self.assertEqual(command(["apply", "--run", str(run)]), 0)
                self.assertEqual(command(["verify", "--run", str(run)]), 0)
                self.assertEqual(command(["rollback", "--run", str(run)]), 0)
            self.assertEqual(FakeTarget.bytes, original)

    def test_tar_stream_uses_exec_in_and_nonascii_safe_member(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "中文视频.mp4"
            source.write_bytes(b"video" * 70000)
            commands, scripts, captures = [], [], []
            class Capture(io.BytesIO):
                def close(self):
                    captures.append(self.getvalue())
                    super().close()
            class FakeProcess:
                returncode = 0
                def __init__(self, command, **kwargs):
                    commands.append(command)
                    self.stdin = Capture()
                def communicate(self, timeout=None):
                    return b"", b""
                def kill(self):
                    pass
            class FakeAdb:
                executable = "fake-adb"
                serial = "selected-fake"
                def shell_bytes(self, script):
                    scripts.append(script)
                    return b""
            target = deploy.AndroidTarget.__new__(deploy.AndroidTarget)
            target.roots = {"esde": "/sdcard/ES-DE"}
            target.adb = FakeAdb()
            item = {"scope": "esde", "relative": "downloaded_media/nds/videos/中文 游戏.mp4", "original": {"exists": False, "sha256": None}}
            digest = deploy.file_hash(source)
            with patch.object(deploy.subprocess, "Popen", FakeProcess):
                target.put(item, source, None, digest)
            self.assertEqual(commands[0][:4], ["fake-adb", "-s", "selected-fake", "exec-in"])
            self.assertNotIn("shell", commands[0])
            with tarfile.open(fileobj=io.BytesIO(captures[0]), mode="r:") as archive:
                members = archive.getmembers()
                self.assertEqual(len(members), 1)
                self.assertTrue(members[0].name.startswith(".esde-workbench-"))
                self.assertEqual(archive.extractfile(members[0]).read(), source.read_bytes())
            self.assertIn("mv ", scripts[-1])
            self.assertIn(digest, scripts[-1])

    def test_read_only_android_theme_returns_explicit_clone_guidance(self):
        class FakeAdb:
            def shell_bytes(self, script):
                return b"READONLY"
        target = deploy.AndroidTarget.__new__(deploy.AndroidTarget)
        target.roots = {"theme": "/readonly-theme"}
        target.adb = FakeAdb()
        with self.assertRaisesRegex(deploy.ThemeReadOnlyError, "Clone"):
            target.assert_writable({"scope": "theme", "relative": "theme.xml"})

    def test_running_esde_blocks_android_mutations(self):
        class FakeAdb:
            def shell_bytes(self, script):
                return b"RUNNING"
        target = deploy.AndroidTarget.__new__(deploy.AndroidTarget)
        target.adb = FakeAdb()
        target.package = "org.es_de.frontend"
        with self.assertRaisesRegex(deploy.DeploymentError, "Close ES-DE"):
            target.assert_quiescent()

    def test_hash_queries_batch_missing_targets_without_per_file_adb(self):
        scripts = []
        class FakeAdb:
            def shell_bytes(self, script):
                scripts.append(script)
                indexes = re.findall(r"printf '(\d+) ABSENT", script)
                return "".join(index + " ABSENT\n" for index in indexes).encode("ascii")
        target = deploy.AndroidTarget.__new__(deploy.AndroidTarget)
        target.roots = {"esde": "/sdcard/ES-DE"}
        target.adb = FakeAdb()
        items = [{"scope": "esde", "relative": "downloaded_media/nds/videos/中文" + str(i) + ".mp4"} for i in range(10300)]
        descriptions = target.describe_many(items)
        self.assertEqual(len(scripts), 206)
        self.assertEqual(len(descriptions), 10300)
        self.assertTrue(all(item == {"exists": False, "sha256": None} for item in descriptions))

    def test_deep_unicode_paths_use_bounded_smaller_batches(self):
        target = deploy.AndroidTarget.__new__(deploy.AndroidTarget)
        target.roots = {"esde": "/sdcard/ES-DE"}
        items = [{"scope": "esde", "relative": "downloaded_media/nds/videos/" + ("深层中文目录" * 20 + "/") * 3 + "游戏" + str(i) + ".mp4"} for i in range(50)]
        groups = list(target._groups(items, "transfer"))
        self.assertGreater(len(groups), 1)
        self.assertEqual(sum(map(len, groups)), 50)
        self.assertTrue(all(len(group) <= 50 for group in groups))
        self.assertLess(len(target._safe_path_script(items[0]).encode("utf-8")), 2500)

    def test_batch_backup_streams_only_exact_existing_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            names = ["downloaded_media/nds/covers/中文 游戏.png", "gamelists/nds/gamelist.xml"]
            content = [b"original image", b"<gameList />"]
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for name, data in zip(names, content):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            commands = []
            class FakeProcess:
                returncode = 0
                def __init__(self, command, **kwargs):
                    commands.append(command)
                    self.stdout = io.BytesIO(buffer.getvalue())
                def communicate(self, timeout=None):
                    return b"", b""
                def kill(self):
                    pass
            class FakeAdb:
                executable = "fake-adb"
                serial = "fake-selected"
            target = deploy.AndroidTarget.__new__(deploy.AndroidTarget)
            target.roots = {"esde": "/sdcard/ES-DE"}
            target.adb = FakeAdb()
            entries = [({"scope": "esde", "relative": name, "original": {"sha256": sha256(data)}}, base / (str(i) + ".backup")) for i, (name, data) in enumerate(zip(names, content))]
            with patch.object(deploy.subprocess, "Popen", FakeProcess):
                target.backup_many(entries)
            self.assertEqual(len(commands), 1)
            self.assertIn("exec-out", commands[0])
            self.assertIn("tar -cf", commands[0][-1])
            for (_, backup), data in zip(entries, content):
                self.assertEqual(backup.read_bytes(), data)

    def test_batch_tar_verifies_entire_stage_before_any_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sources = [base / "one.mp4", base / "two.mp4"]
            for index, source in enumerate(sources):
                source.write_bytes(("source" + str(index)).encode("utf-8") * 100)
            captures, commands = [], []
            class Capture(io.BytesIO):
                def close(self):
                    captures.append(self.getvalue())
                    super().close()
            class FakeProcess:
                returncode = 0
                def __init__(self, command, **kwargs):
                    commands.append(command)
                    self.stdin = Capture() if "exec-in" in command else None
                    self.stdout = io.BytesIO(b"DONE 0\nDONE 1\n") if "exec-out" in command else None
                def communicate(self, timeout=None):
                    return b"", b""
                def kill(self):
                    pass
            class FakeAdb:
                executable = "fake-adb"
                serial = "selected-fake"
            target = deploy.AndroidTarget.__new__(deploy.AndroidTarget)
            target.roots = {"esde": "/sdcard/ES-DE"}
            target.adb = FakeAdb()
            items = [{"scope": "esde", "relative": "downloaded_media/nds/videos/子目录/中文" + str(i) + ".mp4"} for i in range(2)]
            entries = [(item, source, None, deploy.file_hash(source)) for item, source in zip(items, sources)]
            done = []
            with patch.object(deploy.subprocess, "Popen", FakeProcess):
                target.put_many(entries, lambda item: done.append(item["relative"]))
            self.assertEqual(len(commands), 2)
            self.assertIn("exec-in", commands[0])
            self.assertIn("exec-out", commands[1])
            script = commands[1][-1]
            first_move = script.index("mv ")
            for _, _, _, digest in entries:
                self.assertLess(script.index(digest), first_move)
            self.assertEqual(done, [item["relative"] for item in items])
            with tarfile.open(fileobj=io.BytesIO(captures[0]), mode="r:") as archive:
                members = archive.getmembers()
                self.assertEqual(len(members), 2)
                self.assertTrue(all(member.name.startswith("downloaded_media/nds/videos/子目录/.esde-workbench-") for member in members))
                for member, source in zip(members, sources):
                    self.assertEqual(archive.extractfile(member).read(), source.read_bytes())

    def test_cli_batch_resume_keeps_per_item_durable_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "video.mp4"
            source.write_bytes(b"desired")
            relatives = ["downloaded_media/nds/videos/游戏" + str(i) + ".mp4" for i in range(3)]
            manifest, run = base / "manifest.json", base / "run"
            write_json(manifest, {"files": [{"local": str(source), "relative": relative} for relative in relatives]})
            class FakeTarget:
                data = {relatives[0]: b"original", relatives[1]: None, relatives[2]: None}
                hash_calls = 0
                fail = True
                def path(self, item):
                    return "/sdcard/ES-DE/" + item["relative"]
                def describe(self, item):
                    raise AssertionError("CLI must not fall back to per-file Android hashes")
                def describe_many(self, items):
                    self.hash_calls += 1
                    return [{"exists": self.data[item["relative"]] is not None, "sha256": sha256(self.data[item["relative"]]) if self.data[item["relative"]] is not None else None} for item in items]
                def backup_many(self, entries):
                    for item, path in entries:
                        Path(path).write_bytes(self.data[item["relative"]])
                def assert_quiescent(self):
                    pass
                def assert_writable(self, item):
                    pass
                def put_many(self, entries, on_done):
                    for item, path, current, desired in entries:
                        actual = self.data[item["relative"]]
                        if (sha256(actual) if actual is not None else None) != current:
                            raise deploy.DeploymentError("Fake target precondition changed")
                        self.data[item["relative"]] = Path(path).read_bytes()
                        on_done(item)
                        if self.fail:
                            self.fail = False
                            raise deploy.AdbError("fake disconnect after confirmed first atomic replace")
                def remove_many(self, entries, on_done):
                    for item, current in entries:
                        self.data[item["relative"]] = None
                        on_done(item)
            target = FakeTarget()
            def command(arguments):
                with contextlib.redirect_stdout(io.StringIO()):
                    return deploy.main(arguments)
            with patch.object(deploy, "create_target", return_value=target):
                self.assertEqual(command(["plan", "--manifest", str(manifest), "--target", "android", "--esde-root", "/sdcard/ES-DE", "--serial", "selected-fake", "--out", str(run)]), 0)
                self.assertEqual(target.hash_calls, 2)
                self.assertEqual(command(["apply", "--run", str(run)]), 2)
                state = load_json(run / "deployment_state.json")
                self.assertEqual(state["counts"]["apply"], 1)
                with contextlib.closing(deploy.sqlite3.connect(run / "deployment_journal.sqlite")) as journal:
                    self.assertEqual(journal.execute("SELECT COUNT(*) FROM checkpoints WHERE action='apply'").fetchone()[0], 1)
                self.assertEqual(command(["apply", "--run", str(run)]), 0)
                self.assertEqual(command(["verify", "--run", str(run)]), 0)
                self.assertEqual(target.hash_calls, 6)
                self.assertEqual(command(["rollback", "--run", str(run)]), 0)
                self.assertEqual(target.hash_calls, 8)
            self.assertEqual(target.data, {relatives[0]: b"original", relatives[1]: None, relatives[2]: None})


if __name__ == "__main__":
    unittest.main()
