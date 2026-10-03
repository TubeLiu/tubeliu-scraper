#!/usr/bin/env python3
"""Start or reuse a verified, read-only ES-DE workbench without a terminal window.

The local connection.json contains a workbench access link, never scraping-account
credentials. Keep it in a private local directory and do not share that file.
Importing this module has no process, browser, filesystem, or device side effects.
"""
from __future__ import annotations

import argparse
import csv
import io
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SERVICE = "es-de-resource-workbench"
RUNTIME_FOLDER = ".workbench-runtime"
PRIVATE_NOTE = "connection.json 含工作台访问链接，不含刮削账号凭据；请保留在本机私人目录，勿分享此文件。"


class NoRedirect(HTTPRedirectHandler):
    """Never forward a local authentication header to another endpoint."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def positive_port(value):
    try:
        port = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("端口必须是 0 到 65535 的整数。")
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须是 0 到 65535 的整数。")
    return port


def available_port(host, preferred=8765, attempts=30):
    """Probe availability without connecting to, altering, or stopping occupants."""
    preferred = positive_port(preferred)
    candidates = [0] if preferred == 0 else range(preferred, min(65536, preferred + attempts))
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return probe.getsockname()[1]
    raise OSError("附近端口均已占用；请用 --port 指定另一个端口。未停止任何已有服务。")


def ipv4_addresses():
    """Enumerate this computer's IPv4 addresses; no network request is sent."""
    candidates = set()
    try:
        for answer in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            candidates.add(answer[4][0])
    except OSError:
        pass
    addresses = []
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        # 198.18/15 is frequently a VPN's synthetic adapter, not a Wi-Fi address.
        synthetic = address.version == 4 and address in ipaddress.ip_network("198.18.0.0/15")
        if address.version == 4 and not (address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved or synthetic):
            addresses.append(str(address))
    return sorted(addresses, key=lambda address: int(ipaddress.ip_address(address)))


def access_url(host, port, token=""):
    url = "http://" + host + ":" + str(port) + "/"
    return url + ("?" + urlencode({"token": token}) if token else "")


def health(port, token="", timeout=1.0):
    """Read an authenticated, loopback-only health response without proxies."""
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Workbench-Token"] = token
    request = Request("http://127.0.0.1:" + str(positive_port(port)) + "/api/health", headers=headers)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            body = response.read(65537)
        if len(body) > 65536:
            return None
        value = json.loads(body.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, HTTPError, URLError, HTTPException, ValueError, UnicodeError):
        return None


def health_matches(value, runs, port, instance_id, pid, lan):
    if not value or not instance_id or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    expected_host = "0.0.0.0" if lan else "127.0.0.1"
    try:
        return (
            value.get("service") == SERVICE
            and value.get("version") == 1
            and value.get("instance_id") == instance_id
            and value.get("pid") == pid
            and canonical(value.get("root", "")) == canonical(runs)
            and value.get("port") == port
            and value.get("host") == expected_host
        )
    except (ValueError, TypeError, OSError):
        return False


def harden_runtime(runtime):
    """Limit our newly created runtime directory to the current local user."""
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        runtime.chmod(0o700)
        return True, "运行目录仅允许当前用户访问。"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True, creationflags=flags, timeout=5)
        row = next(csv.reader(io.StringIO(identity.stdout.strip())))
        sid = row[-1].strip()
        if not sid.startswith("S-1-") or any(char not in "S-0123456789" for char in sid):
            raise ValueError("无法确定当前用户 SID")
        # Remove pre-existing explicit grants before replacing inherited grants;
        # /grant:r alone would leave any earlier Everyone/other-user entries.
        subprocess.run(["icacls", str(runtime), "/reset"], capture_output=True, check=True, creationflags=flags, timeout=5)
        subprocess.run(["icacls", str(runtime), "/inheritance:r", "/grant:r", "*" + sid + ":(OI)(CI)F"], capture_output=True, check=True, creationflags=flags, timeout=5)
        return True, "运行目录已限制为 Windows 当前用户。"
    except (OSError, subprocess.SubprocessError, ValueError, StopIteration):
        return False, "无法验证当前用户目录权限；请将运行目录保留在本机私人账户目录，不要放入共享或同步目录。"


