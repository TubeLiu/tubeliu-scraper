#!/usr/bin/env python3
"""Bounded ADB discovery and read-only executable validation.

No download, SDK license acceptance, PATH mutation, or device selection occurs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as host_platform
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

PLATFORM_TOOLS_URL = "https://developer.android.com/tools/releases/platform-tools"
BUNDLE_ROOT = Path(__file__).resolve().parents[1] / "assets" / "adb"


class AdbResolutionError(RuntimeError):
    def __init__(self, message, *, reason="adb_not_found", attempts=None):
        super().__init__(message)
        self.reason = reason
        self.attempts = attempts or []

    def report(self):
        return {"status": "blocked", "reason": self.reason, "message": str(self),
                "install_url": PLATFORM_TOOLS_URL, "attempts": self.attempts,
                "next_step": "Install Google's Platform-Tools, then rerun with --adb pointing to its adb executable.",
                "unaffected": ["local_library", "workbench"]}


def _known_candidates(env, platform, home):
    name = "adb.exe" if platform == "win32" else "adb"
    for variable in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        if env.get(variable):
            yield Path(env[variable]) / "platform-tools" / name, variable
    if platform == "win32":
        if env.get("LOCALAPPDATA"):
            yield Path(env["LOCALAPPDATA"]) / "Android" / "Sdk" / "platform-tools" / name, "default_sdk"
        if env.get("USERPROFILE"):
            yield Path(env["USERPROFILE"]) / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / name, "default_sdk"
    elif platform == "darwin":
        yield home / "Library" / "Android" / "sdk" / "platform-tools" / name, "default_sdk"
    else:
        yield home / "Android" / "Sdk" / "platform-tools" / name, "default_sdk"
        yield home / "Android" / "sdk" / "platform-tools" / name, "default_sdk"


def _resolve_named(value, env):
    candidate = Path(value).expanduser()
    # A path selected by the user is authoritative; never silently switch it.
    if candidate.is_file():
        return candidate.resolve()
    if "/" not in value and "\\" not in value:
        found = shutil.which(value, path=env.get("PATH", ""))
        if found:
            return Path(found).resolve()
    return None


def _bundled_candidate(platform, machine, root):
    """Select a maintained bundle and check every shipped companion file."""
    root = Path(root)
    manifest_path = root / "bundle-manifest.json"
    if not manifest_path.exists():
        return None
    architecture = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine.lower(), machine.lower())
    os_name = {"win32": "Windows", "darwin": "Darwin"}.get(platform)
    if os_name is None:
        return None
    try:
        if root.is_symlink() or manifest_path.is_symlink() or manifest_path.stat().st_size > 2000000:
            raise ValueError("Invalid bundle manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("platforms"), dict):
            raise ValueError("Invalid bundle schema")
        matching = [(name, value) for name, value in manifest["platforms"].items()
                    if isinstance(value, dict) and value.get("os") == os_name and architecture in value.get("architectures", [])]
        if not matching:
            return None
        if len(matching) != 1:
            raise ValueError("Ambiguous bundle platform")
        name, bundle = matching[0]
        files = bundle.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("Missing bundle file hashes")
        verified = set()
        for item in files:
            relative = item["path"]
            if not isinstance(relative, str):
                raise ValueError("Invalid bundle file path")
            parts = relative.split("/")
            if "\\" in relative or relative.startswith("/") or re.match(r"^[A-Za-z]:", relative) or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("Invalid bundle file path")
            path = root
            for part in parts:
                path /= part
                info = path.lstat()
                if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Bundle file must not be a link")
            if not stat.S_ISREG(info.st_mode) or type(item["size"]) is not int or info.st_size != item["size"] or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError("Bundle file size/hash is invalid")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != item["sha256"]:
                raise ValueError("Bundle file checksum mismatch")
            verified.add(relative)
        if bundle.get("executable") not in verified:
            raise ValueError("Bundle executable has no checked hash")
        path = (root / bundle["executable"]).resolve()
        if os.name != "nt" and not path.stat().st_mode & stat.S_IXUSR:
            # ZIP installers can lose mode bits. Enable only owner execution on
            # these just-verified bytes; do not alter quarantine/security policy.
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path, "bundled:" + name
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise AdbResolutionError("Bundled ADB is incomplete or failed its checksum. Reinstall this skill or provide a working --adb PATH.",
                                 reason="adb_bundle_invalid") from error


def _validate(path, source, runner):
    try:
        result = runner([str(path), "version"], stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdbResolutionError("ADB could not run its version check; provide a working --adb PATH. "
                                 + PLATFORM_TOOLS_URL, reason="adb_invalid") from error
    raw = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else str(result.stdout)
    match = re.search(r"^Android Debug Bridge version\s+(\d+(?:\.\d+)+)\s*$", raw, re.MULTILINE)
    if result.returncode != 0 or not match:
        raise AdbResolutionError("The selected executable did not report a valid Android Debug Bridge version. "
                                 + PLATFORM_TOOLS_URL, reason="adb_invalid")
    release = re.search(r"^Version\s+([^\r\n]+)", raw, re.MULTILINE)
    return {"status": "ready", "executable": str(path), "source": source,
            "adb_version": match.group(1), "platform_tools_version": release.group(1).strip() if release else None}


def discover_adb(executable=None, *, environ=None, platform=None, machine=None, home=None, runner=None, bundle_root=None):
    """Return a verified absolute executable and provenance, or an actionable error.

    Explicit --adb and ADB are authoritative. Auto-discovery tries PATH and a
    bundled executable, PATH and a bounded set of SDK locations; invalid auto
    candidates can be skipped. A corrupt supplied bundle is never executed.
    """
    env = dict(os.environ if environ is None else environ)
    platform = platform or sys.platform
    machine = machine or host_platform.machine()
    home = Path(home) if home is not None else Path.home()
    runner = runner or subprocess.run
    bundle_root = BUNDLE_ROOT if bundle_root is None else Path(bundle_root)
    selected = str(executable).strip() if executable is not None else env.get("ADB", "").strip()
    if selected:
        source = "explicit" if executable is not None else "ADB"
        path = _resolve_named(selected, env)
        if path is None:
            raise AdbResolutionError("The selected ADB executable does not exist. Correct --adb/ADB or install Google's Platform-Tools: "
                                     + PLATFORM_TOOLS_URL, reason="adb_selected_not_found")
        if path.is_relative_to(bundle_root.resolve()):
            bundled = _bundled_candidate(platform, machine, bundle_root)
            if bundled is None or bundled[0] != path:
                raise AdbResolutionError("The selected bundled executable does not match this system and architecture.", reason="adb_bundle_invalid")
        return _validate(path, source, runner)
    name = "adb.exe" if platform == "win32" else "adb"
    candidates = []
    bundle_error = None
    try:
        bundled = _bundled_candidate(platform, machine, bundle_root)
        if bundled:
            candidates.append(bundled)
    except AdbResolutionError as error:
        bundle_error = error
    found = shutil.which(name, path=env.get("PATH", ""))
    if found:
        candidates.append((Path(found), "PATH"))
    candidates.extend(_known_candidates(env, platform, home))
    seen, attempts = set(), ([{"source": "bundled", "reason": bundle_error.reason}] if bundle_error else [])
    for candidate, source in candidates:
        path = candidate.expanduser().resolve()
        if str(path) in seen:
            continue
        seen.add(str(path))
        if not path.is_file():
            continue
        try:
            return _validate(path, source, runner)
        except AdbResolutionError:
            attempts.append({"executable": str(path), "source": source, "reason": "adb_invalid"})
    raise AdbResolutionError("ADB was not found in PATH or standard Android SDK locations. Install Google's Platform-Tools: "
                             + PLATFORM_TOOLS_URL, attempts=attempts)


def resolve_adb(executable=None, **kwargs):
    return discover_adb(executable, **kwargs)["executable"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Find and validate ADB without touching a device")
    parser.add_argument("--adb", help="Explicit adb executable; otherwise inspect ADB, PATH, and standard SDK locations")
    parser.add_argument("--out", type=Path, help="Save the environment report as JSON")
    parser.add_argument("--run", type=Path, help="Record this environment check in the real-time workbench")
    args = parser.parse_args(argv)
    code = 0
    try:
        report = discover_adb(args.adb)
    except AdbResolutionError as error:
        report, code = error.report(), 2
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded + "\n", encoding="utf-8")
    if args.run:
        from workbench_store import append_event, emit_update, init_run
        init_run(args.run)
        ready = report["status"] == "ready"
        emit_update(args.run, phase="adb_environment", status="running" if ready else "blocked",
                    phase_status="done" if ready else "blocked", completed=1 if ready else 0, total=1,
                    message="ADB 已确认，可继续检查设备授权" if ready else "ADB 检查未通过；本地游戏库与工作台仍可使用")
        append_event(args.run, "adb_environment", "设备连接工具检查", report)
    print(encoded)
    return code


if __name__ == "__main__":
    sys.exit(main())
