"""Durable, credential-redacting progress storage (Python standard library only)."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STATUSES = {"pending", "running", "done", "completed", "blocked", "error", "failed", "cancelled"}
MEDIA_TYPES = ("covers", "screenshots", "titlescreens", "marquees", "miximages", "videos")
SECRET_KEY = re.compile(r"^(?:ssid|sspassword|devid|devpassword|token|password|passwd|api[_-]?key|access[_-]?token|authorization|secret|client[_-]?secret)$", re.I)
SECRET_INLINE = re.compile(r'''(?i)\b(ssid|sspassword|devid|devpassword|token|password|passwd|api[_-]?key|access[_-]?token|authorization|secret|client[_-]?secret)(["']?\s*[=:]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s&;,]+)''')
AUTH_INLINE = re.compile(r'''(?i)\bauthorization(["']?\s*[=:]\s*)[^\r\n,;}]+''')
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
_SCHEMA_READY = set()
_SCHEMA_LOCK = threading.Lock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS phases (
 phase TEXT PRIMARY KEY, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
 completed INTEGER, total INTEGER, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, system TEXT NOT NULL DEFAULT '', file TEXT NOT NULL DEFAULT '',
 name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
 missing TEXT NOT NULL DEFAULT '[]', unknown_fields TEXT NOT NULL DEFAULT '[]',
 issues TEXT NOT NULL DEFAULT '[]',
 sources TEXT NOT NULL DEFAULT '[]', media TEXT NOT NULL DEFAULT '{}',
 details TEXT NOT NULL DEFAULT '{}',
 video_kind TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_system_status ON jobs(system,status);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, message TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_assets (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL,
 local_path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
 mime TEXT NOT NULL, mtime_ns INTEGER NOT NULL, device INTEGER NOT NULL,
 inode INTEGER NOT NULL, origin TEXT NOT NULL, source_state TEXT NOT NULL,
 remote_path TEXT NOT NULL DEFAULT '', profile_id TEXT NOT NULL DEFAULT '',
 registered_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS media_assets_job_kind ON media_assets(job_id,kind);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def redact_url(value):
    try:
        parts = urlsplit(value)
        hostname = parts.hostname or ""
        if ":" in hostname:
            hostname = "[" + hostname + "]"
        if parts.port:
            hostname += ":" + str(parts.port)
        netloc = ("[REDACTED]@" if parts.username is not None else "") + hostname
        query = [(k, "[REDACTED]" if SECRET_KEY.match(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        fragment = SECRET_INLINE.sub(lambda m: m[1] + m[2] + "[REDACTED]", parts.fragment)
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), fragment))
    except (ValueError, UnicodeError):
        return "[REDACTED URL]"


def redact(value):
    """Remove credentials before persistence; safe for nested JSON and URL query keys."""
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if SECRET_KEY.match(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = URL_PATTERN.sub(lambda m: redact_url(m[0]), value)
        value = AUTH_INLINE.sub(lambda m: "authorization" + m[1] + "[REDACTED]", value)
        return SECRET_INLINE.sub(lambda m: m[1] + m[2] + "[REDACTED]", value)
    return value


def dumps(value):
    return json.dumps(redact(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _schema_identity(path):
    info = path.stat()
    return str(path.resolve()), info.st_dev, info.st_ino


def _prepare_schema(db, initialize):
    columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    if not columns and initialize:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        return
    for column, default in (("issues", "[]"), ("details", "{}")):
        if columns and column not in columns:
            try:
                db.execute("ALTER TABLE jobs ADD COLUMN " + column + " TEXT NOT NULL DEFAULT '" + default + "'")
                db.commit()
            except sqlite3.OperationalError:
                # Another connected worker may have completed the same online migration.
                if column not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                    raise
    asset_columns = {row[1] for row in db.execute("PRAGMA table_info(media_assets)")}
    if not asset_columns:
        db.execute("""CREATE TABLE IF NOT EXISTS media_assets (
          id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL,
          local_path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
          mime TEXT NOT NULL, mtime_ns INTEGER NOT NULL, device INTEGER NOT NULL,
          inode INTEGER NOT NULL, origin TEXT NOT NULL, source_state TEXT NOT NULL,
          remote_path TEXT NOT NULL DEFAULT '', profile_id TEXT NOT NULL DEFAULT '',
          registered_at TEXT NOT NULL)""")
        asset_columns = {row[1] for row in db.execute("PRAGMA table_info(media_assets)")}
    for column in ("remote_path", "profile_id"):
        if column not in asset_columns:
            try:
                db.execute("ALTER TABLE media_assets ADD COLUMN " + column + " TEXT NOT NULL DEFAULT ''")
                db.commit()
            except sqlite3.OperationalError:
                if column not in {row[1] for row in db.execute("PRAGMA table_info(media_assets)")}:
                    raise
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='media_assets_job_kind'").fetchone():
        db.execute("CREATE INDEX IF NOT EXISTS media_assets_job_kind ON media_assets(job_id,kind)")
    db.commit()


def _startup_io_error(error):
    code = getattr(error, "sqlite_errorcode", None)
    return (code is not None and code & 0xff == sqlite3.SQLITE_IOERR) or str(error).lower() == "disk i/o error"


def _open_database(path, initialize):
    """Retry once only before yielding; never replay an uncertain write transaction."""
    for attempt in range(2):
        db = None
        try:
            db = sqlite3.connect(path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            # This read opens the WAL before a caller starts its transaction.
            db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            identity = _schema_identity(path)
            if identity not in _SCHEMA_READY:
                with _SCHEMA_LOCK:
                    if identity not in _SCHEMA_READY:
                        _prepare_schema(db, initialize)
                        _SCHEMA_READY.add(identity)
            return db
        except BaseException as error:
            if db is not None:
                db.close()
            if attempt == 0 and isinstance(error, sqlite3.OperationalError) and _startup_io_error(error):
                # On Windows a just-stopped process can briefly leave shared
                # WAL handles unavailable. Close everything before one retry.
                time.sleep(0.15)
                continue
            raise


@contextmanager
def connect(run, initialize=False):
    path = Path(run) / "state.sqlite"
    if not initialize and not path.is_file():
        raise FileNotFoundError("Run has no state.sqlite; initialize it first")
    db = _open_database(path, initialize)
    try:
        with db:
            yield db
    finally:
        db.close()


def _get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _put_meta(db, key, value):
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))


def _event(db, kind, message, payload):
    stamp = now()
    cursor = db.execute("INSERT INTO events(kind,message,payload,created_at) VALUES(?,?,?,?)", (redact(str(kind)), redact(str(message)), dumps(payload), stamp))
    _put_meta(db, "updated_at", stamp)
    return cursor.lastrowid


def _merge_details(previous, update):
    merged = dict(previous)
    for key, value in update.items():
        merged[key] = _merge_details(merged[key], value) if isinstance(value, dict) and isinstance(merged.get(key), dict) else value
    return merged


def init_run(run, title="ES-DE resource workflow", connection=None):
    run = Path(run).resolve()
    run.mkdir(parents=True, exist_ok=True)
    config = {"version": 1, "id": run.name, "title": redact(title)}
    with connect(run, initialize=True) as db:
        if _get_meta(db, "created_at"):
            # Repeated initialization must never reset a previous or active run.
            return get_state(run)
        stamp = now()
        for key, value in {**config, "created_at": stamp, "updated_at": stamp, "status": "pending", "phase": "init", "message": "", "connection": redact(connection or {"status": "unknown"})}.items():
            _put_meta(db, key, value)
        _event(db, "initialized", "Run initialized", config)
    config_path = run / "config.json"
    temporary = run / "config.json.tmp"
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(config_path)
    return get_state(run)


def emit_update(run, phase=None, status=None, message=None, completed=None, total=None, phase_status=None, connection=None, **extras):
    """Update real measured progress. Counts omitted by callers retain previous values."""
    for candidate in (status, phase_status):
        if candidate is not None and candidate not in STATUSES:
            raise ValueError("Unsupported status: " + candidate)
    for key, value in (("completed", completed), ("total", total)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(key + " must be a nonnegative integer")
    stamp = now()
    with connect(run) as db:
        current_phase = phase or _get_meta(db, "phase", "init")
        old = db.execute("SELECT * FROM phases WHERE phase=?", (current_phase,)).fetchone()
        count = completed if completed is not None else old["completed"] if old else None
        size = total if total is not None else old["total"] if old else None
        if count is not None and size is not None and count > size:
            raise ValueError("completed cannot exceed total")
        p_status = phase_status or ("done" if status == "completed" else status) or (old["status"] if old else "pending")
        p_message = message if message is not None else old["message"] if old else ""
        db.execute("INSERT INTO phases(phase,status,message,completed,total,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(phase) DO UPDATE SET status=excluded.status,message=excluded.message,completed=excluded.completed,total=excluded.total,updated_at=excluded.updated_at", (redact(current_phase), p_status, redact(p_message), count, size, stamp))
        _put_meta(db, "phase", current_phase)
        if status is not None:
            _put_meta(db, "status", status)
        if message is not None:
            _put_meta(db, "message", message)
        if connection is not None:
            _put_meta(db, "connection", connection)
        if extras:
            existing = _get_meta(db, "details", {})
            existing.update(extras)
            _put_meta(db, "details", existing)
        payload = {"phase": current_phase, "status": status, "phase_status": p_status, "completed": count, "total": size, "connection": connection, **extras}
        return _event(db, "progress", p_message, payload)


def _upsert_job(db, id, system=None, file=None, name=None, status=None, missing=None, unknown=None, sources=None, media=None, video_kind=None, issues=None, details=None):
    if not str(id):
        raise ValueError("Job id cannot be empty")
    if status is not None and status not in STATUSES:
        raise ValueError("Unsupported job status: " + status)
    for key, value in (("missing", missing), ("unknown", unknown), ("sources", sources), ("issues", issues)):
        if value is not None and not isinstance(value, (list, dict)):
            raise ValueError(key + " must be a JSON list or object")
    if media is not None and not isinstance(media, dict):
        raise ValueError("media must be a JSON object")
    if details is not None and not isinstance(details, dict):
        raise ValueError("details must be a JSON object")
    previous = db.execute("SELECT * FROM jobs WHERE id=?", (str(id),)).fetchone()
    if details is not None and previous:
        details = _merge_details(json.loads(previous["details"]), details)
    fields = {"system": system, "file": file, "name": name, "status": status, "missing": missing, "unknown_fields": unknown, "issues": issues, "sources": sources, "media": media, "video_kind": video_kind, "details": details}
    defaults = {"status": "pending", "missing": [], "unknown_fields": [], "issues": [], "sources": [], "media": {}, "details": {}}
    json_fields = {"missing", "unknown_fields", "issues", "sources", "media", "details"}
    resolved = {}
    for key, value in fields.items():
        if value is None:
            value = json.loads(previous[key]) if previous and key in json_fields else previous[key] if previous else defaults.get(key, "")
        resolved[key] = dumps(value) if key in json_fields else redact(str(value))
    db.execute("INSERT INTO jobs(id,system,file,name,status,missing,unknown_fields,issues,sources,media,video_kind,details,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET system=excluded.system,file=excluded.file,name=excluded.name,status=excluded.status,missing=excluded.missing,unknown_fields=excluded.unknown_fields,issues=excluded.issues,sources=excluded.sources,media=excluded.media,video_kind=excluded.video_kind,details=excluded.details,updated_at=excluded.updated_at", (redact(str(id)), *resolved.values(), now()))
    return _event(db, "job", resolved["name"] or str(id), {"id": id, "status": resolved["status"], "system": resolved["system"]})


def upsert_job(run, id, system=None, file=None, name=None, status=None, missing=None, unknown=None, sources=None, media=None, video_kind=None, issues=None, details=None):
    with connect(run) as db:
        return _upsert_job(db, id, system, file, name, status, missing, unknown, sources, media, video_kind, issues, details)


def upsert_jobs(run, jobs):
    """Commit at most 200 jobs atomically; identical validation/redaction and one event per job."""
    if not isinstance(jobs, (list, tuple)) or len(jobs) > 200:
        raise ValueError("A job batch must contain at most 200 job objects")
    with connect(run) as db:
        return [_upsert_job(db, **job) for job in jobs]


def append_event(run, kind, message, payload=None):
    with connect(run) as db:
        return _event(db, kind, message, payload or {})


def _job(row, include_details=True):
    value = dict(row)
    for key in ("missing", "unknown_fields", "issues", "sources", "media", "details"):
        value[key] = json.loads(value[key])
    value["unknown"] = value.pop("unknown_fields")
    if not include_details:
        value.pop("details", None)
    return value


def get_jobs(run, search="", system="", status="", page=1, limit=50, include_details=False):
    page, limit = max(1, int(page)), min(200, max(1, int(limit)))
    where, params = [], []
    if search:
        escaped = str(search).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(name LIKE ? ESCAPE '\\' OR file LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')")
        params.extend(["%" + escaped + "%"] * 3)
    for key, value in (("system", system), ("status", status)):
        if value:
            if key == "status" and value in ("done", "error"):
                where.append("status IN (?,?)")
                params.extend((value, "completed" if value == "done" else "failed"))
                continue
            where.append(key + "=?")
            params.append(value)
    clause = " WHERE " + " AND ".join(where) if where else ""
    with connect(run) as db:
        total = db.execute("SELECT COUNT(*) FROM jobs" + clause, params).fetchone()[0]
        rows = db.execute("SELECT * FROM jobs" + clause + " ORDER BY system,id LIMIT ? OFFSET ?", (*params, limit, (page - 1) * limit)).fetchall()
    return {"items": [_job(row, include_details=include_details) for row in rows], "total": total, "page": page, "limit": limit}


def get_job(run, id):
    """Return one complete item without making ten-thousand-row listings carry descriptions."""
    with connect(run) as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (str(id),)).fetchone()
    if row is None:
        raise FileNotFoundError("Job not found")
    return _job(row)


def get_events(run, after=0, limit=100, tail=False):
    with connect(run) as db:
        bound = min(500, max(1, int(limit)))
        if tail:
            rows = list(reversed(db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (bound,)).fetchall()))
        else:
            rows = db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (max(0, int(after)), bound)).fetchall()
    items = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
    return {"items": items, "last_id": items[-1]["id"] if items else max(0, int(after))}


def get_state(run):
    with connect(run) as db:
        meta = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM meta")}
        phases = [dict(row) for row in db.execute("SELECT * FROM phases ORDER BY rowid")]
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}
        summary = {"total": sum(counts.values()), **{key: counts.get(key, 0) for key in STATUSES}, "unknown": 0, "issues": 0, "missing_media": 0, "video_kinds": {}, "systems": {}}
        for row in db.execute("SELECT system,status,missing,unknown_fields,issues,video_kind FROM jobs"):
            summary["unknown"] += bool(json.loads(row["unknown_fields"]))
            summary["missing_media"] += bool(json.loads(row["missing"]))
            summary["issues"] += bool(json.loads(row["issues"]))
            system = summary["systems"].setdefault(row["system"], {"total": 0, "done": 0, "error": 0})
            system["total"] += 1
            system["done"] += row["status"] in ("done", "completed")
            system["error"] += row["status"] in ("error", "failed")
            if row["video_kind"]:
                kinds = summary["video_kinds"]
                kinds[row["video_kind"]] = kinds.get(row["video_kind"], 0) + 1
        summary["done"] += summary["completed"]
        summary["error"] += summary["failed"]
        last_event_id = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
    return {"run": meta, "phases": phases, "summary": summary, "last_event_id": last_event_id, "media_types": list(MEDIA_TYPES)}
