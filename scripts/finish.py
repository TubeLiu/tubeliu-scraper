"""Seal a scoped task only after reading its actual verification receipts."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from workbench_store import append_event, emit_update, get_state, init_run, redact


def read_preparation(path, run, key_path=None):
    import identity
    from deploy import xml_changes, file_hash
    path = Path(path).resolve()
    if not path.is_relative_to(run):
        raise ValueError("Preparation identity proof must belong to this run")
    payload = identity.verify_preparation(json.loads(path.read_text(encoding="utf-8")), key_path=key_path)
    for filename, field in (("output_path", "output_sha256"), ("source_path", "source_sha256"), ("identity_catalog_path", "identity_catalog_sha256")):
        source = Path(payload[filename]).resolve()
        if not source.is_relative_to(run) or not source.is_file() or file_hash(source) != payload[field]:
            raise ValueError("Frozen preparation XML/catalog changed or belongs to another run")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Changed prepared facts require exact signed game identities")
    system_root = str(payload["rom_root"])
    # xml_changes receives the library root, while preparation stores one
    # platform's explicit root. Avoid inferring any other device or platform.
    rom_root = system_root.replace("\\", "/").rsplit("/", 1)[0]
    actual_files = None
    if payload.get("binding", {}).get("kind") == "local":
        from esde_core import walk_files
        root = Path(system_root)
        actual_files = [candidate.relative_to(root).as_posix() for candidate in walk_files(root)]
    changes = xml_changes(Path(payload["source_path"]).read_bytes(), Path(payload["output_path"]).read_bytes(), payload["system"], rom_root, actual_files)
    by_file = {entry["file"]: entry for entry in entries}
    if len(by_file) != len(entries) or any(entry.get("system") != payload["system"] for entry in entries):
        raise ValueError("Preparation proof contains duplicate or cross-platform game identities")
    for change in changes:
        entry = by_file.get(change["file"])
        if entry is None or any(field not in entry["approved_metadata"] or (entry["approved_metadata"][field] or "") != value for field, value in change["metadata"].items()):
            raise ValueError("Preparation proof does not cover every changed fact")
    approved_media = []
    for entry in entries:
        binding = payload.get("binding", {})
        if binding.get("kind") == "local":
            actual = identity.fingerprint_file(Path(system_root) / entry["file"])
        elif binding.get("kind") == "android":
            from esde import Adb
            from adb_runtime import resolve_adb
            adb = Adb(resolve_adb(binding.get("adb_executable")), binding["serial"])
            adb.require_device()
            actual = adb.fingerprint(system_root.rstrip("/") + "/" + entry["file"], approved_root=binding["remote_rom_root"])
        else:
            raise ValueError("Preparation proof has no explicit library/device binding")
        receipt = entry["receipt"]
        identity.verify_receipt(receipt, system=entry["system"], file=entry["file"], rom_fingerprint=actual, metadata=entry["approved_metadata"], media=receipt["payload"].get("media", []), key_path=key_path)
        approved_media.extend({field: item[field] for field in ("type", "relative", "sha256", "size")} for item in receipt["payload"].get("media", []))
    return {"name": path.name, "sha256": file_hash(path), "status": "pass", "kind": "signed_preparation", "approved_games": len(entries), "approved_media": approved_media}


def check_prepared_media(data, approvals, run):
    """Re-read staged bytes; a stale decode report is not a media identity."""
    import identity
    records = data.get("files", [])
    if not approvals or not isinstance(records, list) or not records or any(not isinstance(record, dict) for record in records):
        raise ValueError("Prepared media completion requires concrete approved media and decoded file records")
    measured = []
    for record in records:
        raw = record.get("path")
        if not isinstance(raw, str) or not raw:
            raise ValueError("Prepared media QA requires an explicit staged file path")
        source = Path(raw)
        source = source if source.is_absolute() else run / source
        resolved = source.resolve()
        if not resolved.is_relative_to(run) or not source.is_file() or source.is_symlink():
            raise ValueError("Prepared media QA files must exist inside this run")
        actual = identity.fingerprint_file(resolved)
        if record.get("sha256") != actual["sha256"] or record.get("bytes") != actual["size"] or record.get("status") != "pass" or record.get("technical_verified") is not True:
            raise ValueError("Prepared media bytes changed after their decode QA")
        measured.append((record, actual))
    for approval in approvals:
        kind = "video" if approval["type"] == "videos" else "image"
        matches = [record for record, actual in measured if record.get("kind") == kind and actual["sha256"] == approval["sha256"] and actual["size"] == approval["size"] and (kind != "video" or record.get("verification") == "ffprobe_and_ffmpeg_frame_decode")]
        if not matches:
            raise ValueError("Prepared media QA does not cover the exact approved kind/bytes: " + approval["relative"])


def read_receipt(path, accepted=("pass",), run=None):
    path = Path(path).resolve()
    if run is not None and not path.is_relative_to(Path(run).resolve()):
        raise ValueError("Verification receipts must belong to this run directory")
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(path.name + " must contain a verification object")
    if data.get("status") not in accepted:
        raise ValueError(path.name + " has not passed the required check")
    return data, {"name": path.name, "sha256": hashlib.sha256(raw).hexdigest(), "status": data["status"]}


def finish(run, scope, audit=None, structure=None, deployment=None, media=None, visual=None, require_media=False, report_unknowns=False, notes="", identity_key=None, identity_preparation=None):
    run = Path(run).resolve()
    init_run(run)
    proofs = []
    try:
        # A new failed attempt must invalidate an older completion receipt.
        (run / "completion.json").unlink(missing_ok=True)
        if scope == "audit":
            if not audit:
                raise ValueError("Audit scope requires --audit audit_result.json")
            _, proof = read_receipt(audit, ("pass", "needs_review"), run)
            proofs.append(proof)
        else:
            if not structure:
                raise ValueError("Prepared/deployed scope requires --structure verification_result.json")
            structure_data, proof = read_receipt(structure, run=run)
            proofs.append(proof)
            if scope == "prepare":
                checks = structure_data.get("checks", [])
                if identity_preparation:
                    preparation = read_preparation(identity_preparation, run, identity_key)
                    proofs.append(preparation)
                elif require_media or media or not isinstance(checks, list) or set(checks) != {"structure"}:
                    raise ValueError("Prepared metadata/media completion requires --identity-preparation with a signed exact-ROM proof; structure-only completion must explicitly check only structure")
            if scope == "deploy":
                if not deployment or not visual:
                    raise ValueError("Deployment requires final hash verification and actual visual QA")
                deployment_data, proof = read_receipt(deployment, run=run)
                # A handwritten {status:pass, checks:{identity:true}} and a
                # screenshot never authorize facts or media for a ROM. Read the
                # signed plan and rerun its current target identity boundary.
                from deploy import load_plan, create_target, describe_all, recheck_identity_gate, file_hash
                import identity
                planned = load_plan(run, identity_key)
                signed = identity.verify_payload(deployment_data.get("identity_verification"), kind="deployment_verification", key_path=identity_key)
                expected = {"plan_sha256": file_hash(run / "deployment_plan.json"), "identity_gate": planned["identity_gate"], "files": deployment_data.get("files")}
                if signed != expected or deployment_data.get("identity_gate") != planned["identity_gate"] or deployment_data.get("plan_sha256") != expected["plan_sha256"]:
                    raise ValueError("Deployment identity verification does not cover this sealed plan")
                expected_files = {(item["scope"], item["relative"]): item["desired_sha256"] for item in planned["files"]}
                reported = deployment_data.get("files", [])
                reported_keys = {(record.get("scope"), record.get("relative")) for record in reported}
                if len(reported) != len(expected_files) or reported_keys != set(expected_files) or any(record.get("matches") is not True or record.get("actual_sha256") != expected_files.get((record.get("scope"), record.get("relative"))) for record in reported):
                    raise ValueError("Deployment verification is missing an exact planned file")
                target = create_target(planned["target"])
                recheck_identity_gate(run, planned, target)
                current = describe_all(target, planned["files"])
                if any(state["sha256"] != item["desired_sha256"] for item, state in zip(planned["files"], current)):
                    raise ValueError("Installed target changed after its deployment verification")
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
                require_media = require_media or bool(planned["identity_gate"].get("media"))
            if require_media and not media:
                raise ValueError("Changed/requested media requires --media media_qa.json")
            if media:
                media_data, proof = read_receipt(media, run=run)
                if scope == "prepare":
                    check_prepared_media(media_data, preparation["approved_media"], run)
                if scope == "deploy" and planned["identity_gate"].get("media"):
                    records = media_data.get("files", [])
                    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                        raise ValueError("Changed media QA must enumerate the decoded files")
                    for binding in planned["identity_gate"]["media"]:
                        kind = "video" if binding["type"] == "videos" else "image"
                        matches = [record for record in records if record.get("kind") == kind and record.get("sha256") == binding["sha256"] and record.get("bytes") == binding["size"] and record.get("status") == "pass" and record.get("technical_verified") is True and (kind != "video" or record.get("verification") == "ffprobe_and_ffmpeg_frame_decode")]
                        if not matches:
                            raise ValueError("Changed media lacks decode QA for its exact approved bytes: " + binding["relative"])
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
    except (ValueError, OSError, TypeError, KeyError, RuntimeError) as error:
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
    parser.add_argument("--identity-key", help="Explicit private key used for this deployment; omitted uses the private user configuration")
    parser.add_argument("--identity-preparation", help="Signed frozen XML/catalog proof for a prepared metadata/media change")
    try:
        print(json.dumps(finish(**vars(parser.parse_args())), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
