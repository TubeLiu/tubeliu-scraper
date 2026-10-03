"""Attach real metadata and explicitly inventoried media to existing historical jobs.

This is a format adapter for saved ES-DE repair receipts, not a live device scan.
It never writes a device, changes game history or fabricates missing media.
"""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET

from esde_core import METADATA_FIELDS, PROTECTED_FIELDS, parse_document
from workbench_store import append_event, get_job, get_jobs, redact, upsert_jobs
from workbench_media import MIME_TYPES, register_media_many, register_remote_media_many


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def details_for(row, node=None, checked_at=None):
    details = {key: row.get(key) for key in ("name", "path", "desc", "developer", "publisher", "genre", "players", "releasedate", "rating", "scrapername", "date_precision") if key in row}
    details.update({"file": row["file"], "notes": row.get("notes", []), "history": dict(row.get("preserved", {})), "checked_at": checked_at, "source_mode": "historical_receipt"})
    if node is not None:
        fields = defaultdict(list)
        for child in node:
            if isinstance(child.tag, str):
                fields[child.tag].append({"value": child.text or "", "attributes": dict(child.attrib), "xml": ET.tostring(child, encoding="unicode")})
        for key in METADATA_FIELDS:
            if key in fields:
                details[key] = fields[key][0]["value"]
        details["path"] = node.findtext("path", details.get("path", "./" + row["file"]))
        details["history"].update({key: node.findtext(key) for key in PROTECTED_FIELDS})
        details["extra"] = {key: value for key, value in fields.items() if key not in {*METADATA_FIELDS, "path", *PROTECTED_FIELDS}}
        details["attributes"] = dict(node.attrib)
        details["raw_fields"] = dict(fields)
    return redact(details)


def xml_nodes(root, system=None):
    if root is None:
        return {}
    root = Path(root)
    sources = [(system or "switch", root)] if root.is_file() else [(p.parent.name, p) for p in root.glob("*/gamelist.xml")]
    nodes = {}
    for selected, source in sources:
        document = parse_document(source.read_bytes())
        for node in document.find("gameList").findall("game"):
            reference = node.findtext("path", "")
            if reference.startswith("./"):
                key = (selected, reference[2:])
                if key in nodes:
                    raise ValueError("Historical gamelist has duplicate actual references")
                nodes[key] = node
    return nodes


