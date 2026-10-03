"""Explicit, read-only media registry for the local authenticated workbench.

Browser requests contain opaque asset IDs, never a local or Android file path.
Only the workflow's Python helpers can register a specific approved local file.
"""
from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import stat
import threading
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote

from workbench_store import _event, connect, dumps, now, redact

ASSET_ID = re.compile(r"^[a-f0-9]{48}$")
KIND = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
MIME_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
              ".mp4": "video/mp4", ".webm": "video/webm"}
_HASH_CACHE = OrderedDict()
_HASH_LOCK = threading.Lock()
_REGISTRY_INSERT = """INSERT INTO media_assets(id,job_id,kind,local_path,sha256,size,mime,mtime_ns,device,inode,origin,source_state,remote_path,profile_id,registered_at)
 VALUES(:id,:job_id,:kind,:local_path,:sha256,:size,:mime,:mtime_ns,:device,:inode,:origin,:source_state,:remote_path,:profile_id,:registered_at)
 ON CONFLICT(id) DO UPDATE SET local_path=excluded.local_path,sha256=excluded.sha256,size=excluded.size,
 mime=excluded.mime,mtime_ns=excluded.mtime_ns,device=excluded.device,inode=excluded.inode,
 origin=excluded.origin,source_state=excluded.source_state,registered_at=excluded.registered_at"""