def write_connection(runtime, connection):
    path = runtime / "connection.json"
    temporary = runtime / (".connection-" + uuid.uuid4().hex + ".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(connection, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def load_connection(runtime):
    try:
        path = runtime / "connection.json"
        if path.stat().st_size > 65536:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def verified_existing(connection, runs):
    if not connection or connection.get("service") != SERVICE:
        return False
    try:
        if canonical(connection.get("runs", "")) != canonical(runs):
            return False
        port = positive_port(connection.get("port"))
        if not port:
            return False
        lan = connection.get("lan") is True
        token = connection.get("access_token", "")
        if not isinstance(token, str) or (lan and len(token) < 24):
            return False
        return health_matches(health(port, token), runs, port, connection.get("instance_id"), connection.get("pid"), lan)
    except (ValueError, TypeError, argparse.ArgumentTypeError):
        return False


def launch(runs, port=8765, lan=False, open_browser=False, ready_timeout=15.0):
    """Launch a verified server; return only explicit workbench connection fields."""
    runs = Path(runs).resolve()
    if not runs.is_dir():
        raise ValueError("任务目录不存在；请先创建 --runs 指向的本机任务目录。")
    requested_port = positive_port(port)
    runtime = runs / RUNTIME_FOLDER
    if canonical(runtime.resolve().parent) != canonical(runs):
        raise ValueError("运行目录链接到了任务目录之外；请使用本机任务目录中的独立运行目录。")
    restricted, permission_note = harden_runtime(runtime)
    previous = load_connection(runtime)
    if verified_existing(previous, runs):
        if previous.get("lan") is not bool(lan) or previous.get("requested_port", previous.get("port")) != requested_port:
            raise ValueError("本任务目录已有经过验证的工作台使用不同端口或访问范围运行。请沿用已有设置，或手动关闭原工作台后再启动；未停止任何进程。")
        # Rebuild a known schema instead of carrying arbitrary persisted fields
        # (including accidentally appended account credentials) into the record.
        allowed = ("service", "version", "pid", "instance_id", "runs", "host", "requested_port", "port", "lan", "access_token", "started_at")
        result = {key: previous[key] for key in allowed if key in previous}
        # Reconstruct links locally instead of trusting persisted destination URLs.
        result["local_url"] = access_url("127.0.0.1", result["port"], result.get("access_token", ""))
        result["lan_urls"] = [access_url(address, result["port"], result.get("access_token", "")) for address in ipv4_addresses()] if lan else []
        result["reused"] = True
        result["runtime"] = str(runtime)
        result["privacy_note"] = PRIVATE_NOTE
        result["restricted_to_current_user"] = restricted
        result["permission_note"] = permission_note
        write_connection(runtime, result)
        if open_browser:
            webbrowser.open(result["local_url"], new=2)
        return result
    host = "0.0.0.0" if lan else "127.0.0.1"
    token = secrets.token_urlsafe(32) if lan else ""
    instance_id = uuid.uuid4().hex
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["ESDE_WORKBENCH_INSTANCE_ID"] = instance_id
    if token:
        environment["ESDE_WORKBENCH_TOKEN"] = token
    else:
        environment.pop("ESDE_WORKBENCH_TOKEN", None)
    script = Path(__file__).resolve().with_name("workbench.py")
    if not script.is_file():
        raise ValueError("缺少 workbench.py，工作台包不完整。")
    selected_port = available_port(host, requested_port)
    command = [sys.executable, "-B", str(script), "serve", "--root", str(runs), "--host", host, "--port", str(selected_port)]
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "env": environment, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    deadline = time.monotonic() + max(1.0, ready_timeout)
    ready = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise OSError("工作台启动进程已退出；端口可能在检查后被占用。请重试或指定另一个 --port。未停止任何已有服务。")
        candidate = health(selected_port, token, timeout=0.5)
        if health_matches(candidate, runs, selected_port, instance_id, process.pid, bool(lan)):
            ready = candidate
            break
        time.sleep(0.1)
    if ready is None:
        # Popen is our own newly created child handle, so cleanup cannot target an
        # unrelated occupant, stale PID, or process read from connection.json.
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise OSError("工作台未通过带身份校验的健康检查，已结束本次新建的启动进程。未停止任何已有服务。")
    result = {
        "service": SERVICE,
        "version": 1,
        "pid": process.pid,
        "instance_id": instance_id,
        "runs": str(runs),
        "host": host,
        "requested_port": requested_port,
        "port": selected_port,
        "lan": bool(lan),
        "access_token": token,
        "local_url": access_url("127.0.0.1", selected_port, token),
        "lan_urls": [access_url(address, selected_port, token) for address in ipv4_addresses()] if lan else [],
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime": str(runtime),
        "reused": False,
        "restricted_to_current_user": restricted,
        "permission_note": permission_note,
        "privacy_note": PRIVATE_NOTE,
    }
    write_connection(runtime, result)
    if open_browser:
        webbrowser.open(result["local_url"], new=2)
    return result


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--runs", required=True, help="包含真实任务记录的本机目录")
    cli.add_argument("--port", type=positive_port, default=8765, help="优先端口；占用时尝试后续可用端口，0 为自动选择")
    cli.add_argument("--lan", action="store_true", help="同时允许同一 Wi-Fi 设备使用随机凭据访问")
    cli.add_argument("--open", action="store_true", dest="open_browser", help="确认启动成功后打开电脑浏览器")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = launch(**vars(args))
    except (ValueError, OSError, argparse.ArgumentTypeError) as exc:
        print("工作台未启动：" + str(exc), file=sys.stderr)
        return 2
    print("已复用工作台。" if result["reused"] else "工作台已启动并通过身份验证。")
    print("电脑访问：" + result["local_url"])
    for url in result["lan_urls"]:
        print("同一 Wi-Fi 访问：" + url)
    if result["lan"] and not result["lan_urls"]:
        print("尚未发现可用的局域网 IPv4 地址；请连接 Wi-Fi 后重新运行。")
    if result["requested_port"] and result["port"] != result["requested_port"]:
        print("原端口已占用，工作台改用 " + str(result["port"]) + "；原服务继续运行。")
    print("连接记录：" + str(Path(result["runtime"]) / "connection.json"))
    print(result["permission_note"])
    print(PRIVATE_NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