def _read_inventory(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        return [item["path"] if isinstance(item, dict) else item for item in read(path)]
    return [line.strip("\r\n") for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def attach(run, metadata, *, deployment=None, cache_index=None, inventory=None, gamelists=None,
           system=None, media_root=None, adb=None, serial=None, checked_at=None):
    run = Path(run).resolve()
    rows = read(metadata)
    if not isinstance(rows, list):
        raise ValueError("Metadata must be an explicit list of actual game records")
    jobs = {}
    for page in range(1, 1 + (get_jobs(run, limit=200)["total"] + 199) // 200):
        jobs.update({job["id"]: job for job in get_jobs(run, page=page, limit=200)["items"]})
    game_ids = {}
    batch = []
    nodes = xml_nodes(gamelists, system)
    for row in rows:
        selected = row.get("system") or system
        if not selected or not isinstance(row.get("file"), str):
            raise ValueError("Each metadata record needs explicit system and actual file")
        job_id = selected + ":" + row["file"]
        if job_id not in jobs:
            raise ValueError("Metadata record is not in this historical run: " + job_id)
        game_ids.setdefault((selected, str(PurePosixPath(row["file"]).with_suffix(""))), []).append(job_id)
        batch.append({"id": job_id, "details": details_for(row, nodes.get((selected, row["file"])), checked_at)})
        if len(batch) == 200:
            upsert_jobs(run, batch)
            batch = []
    if batch:
        upsert_jobs(run, batch)

    available = {}
    if cache_index:
        cached = read(cache_index)
        if not isinstance(cached, dict):
            raise ValueError("Cache index maps approved remote media paths to local/hash records")
        available.update(cached)
    if deployment:
        manifest = read(deployment)
        available.update({item["remote"]: item for item in manifest.get("media", [])})
    # Verified duplicate targets may share one exact content hash. Bind them to
    # one existing source instead of hashing/copying every physical replica.
    hash_paths = {}
    for item in available.values():
        local, digest = item.get("local"), item.get("sha256")
        if digest and digest not in hash_paths and local and Path(local).is_file():
            hash_paths[digest] = local
    paths = list(dict.fromkeys(_read_inventory(inventory) if inventory else available))
    if media_root is None:
        roots = {p.split("/downloaded_media/", 1)[0] + "/downloaded_media" for p in paths if "/downloaded_media/" in p}
        if len(roots) != 1:
            raise ValueError("Provide --media-root when the recorded inventory has no single root")
        media_root = next(iter(roots))
    prefix = str(media_root).rstrip("/") + "/"
    bound = defaultdict(lambda: defaultdict(list))
    local_items, local_targets, remote_items, remote_targets = [], [], [], []
    scanned = 0
    approved_remote = []
    for remote in paths:
        if not isinstance(remote, str) or not remote.startswith(prefix):
            continue
        parts = remote[len(prefix):].split("/", 2)
        if len(parts) != 3:
            continue
        selected, kind, filename = parts
        ids = game_ids.get((selected, str(PurePosixPath(filename).with_suffix(""))), [])
        if not ids:
            continue
        item = available.get(remote, {})
        supported = PurePosixPath(filename).suffix.lower() in MIME_TYPES
        approved_remote.append({"path": remote, "size": item.get("bytes") or item.get("size"), "sha256": item.get("sha256")})
        for job_id in ids:
            candidate = {"path": remote, "size": item.get("bytes") or item.get("size"), "sha256": item.get("sha256"), "origin": "historical_inventory", "preview": {"available": False, "reason": "本机未缓存；连接原设备后可读取" if supported else "该格式已扫描，暂不支持网页预览"}}
            bound[job_id][kind].append(candidate)
            scanned += 1
            local = hash_paths.get(item.get("sha256")) or item.get("local")
            if supported and local and Path(local).is_file():
                local_items.append({"job_id": job_id, "kind": kind, "path": local, "sha256": item.get("sha256"), "origin": "historical_prepared" if deployment and remote in available and item.get("remote") else "historical_cache"})
                local_targets.append(candidate)
            elif supported and adb and serial:
                remote_items.append({"job_id": job_id, "kind": kind, "remote_path": remote, "size": item.get("bytes") or item.get("size"), "sha256": item.get("sha256"), "origin": "historical_inventory"})
                remote_targets.append(candidate)

    profile_id = None
    if (adb is None) != (serial is None):
        raise ValueError("ADB and exact serial must be provided together")
    if remote_items:
        from preview_cache import bind_remote_profile, PreviewUnavailable
        profile_id = bind_remote_profile(run, adb=adb, serial=serial, media_root=media_root, inventory=approved_remote)
        if isinstance(profile_id, dict):
            profile_id = profile_id["id"]
        approved = []
        targets = []
        for item, candidate in zip(remote_items, remote_targets):
            try:
                from preview_cache import validate_remote_media
                validate_remote_media(run, {**item, "profile_id": profile_id})
            except PreviewUnavailable:
                candidate["preview"]["reason"] = "该格式已扫描，暂不支持网页预览"
                continue
            approved.append({**item, "profile_id": profile_id})
            targets.append(candidate)
        remote_items, remote_targets = approved, targets

    counts = {"records": len(rows), "scanned_media": scanned, "local_previews": 0, "device_previews": 0, "preview_errors": []}
    def publish(ids):
        updates = []
        for job_id in set(ids):
            media = dict(jobs[job_id]["media"])
            for kind, candidates in bound[job_id].items():
                previous = media.get(kind, {})
                value = dict(previous) if isinstance(previous, dict) else {"scanned_value": previous}
                primary = next((item for item in candidates if item["preview"].get("cached")), next((item for item in candidates if item["preview"].get("available")), candidates[0]))
                value.update({"path": primary["path"], "candidates": candidates, "extras": {**value.get("extras", {}), "preview": primary["preview"]}})
                media[kind] = value
            jobs[job_id]["media"] = media
            updates.append({"id": job_id, "media": media})
            if len(updates) == 200:
                upsert_jobs(run, updates)
                updates = []
        if updates:
            upsert_jobs(run, updates)
    for index in range(0, len(local_items), 500):
        items, targets = local_items[index:index + 500], local_targets[index:index + 500]
        try:
            previews = register_media_many(run, items, update_jobs=False)
            for target, preview in zip(targets, previews):
                target["preview"] = preview
                counts["local_previews"] += 1
        except (ValueError, OSError):
            for item, target in zip(items, targets):
                try:
                    target["preview"] = register_media_many(run, [item], update_jobs=False)[0]
                    counts["local_previews"] += 1
                except (ValueError, OSError) as error:
                    target["preview"] = {"available": False, "reason": "本地素材不可用或已与历史记录不同"}
                    if len(counts["preview_errors"]) < 20:
                        counts["preview_errors"].append(redact(str(error)))
        publish(item["job_id"] for item in items)
        if index % 2500 == 0:
            print("Attached local media " + str(min(index + 500, len(local_items))) + "/" + str(len(local_items)), flush=True)
    for index in range(0, len(remote_items), 500):
        previews = register_remote_media_many(run, remote_items[index:index + 500], update_jobs=False)
        for target, preview in zip(remote_targets[index:index + 500], previews):
            target["preview"] = preview
            counts["device_previews"] += 1
        publish(item["job_id"] for item in remote_items[index:index + 500])
    publish(bound)
    append_event(run, "detail_attached", "完整历史资料与实际媒体已接入详情；缺缓存记录保留，未重新扫描设备", counts)
    (run / "detail_attachment.json").write_text(json.dumps(redact(counts), ensure_ascii=False, indent=2), encoding="utf-8")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("run", "metadata"):
        parser.add_argument("--" + key, required=True)
    for key in ("deployment", "cache-index", "inventory", "gamelists", "system", "media-root", "adb", "serial", "checked-at"):
        parser.add_argument("--" + key)
    try:
        print(json.dumps(attach(**vars(parser.parse_args())), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"error": redact(str(error))}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