def _plain_path(path):
    """Reject symbolic links and Windows junction/reparse redirects in every component."""
    original = Path(os.path.abspath(os.fspath(path)))
    for part in (original, *original.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Media path cannot contain symbolic links or reparse redirects")
    canonical = original.resolve(strict=True)
    if canonical != original:
        raise ValueError("Media path redirects to a different location")
    info = canonical.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Media must be a regular local file")
    return canonical, info


def _fingerprint(info):
    # Windows file identities can exceed SQLite's signed 64-bit INTEGER.
    return (info.st_size, info.st_mtime_ns, hex(info.st_dev), hex(info.st_ino))


def _mime(path, header):
    extension_mime = MIME_TYPES.get(path.suffix.lower())
    valid = {
        "image/png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": header.startswith(b"\xff\xd8\xff"),
        "image/gif": header.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
        "image/bmp": header.startswith(b"BM"),
        "video/mp4": len(header) >= 12 and header[4:8] == b"ftyp",
        "video/webm": header.startswith(b"\x1a\x45\xdf\xa3"),
    }
    if extension_mime and extension_mime.startswith("image/"):
        # Historical scrapers sometimes store JPEG/WebP content under .png.
        # Serve its actual raster MIME; never admit SVG/HTML/text or reinterpret
        # video extensions as pictures merely because their header matches one.
        for mime, matches in valid.items():
            if mime.startswith("image/") and matches:
                return mime
    elif extension_mime and valid.get(extension_mime):
        return extension_mime
    raise ValueError("Unsupported media extension or file signature")


def _readable(path, expected=None):
    canonical, info = _plain_path(path)
    descriptor = os.open(canonical, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    handle = os.fdopen(descriptor, "rb")
    try:
        opened = os.fstat(handle.fileno())
        if _fingerprint(opened) != _fingerprint(info):
            raise ValueError("Media changed while opening")
        if expected and _fingerprint(opened) != (expected["size"], expected["mtime_ns"],
                                               str(expected["device"]) if str(expected["device"]).startswith("0x") else hex(int(expected["device"])),
                                               str(expected["inode"]) if str(expected["inode"]).startswith("0x") else hex(int(expected["inode"]))):
            raise FileNotFoundError("Registered media is no longer available unchanged")
        # A directory may have been replaced between lstat and open; the handle's
        # identity check plus a second component check rejects that redirection.
        again, current = _plain_path(canonical)
        if again != canonical or _fingerprint(current) != _fingerprint(opened):
            raise ValueError("Media path changed while opening")
        return canonical, opened, handle
    except BaseException:
        handle.close()
        raise


def _preview(run, row):
    return {"available": True, "asset_id": row["id"],
            "url": "/api/runs/" + quote(Path(run).name, safe="") + "/media/" + row["id"],
            "mime": row["mime"], "size": row["size"] or None, "sha256": row["sha256"] or None,
            "origin": row["origin"], "source_state": row["source_state"],
            "cache_state": "pending" if row["source_state"] == "registered_remote" else "ready",
            "cached": row["source_state"] == "registered_local",
            "needs_device": row["source_state"] == "registered_remote"}


def _prepare_local(job_id, kind, path, sha256=None, origin="local_cache"):
    if not KIND.fullmatch(str(kind)):
        raise ValueError("Invalid media kind")
    if sha256 is not None and not SHA256.fullmatch(str(sha256)):
        raise ValueError("Expected SHA-256 must contain 64 hexadecimal characters")
    canonical, info, handle = _readable(path)
    if redact(str(canonical)) != str(canonical):
        handle.close()
        raise ValueError("Media path contains credential-like text")
    with handle:
        mime = _mime(canonical, handle.read(64))
        if info.st_size <= 0:
            raise ValueError("Media file is empty")
        handle.seek(0)
        fingerprint = (str(canonical), *_fingerprint(info))
        with _HASH_LOCK:
            digest = _HASH_CACHE.get(fingerprint)
        if digest is None:
            digest = hashlib.sha256()
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            digest = digest.hexdigest()
            with _HASH_LOCK:
                _HASH_CACHE[fingerprint] = digest
                _HASH_CACHE.move_to_end(fingerprint)
                while len(_HASH_CACHE) > 32768:
                    _HASH_CACHE.popitem(last=False)
        if _fingerprint(os.fstat(handle.fileno())) != _fingerprint(info):
            raise ValueError("Media changed while hashing")
    if sha256 is not None and digest.lower() != str(sha256).lower():
        raise ValueError("Media SHA-256 does not match expected digest")
    identity = json.dumps([str(job_id), str(kind), str(canonical), digest], ensure_ascii=False, separators=(",", ":"))
    asset_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48]
    row = {"id": asset_id, "job_id": str(job_id), "kind": str(kind),
           "local_path": str(canonical), "sha256": digest, "size": info.st_size,
           "mime": mime, "mtime_ns": info.st_mtime_ns, "device": hex(info.st_dev),
           "inode": hex(info.st_ino), "origin": redact(str(origin)), "source_state": "registered_local",
           "remote_path": "", "profile_id": "",
           "registered_at": now()}
    return row


def _register_rows(run, rows, update_jobs):
    previews = [_preview(run, row) for row in rows]
    if not rows:
        return previews
    with connect(run) as db:
        ids = list(dict.fromkeys(row["job_id"] for row in rows))
        jobs = {row["id"]: json.loads(row["media"]) for row in db.execute(
            "SELECT id,media FROM jobs WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)}
        if len(jobs) != len(ids):
            raise FileNotFoundError("A media job does not exist")
        db.executemany(_REGISTRY_INSERT, rows)
        if update_jobs:
            for row, preview in zip(rows, previews):
                media = jobs[row["job_id"]]
                media[row["kind"]] = _attach_preview(media.get(row["kind"], {}), row, preview)
            db.executemany("UPDATE jobs SET media=?,updated_at=? WHERE id=?", [(dumps(media), now(), id) for id, media in jobs.items()])
        _event(db, "media_preview", "Registered media for preview", {"count": len(rows), "jobs": len(ids), "ids": ids[:20]})
    return previews


def _candidate_preview(item):
    if not isinstance(item, dict):
        return {}
    return item if item.get("asset_id") else item.get("preview") or (item.get("extras") or {}).get("preview") or {}


def _path_key(value):
    value = str(value or "").replace("\\", "/")
    return value if value.startswith("/") else value.casefold()


def _attach_preview(previous, row, preview):
    """Keep every scanned candidate and update only this asset's stable preview."""
    array_value = isinstance(previous, list)
    item = copy.deepcopy(previous) if isinstance(previous, dict) else {"scanned_value": copy.deepcopy(previous)}
    extras = dict(item.get("extras", {})) if isinstance(item.get("extras"), dict) else {}
    if array_value:
        candidates = copy.deepcopy(previous)
    else:
        candidates = copy.deepcopy(extras.get("candidates", []))
        if not candidates:
            candidates = copy.deepcopy(item.get("candidates", []))
        if not candidates and isinstance(item.get("scanned_value"), list):
            candidates = copy.deepcopy(item["scanned_value"])
        if not candidates and any(key in item for key in ("path", "file", "status", "present")):
            base = {key: copy.deepcopy(value) for key, value in item.items() if key not in ("extras", "candidates", "scanned_value")}
            if extras.get("preview"):
                base["preview"] = copy.deepcopy(extras["preview"])
            candidates = [base]
    candidates = [{"path": value.get("path", ""), "preview": dict(value)} if isinstance(value, dict) and value.get("asset_id")
                  else dict(value) if isinstance(value, dict) else {"path": value} for value in candidates]
    expected_paths = {_path_key(row[key]) for key in ("remote_path", "local_path") if row.get(key)}
    chosen = None
    for candidate in candidates:
        old_preview = _candidate_preview(candidate)
        if old_preview.get("asset_id") == row["id"] or _path_key(candidate.get("path") or candidate.get("file")) in expected_paths:
            chosen = candidate
            break
    if chosen is None and len(candidates) == 1 and not _candidate_preview(candidates[0]):
        # A prepared/local cache can represent an original single Android path;
        # it must not replace that path with the computer's cache filename.
        chosen = candidates[0]
    if chosen is None:
        chosen = {"path": row.get("remote_path") or row["local_path"], "size": row["size"] or None}
        candidates.append(chosen)
    chosen["preview"] = preview
    candidate_extras = dict(chosen.get("extras", {})) if isinstance(chosen.get("extras"), dict) else {}
    if "preview" in candidate_extras:
        candidate_extras["preview"] = preview
        chosen["extras"] = candidate_extras
    primary_id = (extras.get("preview") or {}).get("asset_id")
    primary = next((candidate for candidate in candidates if _candidate_preview(candidate).get("asset_id") == primary_id), None) if primary_id else None
    if primary is None:
        primary = next((candidate for candidate in candidates if _candidate_preview(candidate).get("available")), None)
    if primary is not None:
        extras["preview"] = _candidate_preview(primary)
        if array_value or isinstance(item.get("scanned_value"), list):
            for key, value in primary.items():
                if key not in ("extras", "preview", "candidates"):
                    item[key] = value
        elif not item.get("path") and primary.get("path"):
            item["path"] = primary["path"]
    # Normalize candidate collections into one UI-visible place. No candidate is
    # dropped across batch boundaries or when another asset becomes cached.
    item.pop("candidates", None)
    extras["candidates"] = candidates
    item["extras"] = extras
    return item


def register_media(run, job_id, kind, path, sha256=None, origin="local_cache", update_job=True):
    """Register an explicit regular local file; preserve original scan paths and status."""
    return register_media_many(run, [{"job_id": job_id, "kind": kind, "path": path, "sha256": sha256, "origin": origin}], update_jobs=update_job)[0]


def register_media_many(run, items, update_jobs=True):
    """Validate/hash explicit local files, then commit at most 500 registrations atomically.

    Duplicate local files reuse a hash only while their full file fingerprint matches.
    With update_jobs=False callers can attach returned previews using upsert_jobs.
    """
    if not isinstance(items, (list, tuple)) or len(items) > 500:
        raise ValueError("A media batch must contain at most 500 explicit items")
    return _register_rows(run, [_prepare_local(**item) for item in items], update_jobs)


def register_remote_media(run, job_id, kind, remote_path, profile_id, size=None, sha256=None, origin="android_snapshot", update_job=True):
    return register_remote_media_many(run, [{"job_id": job_id, "kind": kind, "remote_path": remote_path, "profile_id": profile_id,
                                           "size": size, "sha256": sha256, "origin": origin}], update_jobs=update_job)[0]


def register_remote_media_many(run, items, update_jobs=True):
    """Register scoped snapshot entries, not request-supplied Android file paths."""
    from preview_cache import validate_remote_media
    if not isinstance(items, (list, tuple)) or len(items) > 500:
        raise ValueError("A media batch must contain at most 500 explicit items")
    rows = []
    for item in items:
        if not KIND.fullmatch(str(item.get("kind", ""))):
            raise ValueError("Invalid media kind")
        if item.get("sha256") is not None and not SHA256.fullmatch(str(item["sha256"])):
            raise ValueError("Invalid remote media SHA-256")
        remote = str(item["remote_path"])
        mime = MIME_TYPES.get(Path(remote).suffix.lower())
        if mime is None or redact(remote) != remote:
            raise ValueError("Remote media extension or path is not approved")
        size = item.get("size")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise ValueError("Remote media size must be a nonnegative integer or unknown")
        identity = json.dumps([str(item["job_id"]), str(item["kind"]), remote, str(item["profile_id"])], ensure_ascii=False)
        row = {"id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48], "job_id": str(item["job_id"]),
               "kind": str(item["kind"]), "local_path": "", "sha256": item.get("sha256") or "", "size": size or 0,
               "mime": mime, "mtime_ns": 0, "device": 0, "inode": 0,
               "origin": redact(str(item.get("origin", "android_snapshot"))), "source_state": "registered_remote",
               "remote_path": remote, "profile_id": str(item["profile_id"]), "registered_at": now()}
        validate_remote_media(run, row)
        rows.append(row)
    return _register_rows(run, rows, update_jobs)


def open_registered_media(run, asset_id, materialize_remote=True):
    """Return an open verified file handle; the caller closes and streams it."""
    if not ASSET_ID.fullmatch(str(asset_id)):
        raise FileNotFoundError("Media asset not found")
    with connect(run) as db:
        stored = db.execute("SELECT * FROM media_assets WHERE id=?", (str(asset_id),)).fetchone()
    if stored is None:
        raise FileNotFoundError("Media asset not found")
    row = dict(stored)
    if row["source_state"] == "registered_remote":
        if not materialize_remote:
            return row, None
        from preview_cache import materialize
        local_path = materialize(run, row)
        local = _prepare_local(row["job_id"], row["kind"], local_path, sha256=row["sha256"] or None, origin=row["origin"])
        if row["size"] and local["size"] != row["size"]:
            raise FileNotFoundError("Cached media size differs from snapshot")
        local.update({"id": row["id"], "remote_path": row["remote_path"], "profile_id": row["profile_id"]})
        _register_rows(run, [local], update_jobs=True)
        row = local
    if row["mime"] not in MIME_TYPES.values() or row["source_state"] != "registered_local":
        raise FileNotFoundError("Media asset not available")
    try:
        _, info, handle = _readable(row["local_path"], expected=row)
        if _mime(Path(row["local_path"]), handle.read(64)) != row["mime"]:
            raise FileNotFoundError("Registered media signature changed")
        handle.seek(0)
    except (OSError, ValueError) as exc:
        raise FileNotFoundError("Media asset no longer available unchanged") from exc
    return row, handle


def refresh_preview_availability(run, job):
    """Distinguish scanned presence from playable cache availability in one detail view."""
    previews = []
    for item in job.get("media", {}).values():
        if isinstance(item, list):
            candidates = item
        elif isinstance(item, dict):
            candidates = [item, *(item.get("extras") or {}).get("candidates", []), *item.get("candidates", [])]
        else:
            continue
        for candidate in candidates:
            preview = _candidate_preview(candidate)
            if isinstance(preview, dict) and preview.get("asset_id"):
                previews.append(preview)
    resolved = {}
    for preview in previews:
        asset_id = preview["asset_id"]
        if asset_id in resolved:
            preview.update(resolved[asset_id])
            continue
        try:
            row, handle = open_registered_media(run, asset_id, materialize_remote=False)
            if handle is not None:
                handle.close()
            preview.update(_preview(run, row))
            preview.pop("reason", None)
        except FileNotFoundError:
            preview.update({"available": False, "reason": "本地媒体缓存不存在或已改变，请重新缓存"})
        resolved[asset_id] = dict(preview)
    return job


def byte_range(header, size):
    """Parse a single RFC byte range; reject invalid/multiple/unsatisfiable ranges."""
    if header is None:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", str(header).strip())
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError("Invalid or unsatisfiable byte range")
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if start >= size or end < start:
            raise ValueError("Unsatisfiable byte range")
    else:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("Unsatisfiable byte range")
        start, end = max(0, size - suffix), size - 1
    return start, end
