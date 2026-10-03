#!/usr/bin/env python3
"""Reusable read-only ES-DE audit, Android snapshot, and local repair preparation."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from esde_core import (CORE_MEDIA, IGNORED_DIRS, METADATA_FIELDS, MEDIA_XML_FIELDS, PROTECTED_FIELDS,
                       actual_files, audit_system, group_games, merged_game,
                       extension_map, index_media, load_json, normalize_document, parse_document,
                       preserved_snapshot, serialize_document, sha256, write_json)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Reporter:
    """Use the same durable store as the live workbench; JSONL remains a fallback."""
    def __init__(self, out, phase, args):
        self.out = Path(out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.phase = phase
        self.store = None
        try:
            import workbench_store
            self.store = workbench_store
            workbench_store.init_run(self.out, title="ES-DE resource workflow")
        except (ImportError, OSError, ValueError) as error:
            print("Workbench store unavailable; real progress saved in events.jsonl (" + type(error).__name__ + ")", file=sys.stderr)
        self.update("running", "Starting " + phase)

    def _local(self, kind, payload):
        payload = {"time": now(), "kind": kind, **payload}
        if self.store:
            payload = self.store.redact(payload)
        with (self.out / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def update(self, status, message, completed=None, total=None, connection=None, phase_status=None):
        payload = {"phase": self.phase, "status": status, "message": message}
        for key, value in (("completed", completed), ("total", total), ("connection", connection), ("phase_status", phase_status)):
            if value is not None:
                payload[key] = value
        self._local("progress", payload)
        if self.store:
            self.store.emit_update(self.out, **payload)
        print(message, flush=True)

    def job(self, record):
        job = self.job_payload(record)
        if self.store:
            self.store.upsert_job(self.out, **job)
        else:
            self._local("job", job)

    @staticmethod
    def job_payload(record):
        details = record.get("details") or {tag: record.get(tag, "") for tag in ("path", *METADATA_FIELDS)}
        return {"id": record["id"], "system": record["system"], "file": record["file"], "name": record.get("name", ""), "status": "pending" if record["issues"] or record["missing_media"] else "done", "missing": record.get("observed_missing_media", record["missing_media"]), "unknown": record.get("unknown_fields", []), "issues": record["issues"], "media": record["media"], "details": details}

    def jobs(self, records):
        if self.store:
            self.store.upsert_jobs(self.out, [self.job_payload(record) for record in records])
        else:
            for record in records:
                self.job(record)

    def register_previews(self, records, profile_id=None, remote=False):
        """Bind scanned files to opaque preview assets, never Android strings to PC files."""
        if not self.store:
            return
        try:
            import workbench_media
        except ImportError:
            return
        register = getattr(workbench_media, "register_remote_media_many" if remote else "register_media_many", None)
        if register is None or remote and not profile_id:
            return
        from preview_cache import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, PreviewUnavailable
        items = []
        for record in records:
            for kind, candidates in record["media"].items():
                for candidate in candidates:
                    path = candidate.get("path", "")
                    extension = PurePosixPath(path.replace("\\", "/")).suffix.casefold()
                    if candidate.get("size", 0) <= 0 or extension not in (VIDEO_EXTENSIONS if kind == "videos" else IMAGE_EXTENSIONS):
                        continue
                    item = {"job_id": record["id"], "kind": kind, "sha256": candidate.get("sha256") or None}
                    if remote:
                        item.update(remote_path=path, profile_id=profile_id, size=candidate["size"], origin="android_snapshot")
                    else:
                        item.update(path=path, origin="local_scan")
                    items.append(item)
        for start in range(0, len(items), 500):
            chunk = items[start:start + 500]
            try:
                register(self.out, chunk)
            except (OSError, ValueError, PreviewUnavailable) as error:
                # An individual file can disappear between inventory and preview
                # registration. Retain all other scanned assets and game status.
                unavailable = 0
                for item in chunk:
                    try:
                        register(self.out, [item])
                    except (OSError, ValueError, PreviewUnavailable):
                        unavailable += 1
                self.event("preview_unavailable", "Some scanned media could not be registered for preview", {"files": unavailable, "reason": str(error)})

    def event(self, kind, message, payload=None):
        payload = payload or {}
        self._local(kind, {"message": message, "payload": payload})
        if self.store:
            self.store.append_event(self.out, kind, message, payload)


class AdbError(RuntimeError):
    pass


class Adb:
    def __init__(self, executable="adb", serial=None):
        self.executable = str(executable)
        self.serial = serial

    def call(self, args, timeout=120):
        command = [self.executable] + (["-s", self.serial] if self.serial else []) + list(args)
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
        except FileNotFoundError as error:
            raise AdbError("ADB executable not found; provide --adb PATH") from error
        except subprocess.TimeoutExpired as error:
            raise AdbError("ADB timed out; reconnect the device and rerun to resume") from error
        if result.returncode:
            error_text = result.stderr.decode("utf-8", errors="replace").strip()
            raise AdbError("ADB command failed: " + error_text[:500])
        return result.stdout

    def devices(self):
        result = []
        for line in self.call(["devices", "-l"]).decode("utf-8", errors="replace").splitlines():
            if not line.strip() or line.startswith("List of devices") or line.startswith("*"):
                continue
            fields = line.split()
            if len(fields) < 2:
                continue
            properties = dict(field.split(":", 1) for field in fields[2:] if ":" in field)
            result.append({"serial": fields[0], "state": fields[1], **properties})
        return result

    def require_device(self):
        if not self.serial:
            raise AdbError("An explicit --serial is required; never select the first attached device")
        matched = [device for device in self.devices() if device["serial"] == self.serial]
        if not matched:
            raise AdbError("Selected device is disconnected; reconnect and rerun to resume")
        if matched[0]["state"] == "unauthorized":
            raise AdbError("Unlock the selected device and allow USB debugging, then rerun")
        if matched[0]["state"] != "device":
            raise AdbError("Selected device is " + matched[0]["state"] + "; reconnect and rerun")
        return matched[0]

    def shell_bytes(self, script, timeout=300):
        # Android's remote shell performs parsing. Quote the complete script for
        # sh -c, and quote every path inside the script separately.
        return self.call(["exec-out", "sh", "-c", shlex.quote(script)], timeout=timeout)

    def read(self, path):
        return self.shell_bytes("cat " + shlex.quote(path))

    def inventory(self, root, required=True):
        root = remote_root(root)
        check = "test -d " + shlex.quote(root)
        find = "find " + shlex.quote(root) + " -type f -exec stat -c '%s' {} \\; -print0"
        script = check + " && " + find if required else "if " + check + "; then " + find + "; fi"
        data = self.shell_bytes(script, timeout=600)
        result = []
        for record in data.split(b"\0"):
            if not record:
                continue
            size, separator, path = record.partition(b"\n")
            if not separator or not size.strip().isdigit():
                raise AdbError("Device inventory output unsupported; expected stat size plus NUL-delimited path")
            decoded = path.decode("utf-8")
            if not decoded.startswith(root.rstrip("/") + "/"):
                raise AdbError("Inventory returned a path outside the explicit root")
            result.append({"path": decoded, "size": int(size.strip())})
        return result


def remote_root(value):
    value = str(value).replace("\\", "/").rstrip("/")
    if not value.startswith("/") or value == "" or any(c in value for c in ("\x00", "\n", "\r")):
        raise ValueError("Remote root must be an explicit absolute Android path")
    if ".." in PurePosixPath(value).parts:
        raise ValueError("Remote root cannot contain parent traversal")
    return value or "/"


def inventory_relative(path, root):
    prefix = root.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    parts = PurePosixPath(path[len(prefix):]).parts
    if not parts or any(part in ("..", ".") for part in parts):
        return None
    return "/".join(parts)


def snapshot_android(args):
    out = Path(args.out).resolve()
    reporter = Reporter(out, "snapshot", args)
    adb = Adb(args.adb, args.serial)
    manifest_path = out / "manifest.json"
    esde_root, rom_root = remote_root(args.esde_root), remote_root(args.rom_root)
    try:
        device = adb.require_device()
        if manifest_path.exists():
            old = load_json(manifest_path)
            if old.get("serial") != args.serial or old.get("remote_esde_root") != esde_root or old.get("remote_rom_root") != rom_root:
                raise ValueError("Existing snapshot belongs to another device/root; use a separate --out")
        reporter.update("running", "Device authorized; collecting read-only inventories", connection={"status": "connected", "serial": device["serial"]})
        manifest = {"schema_version": 1, "serial": args.serial, "adb_executable": str(Path(args.adb).resolve()) if Path(args.adb).is_file() else str(args.adb), "remote_esde_root": esde_root, "remote_rom_root": rom_root, "remote_media_root": esde_root + "/downloaded_media", "created_at": now(), "complete": False, "files": []}
        write_json(manifest_path, manifest)
        rom_inventory = adb.inventory(rom_root)
        write_json(out / "rom_inventory.json", rom_inventory)
        reporter.event("inventory", "Actual ROM inventory collected", {"files": len(rom_inventory)})
        media_inventory = adb.inventory(esde_root + "/downloaded_media", required=False)
        write_json(out / "media_inventory.json", media_inventory)
        # Only game metadata and custom system definitions are read. Application
        # settings, accounts, Android private app data and ROM bytes are excluded.
        metadata_files = []
        for directory in ("gamelists", "custom_systems"):
            entries = adb.inventory(esde_root + "/" + directory, required=False)
            for item in entries:
                relative = inventory_relative(item["path"], esde_root)
                if relative and (relative.endswith("/gamelist.xml") or relative.startswith("custom_systems/") and relative.endswith(".xml")):
                    metadata_files.append((item, relative))
        reporter.update("running", "Collecting XML snapshots; ROM and media files remain on the device", completed=0, total=len(metadata_files))
        for count, (item, relative) in enumerate(metadata_files, 1):
            data = adb.read(item["path"])
            local = out / "es-de" / Path(relative)
            local.parent.mkdir(parents=True, exist_ok=True)
            if not local.exists() or sha256(local.read_bytes()) != sha256(data):
                temporary = local.with_name(local.name + ".part")
                temporary.write_bytes(data)
                temporary.replace(local)
            manifest["files"].append({"remote": item["path"], "local": "es-de/" + relative, "size": len(data), "sha256": sha256(data)})
            write_json(manifest_path, manifest)
            reporter.update("running", "Snapshotted " + relative, completed=count, total=len(metadata_files))
        manifest["complete"] = True
        manifest["completed_at"] = now()
        manifest["rom_inventory_count"] = len(rom_inventory)
        manifest["media_inventory_count"] = len(media_inventory)
        write_json(manifest_path, manifest)
        reporter.update("running", "Android snapshot complete: " + str(out), completed=len(metadata_files), total=len(metadata_files), phase_status="done")
        return 0
    except (AdbError, OSError, ValueError) as error:
        reporter.update("blocked" if isinstance(error, AdbError) else "error", str(error), connection={"status": "disconnected_or_unavailable"} if isinstance(error, AdbError) else None)
        reporter.event("resume", "Successful XML copies retained; rerun with the same device, roots and output directory")
        return 2


def local_media_files(root):
    root = Path(root)
    if not root.exists():
        return []
    result = []
    for parent, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not Path(parent, name).is_symlink()]
        for name in names:
            file = Path(parent, name)
            if file.is_file() and not file.is_symlink():
                result.append({"path": file.as_posix(), "size": file.stat().st_size})
    return result


OPTIONAL_IMAGE_MEDIA = ("backcovers", "3dboxes", "physicalmedia", "fanart")


def scanned_media_index(entries, media_root):
    """Include discovered optional raster assets without changing six-core checks."""
    indexed = index_media(entries, media_root)
    prefix = str(media_root).replace("\\", "/").rstrip("/") + "/"
    for item in entries:
        path = str(item["path"]).replace("\\", "/")
        if not path.startswith(prefix):
            continue
        parts = path[len(prefix):].split("/", 2)
        if len(parts) != 3 or parts[1] not in OPTIONAL_IMAGE_MEDIA:
            continue
        system, kind, relative = parts
        key = (system.casefold(), kind, str(PurePosixPath(relative).with_suffix("")))
        indexed[key].append({"path": path, "size": item.get("size", 0)})
    return indexed


def collect_audit_inputs(args):
    overrides = getattr(args, "extensions_override", None) or (load_json(args.extensions) if args.extensions else {})
    if args.snapshot:
        snapshot = Path(args.snapshot).resolve()
        manifest = load_json(snapshot / "manifest.json")
        if not manifest.get("complete"):
            raise ValueError("Snapshot is incomplete; rerun snapshot-android before auditing")
        esde = Path(args.esde_root).resolve() if args.esde_root else snapshot / "es-de"
        roots = {}
        inventory = load_json(snapshot / "rom_inventory.json")
        extensions = extension_map(overrides, esde)
        files = {}
        unsupported = set()
        for item in inventory:
            relative = inventory_relative(item["path"], manifest["remote_rom_root"])
            if not relative or "/" not in relative:
                continue
            system, file = relative.split("/", 1)
            if args.system and system not in args.system:
                continue
            if system.casefold() not in extensions:
                unsupported.add(system)
                continue
            parts = PurePosixPath(file).parts
            if any(part.casefold() in IGNORED_DIRS for part in parts[:-1]) or PurePosixPath(file).suffix.casefold() not in extensions[system.casefold()]:
                continue
            files.setdefault(system, []).append(file)
            roots[system] = manifest["remote_rom_root"].rstrip("/") + "/" + system
        media_root = manifest["remote_media_root"]
        media_index = scanned_media_index(load_json(snapshot / "media_inventory.json"), media_root)
        return esde, {system: sorted(set(values)) for system, values in files.items()}, roots, media_index, sorted(unsupported)
    if not args.rom_root or not args.esde_root:
        raise ValueError("Local audit requires --rom-root and --esde-root; Android audit uses --snapshot")
    rom = Path(args.rom_root).resolve()
    esde = Path(args.esde_root).resolve()
    if not rom.is_dir() or not esde.is_dir():
        raise ValueError("Explicit local ROM and ES-DE directories must exist")
    extensions = extension_map(overrides, esde)
    files, roots, unsupported = {}, {}, []
    for directory in sorted(rom.iterdir()):
        if not directory.is_dir() or directory.is_symlink() or directory.name.casefold() in IGNORED_DIRS:
            continue
        if args.system and directory.name not in args.system:
            continue
        if directory.name.casefold() not in extensions:
            unsupported.append(directory.name)
            continue
        selected = actual_files(directory, extensions[directory.name.casefold()])
        if selected:
            files[directory.name] = selected
            roots[directory.name] = str(directory)
    media_root = Path(args.media_root).resolve() if args.media_root else esde / "downloaded_media"
    media_index = scanned_media_index(local_media_files(media_root), media_root.as_posix())
    return esde, files, roots, media_index, unsupported


def gamelist_for(esde, system):
    direct = esde / "gamelists" / system / "gamelist.xml"
    if direct.is_file():
        return direct
    parent = esde / "gamelists"
    matches = [directory / "gamelist.xml" for directory in parent.iterdir() if directory.is_dir() and directory.name.casefold() == system.casefold()] if parent.is_dir() else []
    return matches[0] if len(matches) == 1 else direct


def element_details(node):
    """Lossless JSON shape for repeated/private XML fields, nesting and attributes."""
    from workbench_store import SECRET_KEY, redact
    tag = node.tag if isinstance(node.tag, str) else "#comment"
    sensitive = bool(SECRET_KEY.match(tag.rsplit("}", 1)[-1]))
    return redact({"tag": tag, "text": "[REDACTED]" if sensitive else node.text or "",
                   "tail": node.tail or "", "attributes": {key: "[REDACTED]" for key in node.attrib} if sensitive else dict(node.attrib),
                   "children": [] if sensitive else [element_details(child) for child in node]})


def enrich_details(rows, system_root, gamelist, actual_sizes=None, remote=False):
    actual = [row["file"] for row in rows]
    document = parse_document(Path(gamelist).read_bytes()) if Path(gamelist).is_file() else None
    groups, _ = group_games(document, system_root, actual) if document is not None else ({}, [])
    known = {"path", *METADATA_FIELDS, *MEDIA_XML_FIELDS, *PROTECTED_FIELDS}
    for row in rows:
        source_nodes = groups.get(row["file"], [])
        node = merged_game(source_nodes, row["file"])[0] if source_nodes else None
        actual_path = str(system_root).replace("\\", "/").rstrip("/") + "/" + row["file"]
        size = (actual_sizes or {}).get(actual_path)
        if not remote:
            try:
                size = Path(actual_path).stat().st_size
            except OSError:
                size = None
        metadata = {tag: row.get(tag, "") for tag in ("path", *METADATA_FIELDS)}
        protected = row.get("protected", {})
        fields = [element_details(child) for child in node] if node is not None else []
        row["details"] = {**metadata, "metadata": metadata,
                          "history": {tag: protected.get(tag) for tag in ("playcount", "playtime", "lastplayed")},
                          "protected": protected,
                          "file": {"path": row["file"], "actual_path": actual_path, "size": size,
                                   "origin": "android_snapshot" if remote else "local_scan"},
                          "extra": {"attributes": dict(node.attrib) if node is not None else {},
                                    "fields": [field for field in fields if field["tag"] not in known],
                                    "all_fields": fields,
                                    "original_paths": [source.findtext("path", "") for source in source_nodes],
                                    "original_nodes": [element_details(source) for source in source_nodes]}}
    return rows


def audit(args, verify=False):
    reporter = Reporter(args.out, "verify" if verify else "audit", args)
    try:
        receipt_path = Path(args.out) / ("verification_result.json" if verify else "audit_result.json")
        write_json(receipt_path, {"schema_version": 1, "status": "pending", "started_at": now()})
        expected_preserved = load_json(args.preserved) if verify and args.preserved else None
        checks = getattr(args, "checks", ("structure", "metadata", "media"))
        language = getattr(args, "language", "zh")
        esde, files, roots, media_index, unsupported = collect_audit_inputs(args)
        preview_profile = None
        actual_sizes = None
        if args.snapshot:
            actual_sizes = {item["path"]: item.get("size") for item in load_json(Path(args.snapshot) / "rom_inventory.json")}
            try:
                from preview_cache import bind_snapshot, PreviewUnavailable
                preview_profile = bind_snapshot(reporter.out, args.snapshot,
                                                adb=getattr(args, "preview_adb", None),
                                                serial=getattr(args, "preview_serial", None))
            except (OSError, ValueError, PreviewUnavailable) as error:
                reporter.event("preview_binding", "Android media inventory retained; on-demand preview transport is not bound", {"reason": str(error)})
        total = sum(len(selected) for selected in files.values())
        if not total:
            raise ValueError("No supported actual ROM files found; configure --extensions or the explicit roots")
        reporter.update("running", "Auditing actual ROM files; requested checks: " + ",".join(sorted(checks)), completed=0, total=total)
        records, summary, completed = [], [], 0
        preserved = {}
        for system, selected in files.items():
            gamelist = gamelist_for(esde, system)
            if args.snapshot and not args.esde_root:
                manifest = load_json(Path(args.snapshot) / "manifest.json")
                relative = "es-de/" + gamelist.relative_to(esde).as_posix()
                saved = next((item for item in manifest["files"] if item["local"] == relative), None)
                if saved is None:
                    # A prior resumable snapshot may contain an XML removed on
                    # the device. Only the current manifest is authoritative.
                    gamelist = Path(args.out) / ".not-present-in-snapshot" / system / "gamelist.xml"
                elif sha256(gamelist.read_bytes()) != saved["sha256"]:
                    raise ValueError("Snapshot XML changed after collection: " + relative)
            rows, system_summary = audit_system(system, selected, roots[system], gamelist, media_index, checks, language)
            for row in rows:
                stem = str(PurePosixPath(row["file"]).with_suffix(""))
                row["media"].update({kind: media_index[(system.casefold(), kind, stem)]
                                     for kind in OPTIONAL_IMAGE_MEDIA if (system.casefold(), kind, stem) in media_index})
            enrich_details(rows, roots[system], gamelist, actual_sizes, remote=bool(args.snapshot))
            records.extend(rows)
            summary.append(system_summary)
            if gamelist.is_file():
                preserved[system] = preserved_snapshot(parse_document(gamelist.read_bytes()), roots[system], selected)
            for start in range(0, len(rows), 200):
                chunk = rows[start:start + 200]
                reporter.jobs(chunk)
                reporter.register_previews(chunk, profile_id=preview_profile, remote=bool(args.snapshot))
                reporter.update("running", "Audited " + system + ": " + str(start + len(chunk)) + " actual files", completed=completed + start + len(chunk), total=total)
            completed += len(rows)
            reporter.update("running", "Audited " + system + ": " + str(len(rows)) + " actual files", completed=completed, total=total)
        write_json(Path(args.out) / "audit_games.json", records)
        write_json(Path(args.out) / "audit_summary.json", summary)
        preserved_path = Path(args.out) / "preserved_fields.json"
        # Keep the first immutable baseline. A validation pass must never replace
        # the very input against which it checks newly altered history.
        current_path = Path(args.out) / ("verified_preserved_fields.json" if verify else "audit_current_preserved_fields.json")
        write_json(current_path, preserved)
        if not verify and not preserved_path.exists():
            write_json(preserved_path, preserved)
        issues = sum(bool(row["issues"] or row["missing_media"]) for row in records)
        unresolved = sum(len(system["unresolved_entries"]) for system in summary) if "structure" in checks else 0
        result = {"schema_version": 1, "checked_at": now(), "actual_rom_files": total, "systems": len(files), "checks": sorted(checks), "language": language, "records_with_issues": issues, "unknown_game_records": sum(bool(row["unknown_fields"]) for row in records), "unresolved_entries": unresolved, "unsupported_system_directories": unsupported, "media_check": "exact native filename and nonzero file size; content decode and visual identity require additional QA" if "media" in checks else "not_requested", "status": "pass" if not issues and not unresolved and not unsupported else "needs_review"}
        if verify and args.preserved:
            expected = expected_preserved
            differences = []
            # Support a full audit snapshot or the single-system normalize file.
            if "fields" not in expected and expected and all("fields" in value for value in expected.values()):
                if len(files) != 1:
                    raise ValueError("Single-system preserved snapshot requires --system selection")
                expected = {next(iter(files)): expected}
            for system, rows in expected.items():
                for file, fields in rows.items():
                    if preserved.get(system, {}).get(file) != fields:
                        differences.append({"system": system, "file": file, "reason": "protected_or_private_fields_changed"})
            result["preserved_field_differences"] = differences
            if differences:
                result["status"] = "needs_review"
        write_json(receipt_path, result)
        if unsupported:
            reporter.event("unsupported_systems", "Systems require explicit extension configuration", {"systems": unsupported})
        passed_message = "Requested checks passed; awaiting media and visual QA" if "media" in checks else "Requested structural/metadata checks passed; media QA was not requested"
        reporter.update("blocked" if verify and result["status"] != "pass" else "running", (passed_message if verify and result["status"] == "pass" else "Audit complete; " + str(issues) + " actual files require work") + "; results: " + str(Path(args.out).resolve()), completed=total, total=total, phase_status="done" if not verify or result["status"] == "pass" else "blocked")
        if verify and result["status"] == "pass" and reporter.store:
            remaining = "media decoding and visual identity QA remain" if "media" in checks else "scoped checks passed; finish the requested delivery without claiming unrequested media QA"
            reporter.store.emit_update(reporter.out, phase="awaiting_media_visual_qa" if "media" in checks else "awaiting_scoped_completion", status="running", message=remaining, phase_status="pending")
        return 0 if not verify or result["status"] == "pass" else 1
    except (OSError, ValueError, ET.ParseError) as error:
        write_json(Path(args.out) / ("verification_result.json" if verify else "audit_result.json"), {"schema_version": 1, "status": "error", "checked_at": now(), "error": str(error)})
        reporter.update("error", str(error))
        return 2


def normalize(args, prepare=False):
    out = Path(args.out).resolve()
    reporter = Reporter(args.run_dir or out.parent, "prepare" if prepare else "normalize", args)
    try:
        source = Path(args.gamelist).resolve()
        if source == out:
            raise ValueError("Use a separate --out file; normalization never overwrites its input")
        if args.snapshot:
            override = {args.system: args.extensions.replace(",", " ").split()} if args.extensions else {}
            inputs = argparse.Namespace(snapshot=args.snapshot, esde_root=None, extensions=None, extensions_override=override, system=[args.system])
            _, systems, roots, _, _ = collect_audit_inputs(inputs)
            if args.system not in systems:
                raise ValueError("Selected system has no actual supported files in the snapshot")
            root, actual = roots[args.system], systems[args.system]
            if args.extensions:
                selected_extensions = set(args.extensions.replace(",", " ").casefold().split())
                actual = [file for file in actual if PurePosixPath(file).suffix.casefold() in selected_extensions]
        else:
            root = Path(args.system_rom_root).resolve()
            selected_extensions = set(args.extensions.replace(",", " ").casefold().split()) if args.extensions else extension_map().get(args.system.casefold(), set())
            if not selected_extensions:
                raise ValueError("Unsupported system; provide --extensions .ext,.ext")
            actual = actual_files(root, selected_extensions)
        if not actual:
            raise ValueError("No actual ROM files matched the explicit system root and extensions")
        source_data = source.read_bytes()
        document = parse_document(source_data)
        before = preserved_snapshot(document, root, actual)
        patch = load_json(args.patch) if prepare else []
        if isinstance(patch, dict):
            patch = patch.get("games")
        if not isinstance(patch, list):
            raise ValueError("Patch schema must be a list or {games: [...]} object")
        output, report = normalize_document(document, root, actual, patch)
        after = preserved_snapshot(output, root, actual)
        for file, fields in before.items():
            if after[file] != fields:
                raise ValueError("Protected fields changed unexpectedly for " + file)
        report.update({"system": args.system, "source_sha256": sha256(source_data), "output_sha256": sha256(serialize_document(output)), "prepared_at": now()})
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_name(out.name + ".part")
        temp.write_bytes(serialize_document(output))
        temp.replace(out)
        write_json(str(out) + ".report.json", report)
        write_json(str(out) + ".preserved.json", before)
        reporter.event("conflicts", "Nonempty conflicts resolved by deterministic history precedence; inspect report before deployment", {"count": len(report["conflicts"]), "report": out.name + ".report.json"})
        reporter.update("running", "Prepared " + str(len(actual)) + " unique actual-file references: " + str(out), completed=len(actual), total=len(actual), phase_status="done")
        return 0
    except (OSError, ValueError, ET.ParseError) as error:
        reporter.update("error", str(error))
        return 2


def parse_checks(value):
    checks = {"structure", "metadata", "media"} if value == "all" else set(value.split(","))
    if not checks or checks - {"structure", "metadata", "media"}:
        raise argparse.ArgumentTypeError("Use all or a comma-separated subset of structure,metadata,media")
    return tuple(sorted(checks))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    devices = commands.add_parser("devices", help="List ADB states without selecting a device")
    devices.add_argument("--adb", default=os.environ.get("ADB", "adb"))
    snapshot = commands.add_parser("snapshot-android", help="Read-only resumable XML and inventory snapshot")
    snapshot.add_argument("--adb", default=os.environ.get("ADB", "adb"))
    snapshot.add_argument("--serial", required=True)
    snapshot.add_argument("--esde-root", required=True)
    snapshot.add_argument("--rom-root", required=True)
    snapshot.add_argument("--out", required=True)
    for name in ("audit", "verify-local"):
        command = commands.add_parser(name, help="Audit actual local ROM files or Android snapshot inventories")
        command.add_argument("--rom-root")
        command.add_argument("--esde-root")
        command.add_argument("--media-root", help="Override local downloaded_media directory")
        command.add_argument("--snapshot", help="Complete snapshot-android output; avoids downloading ROM/media bytes")
        command.add_argument("--preview-adb", help="Explicit ADB executable for lazy snapshot media previews; defaults to snapshot binding")
        command.add_argument("--preview-serial", help="Must exactly match the snapshot device; never selects another attached device")
        command.add_argument("--extensions", help="JSON mapping system names to extension lists, overriding built-ins")
        command.add_argument("--system", action="append", help="Restrict to a system directory; repeat for several systems")
        command.add_argument("--out", required=True, help="Durable workbench run directory")
        command.add_argument("--checks", type=parse_checks, default=("structure", "metadata", "media"), help="all or a scoped comma-separated subset: structure,metadata,media")
        command.add_argument("--language", choices=("zh", "any"), default="zh", help="zh requires Chinese known metadata; any preserves the requested existing language")
        if name == "verify-local":
            command.add_argument("--preserved", help="Compare protected/private fields with earlier snapshot JSON")
    for name in ("normalize", "prepare"):
        command = commands.add_parser(name, help="Prepare a separate XML output with unique actual-file references")
        command.add_argument("--gamelist", required=True)
        command.add_argument("--system", required=True)
        source_root = command.add_mutually_exclusive_group(required=True)
        source_root.add_argument("--system-rom-root", help="Existing local system ROM directory")
        source_root.add_argument("--snapshot", help="Android snapshot with actual file inventory and explicit remote roots")
        command.add_argument("--extensions", help="Comma- or space-separated extensions for this system")
        command.add_argument("--out", required=True, help="Separate output XML, never the input file")
        command.add_argument("--run-dir", help="Workbench run directory (defaults to output XML parent)")
        if name == "prepare":
            command.add_argument("--patch", required=True, help="JSON factual metadata keyed by actual file; no history/path patches")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "devices":
        try:
            print(json.dumps(Adb(args.adb).devices(), ensure_ascii=False, indent=2))
            return 0
        except AdbError as error:
            print(str(error), file=sys.stderr)
            return 2
    if args.command == "snapshot-android":
        return snapshot_android(args)
    if args.command in ("audit", "verify-local"):
        return audit(args, verify=args.command == "verify-local")
    return normalize(args, prepare=args.command == "prepare")


if __name__ == "__main__":
    raise SystemExit(main())
