#!/usr/bin/env python3
"""Local read-only web dashboard and progress CLI for the ES-DE workflow."""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from workbench_store import append_event, emit_update, get_events, get_job, get_jobs, get_state, init_run, redact, upsert_job
from workbench_media import byte_range, open_registered_media, refresh_preview_availability

RUN_ID = re.compile(r"^[\w.-]{1,120}$", re.UNICODE)


def is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_run(root, run_id):
    if not RUN_ID.fullmatch(run_id) or run_id in (".", ".."):
        raise FileNotFoundError("Run not found")
    root = Path(root).resolve()
    if (root / "state.sqlite").is_file() and run_id == root.name:
        return root
    run = (root / run_id).resolve()
    if run.parent != root or not (run / "state.sqlite").is_file():
        raise FileNotFoundError("Run not found")
    return run


def list_runs(root):
    root = Path(root).resolve()
    items = []
    if (root / "state.sqlite").is_file():
        state = get_state(root)
        items.append({**state["run"], "summary": state["summary"]})
    elif root.is_dir():
        for candidate in root.iterdir():
            try:
                run = resolve_run(root, candidate.name)
                state = get_state(run)
                items.append({**state["run"], "summary": state["summary"]})
            except (FileNotFoundError, OSError, ValueError):
                continue
    items.sort(key=lambda run: run.get("updated_at", ""), reverse=True)
    return {"items": items, "total": len(items)}


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, root, assets, access_token=None, heartbeat_interval=10, poll_interval=0.5):
        self.root = Path(root).resolve()
        self.assets = Path(assets).resolve()
        self.access_token = access_token
        self.heartbeat_interval = heartbeat_interval
        self.poll_interval = poll_interval
        super().__init__(address, WorkbenchHandler)


class WorkbenchHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # URL tokens and path contents must never enter access logs.
        return

    def _response(self, status, body, content_type="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(redact(body), ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        if getattr(self, "set_session", False):
            self.send_header("Set-Cookie", "workbench_access=" + self.server.access_token + "; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self, query):
        token = self.server.access_token
        if token is None:
            try:
                request_host = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
            except ValueError:
                request_host = ""
            if not is_loopback(request_host):
                self._response(403, {"error": "Loopback host required"})
                return False
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            cookie_token = cookie["workbench_access"].value if "workbench_access" in cookie else ""
        except Exception:
            cookie_token = ""
        supplied = self.headers.get("X-Workbench-Token", "") or query.get("token", [""])[0] or cookie_token
        if not hmac.compare_digest(token, supplied):
            self._response(401, {"error": "Access token required"})
            return False
        self.set_session = bool(query.get("token", [""])[0])
        return True

    @staticmethod
    def _int(query, key, default):
        try:
            return int(query.get(key, [str(default)])[0])
        except (ValueError, TypeError):
            raise ValueError(key + " must be an integer")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        self._response(405, {"error": "Dashboard is read-only; use the progress CLI"})

    do_PUT = do_POST
    do_DELETE = do_POST

    def do_GET(self):
        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query, keep_blank_values=True)
            if not self._authorized(query):
                return
            path = unquote(url.path)
            if path == "/api/health":
                self._response(200, {"service": "es-de-resource-workbench", "version": 1, "pid": os.getpid(), "root": str(self.server.root), "host": self.server.server_address[0], "port": self.server.server_address[1], "instance_id": os.environ.get("ESDE_WORKBENCH_INSTANCE_ID", "")})
                return
            if path == "/api/runs":
                self._response(200, list_runs(self.server.root))
                return
            if path.startswith("/api/runs/"):
                # Split before decoding: encodeURIComponent IDs contain %2F for
                # nested ROM filenames and must remain one opaque lookup key.
                segments = [unquote(part) for part in url.path.strip("/").split("/")]
                if len(segments) not in (3, 4, 5):
                    raise FileNotFoundError("Endpoint not found")
                run = resolve_run(self.server.root, segments[2])
                endpoint = segments[3] if len(segments) == 4 else "state"
                if len(segments) == 5:
                    if segments[3] == "jobs":
                        self._response(200, {"job": refresh_preview_availability(run, get_job(run, segments[4]))})
                    elif segments[3] == "media":
                        self._media(run, segments[4])
                    else:
                        raise FileNotFoundError("Endpoint not found")
                    return
                if endpoint == "state":
                    self._response(200, get_state(run))
                elif endpoint == "jobs":
                    self._response(200, get_jobs(run, search=query.get("search", [""])[0], system=query.get("system", [""])[0], status=query.get("status", [""])[0], page=self._int(query, "page", 1), limit=self._int(query, "limit", 50), include_details=query.get("include_details", ["0"])[0] == "1"))
                elif endpoint == "events":
                    self._response(200, get_events(run, after=self._int(query, "after", 0), limit=self._int(query, "limit", 100), tail=query.get("tail", ["0"])[0] == "1"))
                elif endpoint == "stream":
                    if self.command == "HEAD":
                        self._response(405, {"error": "SSE requires GET"})
                    else:
                        self._stream(run, query)
                else:
                    raise FileNotFoundError("Endpoint not found")
                return
            if path.startswith("/api/"):
                raise FileNotFoundError("Endpoint not found")
            self._static(path)
        except FileNotFoundError:
            self._response(404, {"error": "Not found"})
        except (ValueError, TypeError):
            self._response(400, {"error": "Invalid request parameters"})
        except (ConnectionResetError, BrokenPipeError):
            return
        except Exception:
            self._response(500, {"error": "Unable to read workflow state"})

    def _static(self, path):
        relative = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        if relative.startswith("assets/"):
            relative = relative[len("assets/"):]
        candidate = (self.server.assets / relative).resolve()
        try:
            candidate.relative_to(self.server.assets)
        except ValueError:
            raise FileNotFoundError("Not found")
        if candidate.suffix.lower() not in {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".woff2"} or not candidate.is_file():
            raise FileNotFoundError("Not found")
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix in (".html", ".css", ".js", ".svg"):
            content_type += "; charset=utf-8"
        self._response(200, candidate.read_bytes(), content_type)

    def _media(self, run, asset_id):
        try:
            registered, handle = open_registered_media(run, asset_id)
        except FileNotFoundError:
            raise
        except ImportError:
            self._response(503, {"error": "Media cache service unavailable", "code": "cache_unavailable"})
            return
        except Exception as exc:
            if type(exc).__name__ != "PreviewUnavailable":
                raise
            code = str(getattr(exc, "code", "unavailable"))
            status = (404 if code in ("missing", "not_found", "remote_media_unavailable", "unregistered_media")
                      else 409 if code in ("changed", "mismatch", "media_changed", "media_hash_mismatch")
                      else 413 if code == "media_size_limit"
                      else 400 if code.startswith("invalid_") or code == "unsupported_media" else 503)
            self._response(status, {"error": redact(str(getattr(exc, "message", "媒体暂不可预览"))), "code": code})
            return
        with handle:
            size = registered["size"]
            try:
                selected = byte_range(self.headers.get("Range"), size)
            except ValueError:
                self._response(416, {"error": "Requested media range is not satisfiable"},
                               headers={"Content-Range": "bytes */" + str(size), "Accept-Ranges": "bytes"})
                return
            start, end = selected if selected is not None else (0, size - 1)
            self.send_response(206 if selected is not None else 200)
            self.send_header("Content-Type", registered["mime"])
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Disposition", "inline")
            if selected is not None:
                self.send_header("Content-Range", "bytes " + str(start) + "-" + str(end) + "/" + str(size))
            if getattr(self, "set_session", False):
                self.send_header("Set-Cookie", "workbench_access=" + self.server.access_token + "; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            if self.command == "HEAD":
                return
            handle.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    self.close_connection = True
                    return
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _sse(self, kind, data, event_id=None):
        lines = ("id: " + str(event_id) + "\n" if event_id is not None else "")
        lines += "event: " + kind + "\n"
        lines += "data: " + json.dumps(redact(data), ensure_ascii=False, separators=(",", ":")) + "\n\n"
        self.wfile.write(lines.encode("utf-8"))
        self.wfile.flush()

    def _stream(self, run, query):
        snapshot = get_state(run)
        fallback = snapshot["last_event_id"]
        if self.headers.get("Last-Event-ID"):
            fallback = int(self.headers["Last-Event-ID"])
        last_id = max(0, self._int(query, "after", fallback))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.close_connection = True
        try:
            self._sse("snapshot", snapshot)
            heartbeat = time.monotonic()
            while True:
                batch = get_events(run, after=last_id, limit=100)
                for event in batch["items"]:
                    self._sse("update", {"event_id": event["id"], **event}, event["id"])
                    last_id = event["id"]
                if time.monotonic() - heartbeat >= self.server.heartbeat_interval:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                time.sleep(self.server.poll_interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except Exception:
            try:
                self._sse("unavailable", {"error": "Workflow state unavailable"})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return


def build_server(root, assets, host="127.0.0.1", port=8765, access_token=None, **kwargs):
    if not is_loopback(host) and access_token is None:
        access_token = secrets.token_urlsafe(32)
    if ":" in host:
        import socket
        class IPv6WorkbenchServer(WorkbenchServer):
            address_family = socket.AF_INET6
        server_type = IPv6WorkbenchServer
    else:
        server_type = WorkbenchServer
    return server_type((host, port), root, assets, access_token=access_token, **kwargs)


def json_argument(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("Expected valid JSON") from exc


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Initialize a durable run (safe to repeat)")
    init.add_argument("--run", required=True)
    init.add_argument("--title", default="ES-DE resource workflow")
    init.add_argument("--connection", type=json_argument)
    serve = sub.add_parser("serve", help="Start the read-only live workbench")
    serve.add_argument("--root", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--assets", default=str(Path(__file__).resolve().parent.parent / "assets" / "workbench"))
    update = sub.add_parser("update", help="Publish observed phase progress")
    update.add_argument("--run", required=True)
    update.add_argument("--phase")
    update.add_argument("--status", choices=sorted(__import__("workbench_store").STATUSES))
    update.add_argument("--phase-status", choices=sorted(__import__("workbench_store").STATUSES))
    update.add_argument("--message")
    update.add_argument("--completed", type=int)
    update.add_argument("--total", type=int)
    update.add_argument("--connection", type=json_argument)
    job = sub.add_parser("job", help="Upsert one ROM/media/metadata work item")
    job.add_argument("--run", required=True)
    job.add_argument("--id", required=True)
    for key in ("system", "file", "name", "video-kind"):
        job.add_argument("--" + key)
    job.add_argument("--status", choices=sorted(__import__("workbench_store").STATUSES))
    for key in ("missing", "unknown", "issues", "sources", "media", "details"):
        job.add_argument("--" + key, type=json_argument)
    state = sub.add_parser("state", help="Print state and summary JSON")
    state.add_argument("--run", required=True)
    event = sub.add_parser("event", help="Publish a durable event")
    event.add_argument("--run", required=True)
    event.add_argument("--kind", required=True)
    event.add_argument("--message", required=True)
    event.add_argument("--payload", type=json_argument)
    return cli


def main(argv=None):
    args = vars(parser().parse_args(argv))
    command = args.pop("command")
    try:
        if command == "init":
            result = init_run(**args)
        elif command == "update":
            result = {"event_id": emit_update(**args)}
        elif command == "job":
            result = {"event_id": upsert_job(**args)}
        elif command == "state":
            result = get_state(**args)
        elif command == "event":
            result = {"event_id": append_event(**args)}
        else:
            server = build_server(**args, access_token=os.environ.get("ESDE_WORKBENCH_TOKEN") or None)
            port = server.server_address[1]
            shown_host = args["host"]
            if shown_host in ("0.0.0.0", "::"):
                shown_host = "127.0.0.1" if shown_host == "0.0.0.0" else "[::1]"
            elif ":" in shown_host:
                shown_host = "[" + shown_host + "]"
            url = "http://" + shown_host + ":" + str(port) + "/"
            if server.access_token:
                url += "?token=" + server.access_token
            print(json.dumps({"url": url, "host": args["host"], "port": port, "authenticated": bool(server.access_token)}, ensure_ascii=False), flush=True)
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        print(json.dumps(redact(result), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": redact(str(exc))}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
