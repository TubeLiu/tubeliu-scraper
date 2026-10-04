#!/usr/bin/env python3
"""Explicit ScreenScraper candidate queries; never assume a candidate is the correct game.

Official contract: https://www.screenscraper.fr/webapi2.php (checked 2026-10-03).
Developer and user credential pairs use current-user local storage,
with complete environment pairs taking precedence. ``check`` never uses the network.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import screenscraper_credentials as credential_store
from workbench_store import SECRET_KEY, append_event, connect, emit_update, init_run, now, redact, upsert_job

API = "https://api.screenscraper.fr/api2/"
DOCUMENTATION = "https://www.screenscraper.fr/webapi2.php"
APPLICATION = "https://www.screenscraper.fr/forumsujets.php?frub=12&numpage=0"
USER_REGISTRATION = "https://www.screenscraper.fr/membreinscription.php"
DEVELOPER_FIELDS = ("SCREENSCRAPER_DEVID", "SCREENSCRAPER_DEVPASSWORD")
USER_FIELDS = ("SCREENSCRAPER_SSID", "SCREENSCRAPER_SSPASSWORD")


class Blocked(RuntimeError):
    pass


class MissingCredentials(Blocked):
    """Only this optional provider needs attention; other workflow steps can continue."""
    def __init__(self, report):
        self.report = report
        super().__init__(report["prompt"])


def credential_check(env=None):
    """Report configuration without sending a request or exposing credential values."""
    status = credential_store.credential_status(env=env)
    missing = status.get("missing", [])
    ready = bool(status.get("ready"))
    user_configured = bool(status.get("user_configured"))
    state = "ready" if ready else "needs_developer_credentials" if user_configured else "needs_credentials"
    prompt = "ScreenScraper 凭据已配置，将自动复用；本次仅检查配置齐备，未在线验证。"
    if not ready:
        prompt = (
            "ScreenScraper 用户账号已配置，将自动复用。" if user_configured else
            "尚未配置 ScreenScraper 账号；可在官方注册页面创建账号，再运行 configure --user-only 保存账号密码，后续自动复用。"
        ) + (
            "此 API 还需要软件的开发者 ID（devid）和开发者密码（devpassword），普通用户账号不能替代。"
            "缺少应用授权时，可在官方开发者论坛申请；账号已保存并不表示 API 已可用。"
            "也可以先跳过，继续其他来源与本地处理。"
        )
        if status.get("error"):
            prompt += " 当前配置存在问题：" + str(status["error"])
        if missing:
            prompt += " 缺少：" + "、".join(missing) + "。"
    return {
        "provider": "screenscraper", "status": state,
        "ready_for_api": ready, "network_checked": False, "credential_verified": False,
        "configured": bool(status.get("configured")),
        "storage": status.get("storage", "none"), "protection": status.get("protection", "none"),
        "developer_configured": bool(status.get("developer_configured")),
        "user_configured": user_configured,
        "missing_environment_variables": missing,
        "required_environment_variables": list(DEVELOPER_FIELDS),
        "optional_environment_variables": list(USER_FIELDS),
        "developer_application_url": APPLICATION, "documentation_url": DOCUMENTATION,
        "user_registration_url": USER_REGISTRATION, "prompt": prompt,
        "limits_without_credentials": [
            "不能调用 ScreenScraper API 批量匹配和补齐游戏资料、图片及视频。",
            "公开来源与已有资源不保证覆盖同等范围，部分游戏可能保留缺项。",
        ],
        "available_without_credentials": [
            "扫描游戏、已有资料和媒体", "工作台预览与整理已有资源",
            "使用公开来源补充资料", "备份、校验并回写到设备",
        ],
        "can_skip": True, "blocks_entire_workflow": False,
        "workbench_access": "Web 工作台的访问令牌由启动器自动生成，无需申请，与 ScreenScraper 凭据无关。",
    }


def credentials(env=None):
    try:
        values = credential_store.resolve_credentials(env=env)
    except credential_store.CredentialError:
        raise MissingCredentials(credential_check(env)) from None
    developer = {"devid": values.get(DEVELOPER_FIELDS[0], ""), "devpassword": values.get(DEVELOPER_FIELDS[1], "")}
    if not all(developer.values()):
        raise MissingCredentials(credential_check(env))
    user = {"ssid": values.get(USER_FIELDS[0], ""), "sspassword": values.get(USER_FIELDS[1], "")}
    if any(user.values()) and not all(user.values()):
        raise MissingCredentials(credential_check(env))
    return {**developer, **(user if all(user.values()) else {}), "softname": "es-de-resource-workbench"}


def configure_credentials(stdin_json=False, user_only=False):
    """Read secrets from hidden interactive input or explicit stdin, never CLI arguments."""
    if stdin_json:
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise ValueError("Credential input exceeds the allowed size")
        try:
            values = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("Credential input must be a JSON object containing the supported SCREENSCRAPER_ fields") from None
        if user_only and (not isinstance(values, dict) or set(values) != set(USER_FIELDS)):
            raise ValueError("--user-only requires only the complete SCREENSCRAPER_SSID and SCREENSCRAPER_SSPASSWORD pair")
    else:
        if not sys.stdin.isatty():
            raise ValueError("Use an interactive terminal for hidden credential input, or configure --stdin with a JSON object on standard input")
        if user_only:
            values = {
                USER_FIELDS[0]: getpass.getpass("ScreenScraper 账号（不回显）："),
                USER_FIELDS[1]: getpass.getpass("ScreenScraper 密码（不回显）："),
            }
        else:
            values = {
                DEVELOPER_FIELDS[0]: getpass.getpass("ScreenScraper 开发者 ID（devid，不回显）："),
                DEVELOPER_FIELDS[1]: getpass.getpass("ScreenScraper 开发者密码（devpassword，不回显）："),
            }
            user = getpass.getpass("普通用户账号（可选，直接回车跳过，不回显）：")
            if user:
                values[USER_FIELDS[0]] = user
                values[USER_FIELDS[1]] = getpass.getpass("普通用户密码（不回显）：")
    credential_store.save_credentials(values)
    result = credential_check()
    result["message"] = "ScreenScraper 凭据已保存到当前用户的本机配置，后续自动复用；未在线验证。"
    if result["user_configured"] and not result["ready_for_api"]:
        result["message"] = "ScreenScraper 账号密码已保存，后续自动复用；此 API 仍缺少软件的开发者授权，可先继续其他来源与本地处理。"
    return result


def safe_url(url):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        allowed = host == "screenscraper.fr" or host.endswith(".screenscraper.fr")
        if parts.scheme != "https" or not allowed or parts.username or parts.password or (parts.port is not None and parts.port != 443):
            raise ValueError("Only HTTPS ScreenScraper media/API URLs are accepted")
        return parts
    except ValueError:
        # urlsplit can include the complete netloc in Unicode-validation errors.
        raise ValueError("Only HTTPS ScreenScraper media/API URLs are accepted") from None


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def default_open(url, timeout=45):
    safe_url(url)
    return urllib.request.build_opener(RestrictedRedirect()).open(urllib.request.Request(url, headers={"User-Agent": "es-de-resource-workbench/1.0"}), timeout=timeout)


def read_bounded(response, max_bytes, handle=None):
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            if int(length) > max_bytes:
                raise ValueError("Response exceeds the configured byte limit")
        except (TypeError, ValueError) as exc:
            raise ValueError("Response size header is invalid or exceeds the configured limit") from exc
    chunks, size, digest = [], 0, hashlib.sha256()
    while True:
        chunk = response.read(min(65536, max_bytes - size + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("Response exceeded the configured byte limit")
        digest.update(chunk)
        if handle is None:
            chunks.append(chunk)
        else:
            handle.write(chunk)
    return (b"".join(chunks) if handle is None else None), size, digest.hexdigest()


def hash_rom(path, run=None):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("ROM hashing requires the selected regular file, not a symlink")
    path = path.resolve()
    if not path.is_file():
        raise ValueError("ROM must be a regular local file; folder hashing needs an explicitly selected ROM file")
    before = path.stat()
    md5, sha1, sha256, crc, processed, total = hashlib.md5(), hashlib.sha1(), hashlib.sha256(), 0, 0, before.st_size
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
            crc = zlib.crc32(chunk, crc)
            processed += len(chunk)
            if run and (processed % (64 * 1024 * 1024) == 0 or processed == total):
                emit_update(run, phase="hash", status="running", completed=processed, total=total, message="Hashing explicitly selected ROM file")
    after = path.stat()
    if processed != total or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("ROM changed while hashing")
    if run:
        emit_update(run, phase="hash", status="running", phase_status="done", completed=processed, total=total, message="Selected ROM hashes measured")
    return {"romnom": path.name, "romtaille": total, "md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "sha256": sha256.hexdigest(), "crc": format(crc & 0xffffffff, "08X"), "romtype": "iso" if path.suffix.lower() == ".iso" else "rom"}


def _text(value):
    if isinstance(value, dict):
        return value.get("text") or value.get("nom") or value.get("name") or ""
    return str(value) if value is not None else ""


def localized(value, preferences):
    if isinstance(value, dict):
        for preference in preferences:
            for key, item in value.items():
                if key.lower() in (preference, "nom_" + preference, "synopsis_" + preference):
                    return _text(item)
    if isinstance(value, list):
        for preference in preferences:
            for item in value:
                if isinstance(item, dict) and str(item.get("langue") or item.get("region") or "").lower() == preference:
                    return _text(item)
    return ""


def normalize_game(game):
    chinese_name = localized(game.get("noms", {}), ("cn", "zh", "tw"))
    chinese_description = localized(game.get("synopsis", {}), ("zh", "cn", "tw"))
    fallback_name = _text(game.get("nom")) or localized(game.get("noms", {}), ("wor", "us", "eu", "jp", "ss"))
    media = []
    def walk(value, prefix=""):
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("url"):
                    media.append({"type": item.get("type", prefix), "region": item.get("region", ""), "format": item.get("format", ""), "url": redact(item["url"]), "identity_confirmed": False})
                else:
                    walk(item, prefix)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, key)
        elif isinstance(value, str) and value.startswith("https://"):
            media.append({"type": prefix, "url": redact(value), "identity_confirmed": False})
    walk(game.get("medias", {}))
    return redact({"provider": "screenscraper", "provider_game_id": game.get("id"), "system": game.get("systeme", {}), "name": chinese_name or fallback_name, "chinese_name": chinese_name or None, "description_zh": chinese_description or None, "developer": _text(game.get("developpeur")), "publisher": _text(game.get("editeur")), "players": _text(game.get("joueurs")), "dates": game.get("dates", {}), "rom_evidence": game.get("rom", game.get("roms", {})), "media_candidates": media, "identity_confirmed": False, "review_required": True, "unknown": [key for key, value in (("chinese_name", chinese_name), ("description_zh", chinese_description)) if not value], "source_documentation": DOCUMENTATION})


def query(system_id, rom_fields=None, search_name=None, *, env=None, opener=None, max_bytes=12000000, identity_key_path=None):
    if not str(system_id).isdigit() or int(system_id) <= 0:
        raise ValueError("system_id must be the official positive numeric ScreenScraper system ID")
    auth = credentials(env)
    if search_name:
        endpoint, fields = "jeuRecherche.php", {"recherche": search_name}
    else:
        fields = rom_fields or {}
        if not any(fields.get(key) for key in ("crc", "md5", "sha1")) or not isinstance(fields.get("romtaille"), int) or fields["romtaille"] < 0:
            raise ValueError("ROM lookup requires a hash and byte size; use explicit name search for uncertain identities")
        endpoint = "jeuInfos.php"
    allowed = {"crc", "md5", "sha1", "romnom", "romtaille", "romtype", "recherche"}
    parameters = {**auth, "output": "json", "systemeid": str(system_id), **{key: value for key, value in fields.items() if key in allowed and value is not None and value != ""}}
    url = API + endpoint + "?" + urlencode(parameters)
    try:
        with (opener or default_open)(url, timeout=45) as response:
            safe_url(response.geturl())
            raw, _, _ = read_bounded(response, max_bytes)
        payload = json.loads(raw.decode("utf-8-sig"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        # HTTP exception objects contain authenticated URLs; never include their text.
        raise Blocked("ScreenScraper request unavailable, limited, or returned invalid JSON; retry after checking provider status and credentials") from None
    if not isinstance(payload, dict):
        raise Blocked("ScreenScraper returned an unsupported response; no candidate was accepted")
    response = payload.get("response", payload)
    if not isinstance(response, dict):
        raise Blocked("ScreenScraper returned an unsupported response; no candidate was accepted")
    games = response.get("jeux") if search_name else response.get("jeu")
    if isinstance(games, dict):
        games = [games]
    if not isinstance(games, list):
        games = []
    result = {"provider": "screenscraper", "endpoint": endpoint, "queried_at": now(), "query": redact({"systemeid": system_id, **fields}), "candidates": [normalize_game(game) for game in games if isinstance(game, dict)], "identity_confirmed": False, "review_required": True}
    # A test/injected opener is not an authenticated online provider. Explicit
    # fixture keys belong to isolated tests and must never load the user's key.
    if opener is None or identity_key_path is not None:
        from identity import seal_provider_report
        result["identity_source"] = seal_provider_report(result, key_path=identity_key_path)
    return result


def download(url, out, relative, *, env=None, opener=None, max_bytes=100000000, identity_key_path=None):
    parts = safe_url(url)
    parameters = dict(parse_qsl(parts.query, keep_blank_values=True))
    if any(SECRET_KEY.match(key) for key in parameters):
        auth = credentials(env)
        for key in list(parameters):
            if SECRET_KEY.match(key):
                if key not in auth:
                    raise Blocked("Selected media URL requires unsupported credentials")
                parameters[key] = auth[key]
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(parameters), ""))
    root = Path(out).resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Download output must stay inside --out") from exc
    if target == root or target.exists() or Path(str(target) + ".provenance.json").exists():
        raise ValueError("Output already exists or is not a file destination")
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".esde-download-") as scratch:
            temporary = Path(scratch) / "download.part"
            with (opener or default_open)(url, timeout=45) as response, temporary.open("wb") as handle:
                safe_url(response.geturl())
                content_type = response.headers.get("Content-Type", "").lower()
                if content_type.startswith("text/") or "json" in content_type:
                    raise ValueError("Provider returned text instead of selected media")
                _, size, digest = read_bounded(response, max_bytes, handle)
            if not size:
                raise ValueError("Provider returned empty media")
            with temporary.open("rb") as source, target.open("xb") as destination:
                created = True
                shutil.copyfileobj(source, destination)
    except (urllib.error.URLError, TimeoutError, OSError):
        if created:
            target.unlink(missing_ok=True)
        raise Blocked("Selected media could not be downloaded; partial output was not accepted") from None
    provenance = redact({"provider": "screenscraper", "source_url": url, "downloaded_at": now(), "path": str(target), "bytes": size, "sha256": digest, "status": "downloaded_unverified", "identity_confirmed": False, "technical_verified": False})
    if opener is None or identity_key_path is not None:
        from identity import seal_download
        provenance["identity_download"] = seal_download(provenance, key_path=identity_key_path)
    Path(str(target) + ".provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    return provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", aliases=["status", "doctor"], help="Check local/saved credentials without a network request; missing credentials do not block other sources")
    configure_cli = sub.add_parser("configure", help="Save credentials for the current user; hidden interactive input by default")
    configure_cli.add_argument("--stdin", action="store_true", dest="stdin_json", help="Read one JSON object from standard input, never from command-line credential arguments")
    configure_cli.add_argument("--user-only", action="store_true", help="Save only the account/password pair and retain any saved developer credentials")
    sub.add_parser("forget", help="Remove saved credentials; environment overrides must be unset separately")
    query_cli = sub.add_parser("query")
    query_cli.add_argument("--system-id", required=True)
    query_cli.add_argument("--rom")
    query_cli.add_argument("--search-name")
    query_cli.add_argument("--name")
    query_cli.add_argument("--size", type=int)
    for key in ("md5", "sha1", "crc"):
        query_cli.add_argument("--" + key)
    query_cli.add_argument("--save", help="Save only redacted candidates JSON")
    download_cli = sub.add_parser("download")
    download_cli.add_argument("--url", required=True, help="Explicitly selected candidate URL; credential parameters are supplied from environment")
    download_cli.add_argument("--out", required=True)
    download_cli.add_argument("--relative", required=True)
    download_cli.add_argument("--max-bytes", type=int, default=100000000)
    for cli in (query_cli, download_cli):
        cli.add_argument("--run")
        cli.add_argument("--job-id")
        cli.add_argument("--identity-key", help="Explicit trusted signing key path; otherwise use the current user's protected local key")
    args = parser.parse_args(argv)
    phase = "identify" if args.command == "query" else "download"
    try:
        if args.command in ("check", "status", "doctor"):
            print(json.dumps(credential_check(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "configure":
            print(json.dumps(configure_credentials(args.stdin_json, args.user_only), ensure_ascii=False, indent=2))
            return 0
        if args.command == "forget":
            credential_store.forget_credentials()
            result = credential_check()
            result["message"] = "已移除本机保存的凭据；环境变量中的配置仍需单独清除。"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        # Check required authentication before touching run status or hashing a ROM.
        # Missing this optional source should leave other workflow phases runnable.
        if args.command == "query":
            credentials()
        elif any(SECRET_KEY.match(key) for key, _ in parse_qsl(safe_url(args.url).query, keep_blank_values=True)):
            credentials()
        if args.run:
            init_run(args.run)
            emit_update(args.run, phase=phase, status="running", completed=0, total=1, message="Explicit ScreenScraper " + args.command)
        if args.command == "query":
            fields = hash_rom(args.rom, args.run) if args.rom else {"romnom": args.name, "romtaille": args.size, "md5": args.md5, "sha1": args.sha1, "crc": args.crc, "romtype": "rom"}
            result = query(args.system_id, fields, args.search_name, identity_key_path=args.identity_key)
            if args.save:
                Path(args.save).parent.mkdir(parents=True, exist_ok=True)
                Path(args.save).write_text(json.dumps(redact(result), ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            result = download(args.url, args.out, args.relative, max_bytes=args.max_bytes, identity_key_path=args.identity_key)
        if args.run:
            if args.job_id:
                with connect(args.run) as db:
                    row = db.execute("SELECT sources FROM jobs WHERE id=?", (args.job_id,)).fetchone()
                    sources = json.loads(row[0]) if row else []
                if not isinstance(sources, list):
                    sources = [sources]
                sources.append(redact(result))
                upsert_job(args.run, args.job_id, sources=sources)
            emit_update(args.run, phase=phase, status="running", phase_status="done", completed=1, total=1, message="Provider result received; identity requires review")
        print(json.dumps(redact(result), ensure_ascii=False, indent=2))
        return 0
    except MissingCredentials as exc:
        if args.run:
            init_run(args.run)
            append_event(args.run, "provider_unavailable", exc.report["prompt"], exc.report)
        print(json.dumps({"status": "blocked", "error": exc.report["prompt"], "credential_guidance": exc.report}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (Blocked, ValueError, OSError, credential_store.CredentialError) as exc:
        status = "blocked" if isinstance(exc, Blocked) else "error"
        if getattr(args, "run", None):
            init_run(args.run)
            emit_update(args.run, phase=phase, status=status, message=redact(str(exc)))
        print(json.dumps({"status": status, "error": redact(str(exc))}, ensure_ascii=False), file=sys.stderr)
        return 3 if status == "blocked" else 2


if __name__ == "__main__":
    sys.exit(main())
