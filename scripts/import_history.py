"""Import verified historical receipts; never simulate a live scraper or rescan."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from workbench_store import MEDIA_TYPES, append_event, emit_update, init_run, upsert_job, upsert_jobs
from attach_history import details_for


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def receipt(path):
    data = Path(path).read_bytes()
    return {"name": Path(path).name, "sha256": hashlib.sha256(data).hexdigest()}


def import_all(metadata, deployment, verification, run, title):
    rows, manifest, verified = read(metadata), read(deployment), read(verification)
    if verified.get("status") != "pass" or not verified.get("unique_actual_rom_references_checked") or not verified.get("backup_hashes_checked"):
        raise ValueError("Historical receipt does not certify unique references and original backup")
    if verified.get("metadata_records_checked") != len(rows) or verified.get("complete_six_media_files") != len(rows) or verified.get("missing_media"):
        raise ValueError("Historical receipt does not cover this complete game inventory")
    games = {(g["system"], g["file"]): g for g in manifest["games"]}
    keys = [(g["system"], g["file"]) for g in rows]
    if len(keys) != len(set(keys)) or set(keys) != set(games):
        raise ValueError("Inventory is duplicated or differs from verified deployment")
    if any(row.get("path") != "./" + row["file"] for row in rows):
        raise ValueError("Inventory contains a noncanonical actual-file reference")
    init_run(run, title, {"status": "not_connected", "mode": "historical_receipt"})
    emit_update(run, phase="history_import", status="running", message="导入已验收历史记录；本次未连接设备重新扫描", completed=0, total=len(rows))
    batch = []
    for index, row in enumerate(rows, 1):
        game = games[(row["system"], row["file"])]
        media = {kind: {"present": True, "verification": "historical_receipt", "verified_at": verified.get("verified_at")} for kind in MEDIA_TYPES}
        if game.get("video_info"):
            media["videos"].update(game["video_info"])
        sources = [{"url": u, "type": "historical_source"} for u in row.get("source_urls", [])]
        batch.append(dict(id=row["system"] + ":" + row["file"], system=row["system"], file=row["file"], name=row.get("name", ""), status="done", missing=[], unknown=row.get("unknown_fields", []), sources=sources, media=media, video_kind=game.get("video_kind", ""), details=details_for(row, checked_at=verified.get("verified_at"))))
        if index % 200 == 0 or index == len(rows):
            upsert_jobs(run, batch)
            batch = []
            emit_update(run, phase="history_import", status="running", completed=index, total=len(rows), message=f"已导入历史记录 {index}/{len(rows)}")
    proof = {"type": "historical_receipt", "verified_at": verified.get("verified_at"), "receipts": [receipt(p) for p in (metadata, deployment, verification)], "stats": manifest.get("stats", {}), "installed_hashes_checked": verified.get("installed_hashes_checked"), "protected_fields_checked": verified.get("protected_fields_checked"), "visual_status": verified.get("visual_checks", {}).get("status"), "no_live_device_rescan": True}
    append_event(run, "historical_receipt", "历史安装、备份、实际引用与游玩记录已验收；当前设备状态未重新检查", proof)
    emit_update(run, phase="history_import", status="completed", phase_status="done", completed=len(rows), total=len(rows), message="历史记录导入完成；原验收通过，未知事实继续保留", historical=proof)
    return {"imported": len(rows), "run": str(Path(run).resolve()), "mode": "historical_receipt"}


def import_switch(metadata, installation, localization, media_manifest, run, title):
    rows, installed, localized, media = map(read, (metadata, installation, localization, media_manifest))
    if not installed.get("metadata_verified") or not localized.get("verified") or localized.get("chinese_game_count") != len(rows):
        raise ValueError("Switch receipts do not cover the Chinese inventory")
    files = [row["file"] for row in rows]
    if len(files) != len(set(files)):
        raise ValueError("Duplicate inventory files")
    verified_media = {Path(item["remote_path"]).name: item for item in installed.get("media_verification", []) if item.get("verified")}
    by_stem = {}
    for item in media:
        if item.get("media_type") not in MEDIA_TYPES:
            continue
        dest = item["stem"] + Path(item.get("local_path", "")).suffix
        if dest in verified_media:
            by_stem.setdefault(item["stem"], {})[item["media_type"]] = {"present": True, "sha256": item["sha256"], "verification": "historical_receipt"}
    init_run(run, title, {"status": "not_connected", "mode": "historical_receipt"})
    emit_update(run, phase="history_import", status="running", message="导入 Switch 中文化历史验收；未记录的原有媒体显示未记录", completed=0, total=len(rows))
    for row in rows:
        upsert_job(run, id="switch:" + row["file"], system="switch", file=row["file"], name=row.get("name", ""), status="done", missing=[], unknown=[k for k in ("developer", "publisher", "genre", "players") if not row.get(k)], sources=[{"url": u, "type": "historical_source"} for u in row.get("source_urls", [])], media=by_stem.get(PurePosixPath(row["file"]).with_suffix("").as_posix(), {}), details=details_for(row))
    proof = {"type": "historical_receipt", "receipts": [receipt(p) for p in (metadata, installation, localization, media_manifest)], "media_files_installed": installed.get("media_files_installed"), "visual_checks": {k: v for k, v in localized.get("device_visual_verification", {}).items() if isinstance(v, bool)}, "coverage_note": "仅标记历史清单中有 hash 验收的新增媒体；原有媒体没有重新扫描", "no_live_device_rescan": True}
    append_event(run, "historical_receipt", "Switch 历史中文化和纠错完成，原有媒体未在此处重新盘点", proof)
    emit_update(run, phase="history_import", status="completed", phase_status="done", completed=len(rows), total=len(rows), message="Switch 历史记录导入完成", historical=proof)
    return {"imported": len(rows), "run": str(Path(run).resolve()), "mode": "historical_receipt"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    allp = sub.add_parser("all-platforms")
    switch = sub.add_parser("switch")
    for command in (allp, switch):
        command.add_argument("--metadata", required=True)
        command.add_argument("--run", required=True)
        command.add_argument("--title", required=True)
    allp.add_argument("--deployment", required=True)
    allp.add_argument("--verification", required=True)
    switch.add_argument("--installation", required=True)
    switch.add_argument("--localization", required=True)
    switch.add_argument("--media-manifest", required=True)
    args = vars(parser.parse_args())
    mode = args.pop("mode")
    try:
        print(json.dumps((import_all if mode == "all-platforms" else import_switch)(**args), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
