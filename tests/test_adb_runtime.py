import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import adb_runtime


class AdbRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bundle_patch = patch.object(adb_runtime, "BUNDLE_ROOT", self.root / "no-default-bundle")
        bundle_patch.start()
        self.addCleanup(bundle_patch.stop)
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0,
                    b"Android Debug Bridge version 1.0.41\nVersion 37.0.1-123456\n", b""))

    def executable(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"isolated test fixture, never execute")
        return path.resolve()

    def test_explicit_is_authoritative_and_checked_without_device(self):
        selected = self.executable("chosen/adb.exe")
        alternative = self.executable("other/adb.exe")
        result = adb_runtime.discover_adb(str(selected), environ={"ADB": str(alternative)}, runner=self.runner)
        self.assertEqual(result["executable"], str(selected))
        self.assertEqual(result["source"], "explicit")
        self.assertEqual(self.runner.call_args.args[0], [str(selected), "version"])
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 10)

    def test_invalid_explicit_does_not_fall_back(self):
        good = self.executable("good/adb.exe")
        with self.assertRaises(adb_runtime.AdbResolutionError):
            adb_runtime.discover_adb(str(self.root / "missing.exe"), environ={"ADB": str(good)}, runner=self.runner)
        self.runner.assert_not_called()

    def test_environment_selected_path_is_resolved(self):
        selected = self.executable("user/adb.exe")
        result = adb_runtime.discover_adb(environ={"ADB": str(selected)}, runner=self.runner)
        self.assertEqual(result["source"], "ADB")
        self.assertTrue(Path(result["executable"]).is_absolute())

    def test_path_precedes_standard_sdk(self):
        selected = self.executable("path/adb.exe")
        self.executable("sdk/platform-tools/adb.exe")
        with patch.object(adb_runtime.shutil, "which", return_value=str(selected)):
            result = adb_runtime.discover_adb(environ={"ANDROID_SDK_ROOT": str(self.root / "sdk")}, platform="win32", runner=self.runner)
        self.assertEqual(result["source"], "PATH")

    def test_sdk_environment_and_default_locations(self):
        sdk = self.executable("sdk/platform-tools/adb.exe")
        with patch.object(adb_runtime.shutil, "which", return_value=None):
            result = adb_runtime.discover_adb(environ={"ANDROID_HOME": str(self.root / "sdk")}, platform="win32", runner=self.runner)
        self.assertEqual(result["executable"], str(sdk))
        local = self.executable("local/Android/Sdk/platform-tools/adb.exe")
        with patch.object(adb_runtime.shutil, "which", return_value=None):
            result = adb_runtime.discover_adb(environ={"LOCALAPPDATA": str(self.root / "local")}, platform="win32", runner=self.runner)
        self.assertEqual(result["executable"], str(local))

    def test_invalid_auto_candidate_can_use_sdk(self):
        selected = self.executable("path/adb.exe")
        sdk = self.executable("sdk/platform-tools/adb.exe")
        self.runner.side_effect = [subprocess.CompletedProcess([], 0, b"different tool", b""), self.runner.return_value]
        with patch.object(adb_runtime.shutil, "which", return_value=str(selected)):
            result = adb_runtime.discover_adb(environ={"ANDROID_SDK_ROOT": str(self.root / "sdk")}, platform="win32", runner=self.runner)
        self.assertEqual(result["executable"], str(sdk))

    def test_missing_provides_official_url_and_leaves_local_modes_available(self):
        with patch.object(adb_runtime.shutil, "which", return_value=None):
            with self.assertRaises(adb_runtime.AdbResolutionError) as caught:
                adb_runtime.discover_adb(environ={}, home=self.root, platform="linux", runner=self.runner)
        report = caught.exception.report()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["install_url"], adb_runtime.PLATFORM_TOOLS_URL)
        self.assertIn("local_library", report["unaffected"])
        self.runner.assert_not_called()

    def test_bad_output_nonzero_and_timeout_are_rejected(self):
        selected = self.executable("adb.exe")
        for result in (subprocess.CompletedProcess([], 0, b"not adb", b""),
                       subprocess.CompletedProcess([], 1, b"Android Debug Bridge version 1.0.41\n", b"")):
            with self.subTest(result=result):
                self.runner.return_value = result
                with self.assertRaises(adb_runtime.AdbResolutionError):
                    adb_runtime.discover_adb(str(selected), environ={}, runner=self.runner)
        self.runner.side_effect = subprocess.TimeoutExpired("adb", 10)
        with self.assertRaises(adb_runtime.AdbResolutionError):
            adb_runtime.discover_adb(str(selected), environ={}, runner=self.runner)

    def bundle(self, key, os_name, architectures, executable):
        root = self.root / "bundle"
        path = self.executable("bundle/" + executable)
        notice = self.executable("bundle/licenses/NOTICE.md")
        files = [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size,
                  "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in (path, notice)]
        (root / "bundle-manifest.json").write_text(json.dumps({"schema_version": 1, "platforms": {
            key: {"os": os_name, "architectures": architectures, "executable": executable, "files": files}}}), encoding="utf-8")
        return root, path

    def test_bundled_windows_precedes_path(self):
        root, bundled = self.bundle("windows-x86_64", "Windows", ["x86_64"], "windows-x86_64/adb.exe")
        other = self.executable("other/adb.exe")
        with patch.object(adb_runtime.shutil, "which", return_value=str(other)):
            result = adb_runtime.discover_adb(environ={}, platform="win32", machine="AMD64", bundle_root=root, runner=self.runner)
        self.assertEqual(result["executable"], str(bundled))
        self.assertEqual(result["source"], "bundled:windows-x86_64")

    def test_macos_intel_and_apple_silicon_choose_universal_binary(self):
        root, bundled = self.bundle("macos-universal2", "Darwin", ["x86_64", "arm64"], "macos-universal2/adb")
        with patch.object(adb_runtime.shutil, "which", return_value=None):
            for machine in ("x86_64", "arm64"):
                result = adb_runtime.discover_adb(environ={}, platform="darwin", machine=machine, bundle_root=root, runner=self.runner)
                self.assertEqual(result["executable"], str(bundled))

    def test_corrupt_bundle_or_missing_companion_is_not_executed(self):
        root, bundled = self.bundle("windows-x86_64", "Windows", ["x86_64"], "windows-x86_64/adb.exe")
        bundled.write_bytes(b"tampered")
        with patch.object(adb_runtime.shutil, "which", return_value=None):
            with self.assertRaises(adb_runtime.AdbResolutionError) as caught:
                adb_runtime.discover_adb(environ={}, platform="win32", machine="AMD64", home=self.root, bundle_root=root, runner=self.runner)
        self.assertEqual(caught.exception.attempts[0]["reason"], "adb_bundle_invalid")
        self.runner.assert_not_called()

    def test_explicit_still_overrides_bundle(self):
        root, bundled = self.bundle("windows-x86_64", "Windows", ["x86_64"], "windows-x86_64/adb.exe")
        explicit = self.executable("chosen/adb.exe")
        result = adb_runtime.discover_adb(str(explicit), environ={}, platform="win32", machine="AMD64", bundle_root=root, runner=self.runner)
        self.assertEqual(result["executable"], str(explicit))

    def test_bound_bundle_is_rechecked_on_reuse(self):
        root, bundled = self.bundle("windows-x86_64", "Windows", ["x86_64"], "windows-x86_64/adb.exe")
        (root / "licenses/NOTICE.md").write_bytes(b"changed")
        with self.assertRaises(adb_runtime.AdbResolutionError):
            adb_runtime.discover_adb(str(bundled), environ={}, platform="win32", machine="AMD64", bundle_root=root, runner=self.runner)
        self.runner.assert_not_called()

    def test_unsupported_architecture_does_not_run_wrong_binary(self):
        root, bundled = self.bundle("windows-x86_64", "Windows", ["x86_64"], "windows-x86_64/adb.exe")
        with patch.object(adb_runtime.shutil, "which", return_value=None):
            with self.assertRaises(adb_runtime.AdbResolutionError):
                adb_runtime.discover_adb(environ={}, platform="win32", machine="ARM64", home=self.root, bundle_root=root, runner=self.runner)
        self.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
