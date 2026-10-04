"""Lazily read only explicitly inventoried Android ES-DE media into a run cache.

No ROM reads, device writes, implicit device selection or application settings.
The workbench registers opaque assets before requesting this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from adb_runtime import AdbResolutionError, PLATFORM_TOOLS_URL, resolve_adb

MAX_BYTES = 200 * 1024 * 1024
MEDIA_TYPES = {"covers", "screenshots", "titlescreens", "marquees", "miximages", "videos",
               "backcovers", "3dboxes", "physicalmedia", "fanart"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".avi"}
ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
HASH = re.compile(r"^[a-fA-F0-9]{64}$")
_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()
_profiles = {}
_profiles_guard = threading.Lock()


class PreviewUnavailable(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = str(code), str(message)


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError as error:
        raise PreviewUnavailable("cache_unavailable", "Preview files must remain inside the current run") from error


def _profile(path):
    stat = path.stat()
    fingerprint = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    with _profiles_guard:
        saved = _profiles.get(str(path))
        if saved and saved[0] == fingerprint:
            return saved[1]
    data = _json(path)
    with _profiles_guard:
        _profiles[str(path)] = (fingerprint, data)
    return data


def _remote_relative(path, root, kind=None):
    path, root = str(path), str(root).rstrip("/")
    if "\\" in path or any(value in path for value in ("\x00", "\r", "\n")):
        raise PreviewUnavailable("invalid_media_path", "Media path is not a supported Android path")
    if not root.startswith("/") or PurePosixPath(root).name != "downloaded_media":
        raise PreviewUnavailable("invalid_media_root", "Preview requires an explicit downloaded_media root")
    if any(part in {".", ".."} for part in root.split("/") if part):
        raise PreviewUnavailable("invalid_media_root", "Media root contains traversal")
    if not path.startswith(root + "/"):
        raise PreviewUnavailable("invalid_media_path", "Media is outside the explicitly bound downloaded_media root")
    relative = path[len(root) + 1:]
    parts = relative.split("/")
    if len(parts) < 3 or any(part in {"", ".", ".."} for part in parts):
        raise PreviewUnavailable("invalid_media_path", "Expected platform/media-type/filename beneath downloaded_media")
    if parts[1] not in MEDIA_TYPES or kind is not None and parts[1] != kind:
        raise PreviewUnavailable("invalid_media_type", "Media type is not in the read-only preview allowlist")
    extensions = VIDEO_EXTENSIONS if parts[1] == "videos" else IMAGE_EXTENSIONS
    if PurePosixPath(parts[-1]).suffix.casefold() not in extensions:
        raise PreviewUnavailable("unsupported_media", "File extension is not allowed for this preview type")
    return relative


def bind_remote_profile(run, *, adb, serial, media_root, inventory, snapshot=None):
    """Bind explicitly approved historical inventory without connecting to a device."""
    run = Path(run).resolve()
    selected, executable = str(serial or ""), str(adb or "")
    if not selected or any(value in selected for value in ("\x00", "\r", "\n")):
        raise PreviewUnavailable("device_not_bound", "尚未绑定明确的设备序列号，无法读取媒体预览")
    if not executable or any(value in executable for value in ("\x00", "\r", "\n")):
        raise PreviewUnavailable("adb_not_bound", "尚未绑定该设备使用的 ADB 工具，无法读取媒体预览")
    root = str(media_root or "").rstrip("/")
    approved = {}
    for item in inventory:
        try:
            _remote_relative(item["path"], root)
            value = item.get("size")
            size = int(value) if value is not None and int(value) > 0 else None
            approved[item["path"]] = {"size": size, "sha256": item.get("sha256") or ""}
        except (KeyError, TypeError, ValueError, PreviewUnavailable):
            # A snapshot may contain manuals, optional assets and broken files.
            # Keeping them in the inventory never grants the preview reader access.
            continue
    if not approved:
        raise PreviewUnavailable("unregistered_media", "Explicit preview inventory contains no allowed media files")
    identity = {"snapshot": str(Path(snapshot).resolve()) if snapshot else None,
                "serial": selected, "adb": executable, "media_root": root,
                "inventory_sha256": hashlib.sha256(json.dumps(approved, sort_keys=True).encode()).hexdigest()}
    profile_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    profile = {"schema_version": 1, "id": profile_id, **identity, "inventory": approved,
               "timeout": 120, "max_bytes": MAX_BYTES,
               "bound_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    parent = run / "preview-profiles"
    _inside(parent, run)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / (profile_id + ".json")
    if not target.exists():
        temporary = parent / ("." + profile_id + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return profile_id


def bind_snapshot(run, snapshot, *, adb=None, serial=None, media_root=None):
    """Persist one explicit transport profile; this does not connect or fetch media."""
    snapshot = Path(snapshot).resolve()
    manifest = _json(snapshot / "manifest.json")
    if not manifest.get("complete"):
        raise PreviewUnavailable("incomplete_snapshot", "Complete the read-only snapshot before binding previews")
    selected = str(serial or manifest.get("serial") or "")
    if not selected or selected != manifest.get("serial"):
        raise PreviewUnavailable("device_not_bound", "Preview requires the exact serial recorded by this snapshot")
    root = str(media_root or manifest.get("remote_media_root") or "").rstrip("/")
    if root != str(manifest.get("remote_media_root", "")).rstrip("/"):
        raise PreviewUnavailable("media_root_mismatch", "Preview root must exactly match the snapshot media inventory")
    return bind_remote_profile(run, adb=adb or manifest.get("adb_executable"), serial=selected,
                               media_root=root, inventory=_json(snapshot / "media_inventory.json"), snapshot=snapshot)


def _validate(run, descriptor):
    profile_id = str(descriptor.get("profile_id") or "")
    if not ID.fullmatch(profile_id):
        raise PreviewUnavailable("device_not_bound", "媒体尚未绑定明确的设备配置")
    root = Path(run).resolve()
    profile_path = root / "preview-profiles" / (profile_id + ".json")
    _inside(profile_path, root)
    if profile_path.is_symlink() or not profile_path.is_file():
        raise PreviewUnavailable("device_not_bound", "该媒体对应的设备预览配置不可用")
    try:
        profile = _profile(profile_path)
    except (OSError, ValueError) as error:
        raise PreviewUnavailable("invalid_profile", "Selected preview profile could not be read") from error
    if profile.get("id") != profile_id or not profile.get("serial") or not profile.get("adb"):
        raise PreviewUnavailable("invalid_profile", "Selected preview profile has no explicit transport binding")
    remote = str(descriptor.get("remote_path") or "")
    relative = _remote_relative(remote, profile.get("media_root", ""), descriptor.get("kind"))
    item = profile.get("inventory", {}).get(remote)
    if not isinstance(item, dict):
        raise PreviewUnavailable("unregistered_media", "File was not approved by this snapshot's media inventory")
    value = item.get("size")
    size = int(value) if value is not None and int(value) > 0 else None
    limit = min(MAX_BYTES, max(1, int(profile.get("max_bytes", MAX_BYTES))))
    if size is not None and size > limit:
        raise PreviewUnavailable("media_size_limit", "媒体为空或超过 200 MB 预览限制")
    requested_size = descriptor.get("size")
    requested_size = int(requested_size) if requested_size is not None and int(requested_size) > 0 else None
    if requested_size is not None and size is not None and requested_size != size:
        raise PreviewUnavailable("media_changed", "媒体大小与原始快照不同，需要重新核对")
    size = size or requested_size
    if size is not None and size > limit:
        raise PreviewUnavailable("media_size_limit", "媒体超过 200 MB 预览限制")
    expected = str(descriptor.get("sha256") or item.get("sha256") or "")
    if expected and not HASH.fullmatch(expected):
        raise PreviewUnavailable("invalid_media_hash", "Registered media SHA-256 is invalid")
    return profile, relative, size, expected.casefold()


def validate_remote_media(run, descriptor):
    _validate(run, descriptor)


def _require_device(profile):
    # An old bare `adb` profile must be explicitly rebound after discovery;
    # a changed PATH cannot silently select a different transport executable.
    if not Path(profile["adb"]).is_absolute():
        raise PreviewUnavailable("adb_not_bound", "请先检查 ADB，并用确认后的完整路径重新绑定媒体预览：" + PLATFORM_TOOLS_URL)
    try:
        profile["adb"] = resolve_adb(profile["adb"])
    except AdbResolutionError as error:
        raise PreviewUnavailable("adb_unavailable", "已绑定的 ADB 不可用。请修复该路径或安装官方 Platform-Tools：" + PLATFORM_TOOLS_URL) from error
    try:
        result = subprocess.run([profile["adb"], "devices", "-l"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise PreviewUnavailable("device_unavailable", "设备连接工具不可用，请重新连接对应设备后重试") from error
    if result.returncode:
        raise PreviewUnavailable("device_unavailable", "暂时无法检查对应设备的连接状态")
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    matches = [line.split() for line in lines if line.split() and line.split()[0] == profile["serial"]]
    if not matches or len(matches[0]) < 2:
        raise PreviewUnavailable("device_disconnected", "对应设备未连接；重新连接后可重试此媒体预览")
    state = matches[0][1]
    if state != "device":
        raise PreviewUnavailable("device_unauthorized" if state == "unauthorized" else "device_unavailable",
                                 "请解锁对应设备并允许 USB 调试，然后重试此预览" if state == "unauthorized" else "对应设备暂不可用：" + state)


def _fetch(profile, remote, relative, size, target):
    """Stream raw exec-out bytes, reject changed size/symlinks and accept no partial file."""
    root = profile["media_root"]
    # realpath equality forbids following any file/ancestor symlink into another
    # game, a ROM folder or an application settings directory.
    script = ("root=$(realpath " + shlex.quote(root) + ") && "
              "file=$(realpath " + shlex.quote(remote) + ") && "
              "test \"$file\" = \"$root/" + relative.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + "\" && "
              "test -f \"$file\" && test ! -L " + shlex.quote(remote) + " && "
              "test \"$(stat -c %s \"$file\")\" -eq " + str(size) + " && cat \"$file\" && "
              "test \"$(stat -c %s \"$file\")\" -eq " + str(size))
    command = [profile["adb"], "-s", profile["serial"], "exec-out", "sh", "-c", shlex.quote(script)]
    import tempfile
    with tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr)
        except OSError as error:
            raise PreviewUnavailable("device_unavailable", "ADB media reader could not start") from error
        state = {"bytes": 0, "error": None}

        def copy_bytes():
            try:
                with target.open("xb") as output:
                    while True:
                        chunk = process.stdout.read(1024 * 1024)
                        if not chunk:
                            break
                        state["bytes"] += len(chunk)
                        if state["bytes"] > size or state["bytes"] > MAX_BYTES:
                            raise PreviewUnavailable("media_changed", "Media grew after the snapshot; no partial preview was accepted")
                        output.write(chunk)
            except Exception as error:
                state["error"] = error

        reader = threading.Thread(target=copy_bytes, daemon=True)
        reader.start()
        try:
            timeout = min(300, max(1, int(profile.get("timeout", 120))))
            reader.join(timeout)
            if reader.is_alive():
                process.kill()
                process.wait(timeout=5)
                reader.join(5)
                raise PreviewUnavailable("preview_timeout", "读取媒体超时；重新连接对应设备后可重试")
            if state["error"]:
                process.kill()
                process.wait(timeout=5)
                error = state["error"]
                if isinstance(error, PreviewUnavailable):
                    raise error
                raise PreviewUnavailable("cache_unavailable", "Media cache could not be written") from error
            try:
                result = process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait(timeout=5)
                raise PreviewUnavailable("preview_timeout", "ADB did not finish the bounded media read") from error
            if result:
                raise PreviewUnavailable("remote_media_unavailable", "设备上的媒体缺失、已变化或暂时不可读；重新连接后可重试")
            if state["bytes"] != size:
                raise PreviewUnavailable("media_changed", "媒体读取提前中断，未接受不完整的预览文件")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()


def _measure(profile, remote, relative):
    """Read a single approved file size when the historical inventory omitted it."""
    script = ("root=$(realpath " + shlex.quote(profile["media_root"]) + ") && "
              "file=$(realpath " + shlex.quote(remote) + ") && "
              "test \"$file\" = \"$root/" + relative.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + "\" && "
              "test -f \"$file\" && test ! -L " + shlex.quote(remote) + " && stat -c %s \"$file\"")
    try:
        result = subprocess.run([profile["adb"], "-s", profile["serial"], "exec-out", "sh", "-c", shlex.quote(script)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise PreviewUnavailable("device_unavailable", "暂时无法读取对应设备上该媒体的大小") from error
    data = result.stdout.strip()
    if result.returncode or len(data) > 20 or not data.isdigit():
        raise PreviewUnavailable("remote_media_unavailable", "对应设备上的媒体缺失或暂时不可读")
    size = int(data)
    if size <= 0 or size > min(MAX_BYTES, int(profile.get("max_bytes", MAX_BYTES))):
        raise PreviewUnavailable("media_size_limit", "媒体为空或超过 200 MB 预览限制")
    return size


def materialize(run, assetrecord):
    """Return a verified cache Path, or a recoverable error without changing run status."""
    run = Path(run).resolve()
    profile, relative, size, expected = _validate(run, assetrecord)
    asset_id = str(assetrecord.get("id") or "")
    if not ID.fullmatch(asset_id):
        raise PreviewUnavailable("invalid_asset", "Preview requires a registered opaque asset ID")
    with _locks_guard:
        lock = _locks.setdefault((str(run), asset_id), threading.Lock())
    with lock:
        directory = run / "cache" / "media"
        _inside(directory, run)
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise PreviewUnavailable("cache_unavailable", "Preview cache cannot be a symlink")
        suffix = PurePosixPath(relative).suffix.casefold()
        target = directory / (asset_id + suffix)
        receipt = directory / (asset_id + ".json")
        signature = hashlib.sha256(json.dumps({"profile": assetrecord["profile_id"], "path": assetrecord["remote_path"]}, sort_keys=True).encode()).hexdigest()
        if target.is_file() and not target.is_symlink() and receipt.is_file() and not receipt.is_symlink():
            try:
                saved = _json(receipt)
                digest = _hash_file(target)
                actual_size = target.stat().st_size
                if actual_size == saved.get("size") and 0 < actual_size <= MAX_BYTES and (size is None or actual_size == size) and saved.get("signature") == signature and saved.get("sha256") == digest and (not expected or expected == digest):
                    return target
            except (OSError, ValueError):
                pass
        _require_device(profile)
        actual_size = size or _measure(profile, assetrecord["remote_path"], relative)
        temporary = directory / ("." + asset_id + "." + uuid.uuid4().hex + ".part")
        receipt_tmp = directory / ("." + asset_id + "." + uuid.uuid4().hex + ".json.tmp")
        try:
            _fetch(profile, assetrecord["remote_path"], relative, actual_size, temporary)
            digest = _hash_file(temporary)
            if expected and digest != expected:
                raise PreviewUnavailable("media_hash_mismatch", "媒体内容与已登记的校验值不同，未接受此预览")
            receipt_tmp.write_text(json.dumps({"signature": signature, "sha256": digest, "size": actual_size}), encoding="utf-8")
            temporary.replace(target)
            receipt_tmp.replace(receipt)
            return target
        except OSError as error:
            raise PreviewUnavailable("cache_unavailable", "Media cache is unavailable") from error
        finally:
            # Only this request's random temporary files are removed.
            temporary.unlink(missing_ok=True)
            receipt_tmp.unlink(missing_ok=True)
