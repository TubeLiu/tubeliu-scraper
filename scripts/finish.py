"""Seal a scoped task only after reading its actual verification receipts."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from workbench_store import append_event, emit_update, get_state, init_run, redact


def read_receipt(path, accepted=("pass",), run=None):
    path = Path(path).resolve()
    if run is not None and not path.is_relative_to(Path(run).resolve()):
        raise ValueError("Verification receipts must belong to this run directory")
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    if data.get("status") not in accepted:
        raise ValueError(path.name + " has not passed the required check")
    return data, {"name": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "status": data["status"]}


def finish(run, scope, audit=None, structure=None, deployment=None, media=None, visual=None, require_media=False, report_unknowns=False, notes=""):
    run = Path(run).resolve()
    init_run(run)
    proofs = []
    try:
        if scope == "audit":
            if not audit:
                raise ValueError("Audit scope requires --audit audit_result.json")
            _, proof = read_receipt(audit, ("pass", "needs_review"), run)
            proofs.append(proof)
        else:
            if not structure:
                raise ValueError("Prepared/deployed scope requires --structure verification_result.json")
            _, proof = read_receipt(structure, run=run)
            proofs.append(proof)
            if scope == "deploy":
                if not deployment or not visual:
                    raise ValueError("Deployment requires final hash verification and actual visual QA")
                _, proof = read_receipt(deployment, run=run)
                proofs.append(proof)
                data, proof = read_receipt(visual, run=run)
                if not data.get("checked_at") or not data.get("checks") or any(value is not True for value in data["checks"].values()):
                    raise ValueError("Visual QA requires timestamp and explicitly passed relevant checks")
                evidence = data.get("evidence", [])
                if not isinstance(evidence, list) or not evidence:
                    raise ValueError("Visual QA requires actual evidence files")
                for filename in evidence:
                    source = (Path(visual).resolve().parent / filename).resolve()
                    if not source.is_relative_to(run) or not source.is_file() or source.stat().st_size == 0:
                        raise ValueError("Visual evidence must exist inside this run")
                proof["evidence"] = [{"name": str((Path(visual).resolve().parent / p).resolve().relative_to(run)), "sha256": hashlib.sha256((Path(visual).resolve().parent / p).read_bytes()).hexdigest()} for p in evidence]
                proofs.append(proof)
                plan = run / "deployment_plan.json"
                if plan.is_file():
                    planned = json.loads(plan.read_text(encoding="utf-8"))
                    require_media = require_media or any("downloaded_media/" in item.get("relative", "") for item in planned.get("files", []))
            if require_media and not media:
                raise ValueError("Changed/requested media requires --media media_qa.json")
            if media:
                _, proof = read_receipt(media, run=run)
                proofs.append(proof)
        unknown = get_state(run)["summary"]["unknown"]
        if unknown and scope != "audit" and not report_unknowns:
            raise ValueError("Unknown facts remain; include them in delivery and pass --report-unknowns")
        proof = redact({"scope": scope, "receipts": proofs, "unknown_game_records": unknown, "unknowns_reported": bool(report_unknowns or scope == "audit"), "notes": notes})
        (run / "completion.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
        append_event(run, "completion_receipts", "已读取本次范围的验收凭证，保留未知事实", proof)
        for phase in get_state(run)["phases"]:
            if phase["phase"].startswith("awaiting_") and phase["status"] in ("pending", "running", "blocked"):
                emit_update(run, phase=phase["phase"], status="running", phase_status="done", message="本次范围所需验收已完成；其他检查未计入该范围")
        emit_update(run, phase="complete", status="completed", phase_status="done", message="本次范围验收完成" + ("；未知事实已报告" if unknown else ""), completed=len(proofs), total=len(proofs), completion=proof)
        return proof
    except (ValueError, OSError, TypeError, KeyError) as error:
        emit_update(run, phase="complete", status="blocked", phase_status="blocked", message=str(error))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--scope", choices=("audit", "prepare", "deploy"), required=True)
    for key in ("audit", "structure", "deployment", "media", "visual"):
        parser.add_argument("--" + key)
    parser.add_argument("--require-media", action="store_true")
    parser.add_argument("--report-unknowns", action="store_true", help="Unknown factual fields have been included in the delivery; does not fill them")
    parser.add_argument("--notes", default="")
    try:
        print(json.dumps(finish(**vars(parser.parse_args())), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
