#!/usr/bin/env python3
"""Validate and prepare bounded local ES-DE media; identity requires operator evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from workbench_store import MEDIA_TYPES, connect, emit_update, init_run, now, redact, upsert_job

DEFAULT_LIMITS = {"max_duration": 30.0, "max_bytes": 6000000, "max_height": 720}


class Blocked(RuntimeError):
    pass


def tool(name, explicit=None):
    value = explicit or os.environ.get("ESDE_" + name.upper()) or shutil.which(name)
    if not value or not Path(value).is_file():
        raise Blocked("Missing dependency: " + name + "; provide its path or add it to PATH")
    return str(Path(value).resolve())


def pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont
        return Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise Blocked("Missing dependency: Pillow") from exc


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_info(path):
    Image, _, _ = pillow()
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("Image is missing or empty")
    try:
        with Image.open(path) as image:
            width, height, format_name = image.width, image.height, image.format
            if width * height > 64000000:
                raise ValueError("Image exceeds safe pixel limit")
            image.verify()
        with Image.open(path) as image:
            image.load()
    except Exception as exc:
        raise ValueError("Image could not be fully decoded") from exc
    return {"kind": "image", "width": width, "height": height, "format": format_name, "bytes": path.stat().st_size, "sha256": sha256(path), "technical_verified": True}


def validate_probe(probe, size, limits=None):
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    if limits["max_duration"] <= 0 or not math.isfinite(limits["max_duration"]) or limits["max_bytes"] < 10000 or limits["max_height"] < 16:
        raise ValueError("Invalid media limits")
    video = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("Video stream not found")
    try:
        duration = float(probe.get("format", {}).get("duration") or video.get("duration") or 0)
        height = int(video.get("height") or 0)
        width = int(video.get("width") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid video stream metadata") from exc
    errors = []
    if not math.isfinite(duration) or duration <= 0 or duration > limits["max_duration"] + .05:
        errors.append("duration")
    if size <= 0 or size > limits["max_bytes"]:
        errors.append("bytes")
    if height <= 0 or width <= 0 or height > limits["max_height"]:
        errors.append("dimensions")
    if video.get("codec_name") != "h264":
        errors.append("codec_h264")
    if "mp4" not in probe.get("format", {}).get("format_name", "").split(","):
        errors.append("container_mp4")
    return {"kind": "video", "duration": duration, "width": width, "height": height, "codec": video.get("codec_name"), "bytes": size, "limits": limits, "technical_verified": not errors, "errors": errors, "verification": "ffprobe_container_and_stream_metadata"}


def video_info(path, ffprobe=None, limits=None):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("Video is missing or empty")
    binary = tool("ffprobe", ffprobe)
    result = subprocess.run([binary, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError("ffprobe could not read the video")
    try:
        report = validate_probe(json.loads(result.stdout), path.stat().st_size, limits)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("ffprobe returned invalid metadata") from exc
    report["sha256"] = sha256(path)
    return report


def decode_video(path, ffmpeg=None):
    binary = tool("ffmpeg", ffmpeg)
    _ffmpeg(["-xerror", "-i", str(Path(path).resolve()), "-map", "0:v:0", "-f", "null", "-"], binary)


def check_files(files, *, run=None, ffmpeg=None, ffprobe=None, limits=None):
    if not isinstance(files, list) or not files:
        raise ValueError("Media QA needs a nonempty files list")
    reports = []
    if run:
        init_run(run)
        emit_update(run, phase="media_qa", status="running", completed=0, total=len(files), message="Decoding staged media")
    for index, item in enumerate(files):
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict) or not item.get("path"):
            report = {"status": "fail", "error": "Media item requires path", "technical_verified": False}
        else:
            path = Path(item["path"]).resolve()
            kind = item.get("kind") or ("video" if path.suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm"} else "image")
            try:
                report = image_info(path) if kind == "image" else video_info(path, ffprobe, limits)
                if kind == "video" and report["technical_verified"]:
                    decode_video(path, ffmpeg)
                    report["verification"] = "ffprobe_and_ffmpeg_frame_decode"
                report.update({"path": str(path), "status": "pass" if report["technical_verified"] else "fail", "identity_confirmed": bool(item.get("identity_confirmed", False)), "identity_note": item.get("identity_note", ""), "visual_review_required": True})
            except (Blocked, ValueError, OSError, subprocess.SubprocessError) as exc:
                report = {"path": str(path), "status": "blocked" if isinstance(exc, Blocked) else "fail", "error": redact(str(exc)), "technical_verified": False, "identity_confirmed": False, "visual_review_required": True}
        reports.append(report)
        if run:
            emit_update(run, phase="media_qa", status="running", completed=index + 1, total=len(files), message="Media file checked: " + str(report.get("path", "invalid item")))
    status = "fail" if any(item["status"] == "fail" for item in reports) else "blocked" if any(item["status"] == "blocked" for item in reports) else "pass"
    receipt = redact({"status": status, "checked_at": now(), "checked_files": len(reports), "passed_files": sum(item["status"] == "pass" for item in reports), "checks": {"decode": status == "pass", "video_limits": status == "pass", "identity": False}, "visual_review_required": True, "files": reports})
    if run:
        emit_update(run, phase="media_qa", status="running" if status == "pass" else "blocked" if status == "blocked" else "error", phase_status="done" if status == "pass" else "blocked" if status == "blocked" else "error", completed=len(reports), total=len(files), message="Machine media QA " + status + "; visual identity review remains required")
    return receipt


def destination(out, relative):
    root = Path(out).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Output must stay inside --out") from exc
    if candidate == root or candidate.suffix.lower() != ".mp4":
        raise ValueError("Output must be an MP4 file inside --out")
    if candidate.exists() or Path(str(candidate) + ".provenance.json").exists():
        raise ValueError("Output already exists; choose a new path")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _ffmpeg(arguments, binary):
    try:
        result = subprocess.run([binary, "-nostdin", "-hide_banner", "-v", "error", *arguments], capture_output=True, timeout=600, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Media conversion timed out") from exc
    if result.returncode:
        raise ValueError("ffmpeg conversion failed (no output was accepted)")


def encode(source, out, relative, *, preview=False, duration=30, limits=None, ffmpeg=None, ffprobe=None):
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    if limits["max_duration"] <= 0 or limits["max_bytes"] < 10000 or limits["max_height"] < 16 or not math.isfinite(limits["max_duration"]):
        raise ValueError("Invalid media limits")
    duration = min(float(duration), limits["max_duration"])
    if duration <= 0 or not math.isfinite(duration):
        raise ValueError("Duration must be positive and finite")
    ffmpeg, ffprobe = tool("ffmpeg", ffmpeg), tool("ffprobe", ffprobe)
    target = destination(out, relative)
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("Source media not found")
    if source == target:
        raise ValueError("Source and destination must differ")
    max_height = int(limits["max_height"])
    scale = f"scale=w=min(iw\\,1280):h=min(ih\\,{max_height}):force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1"
    bitrate = max(10000, int(limits["max_bytes"] * .85 * 8 / duration) - (0 if preview else 96000))
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".esde-media-") as scratch:
        temporary = Path(scratch) / "output.mp4"
        for factor in (1, .65):
            rate = max(10000, int(bitrate * factor))
            prefix = ["-loop", "1", "-framerate", "30"] if preview else []
            args = [*prefix, "-i", str(source), "-t", str(duration), "-map", "0:v:0", "-vf", scale, "-r", "30", "-c:v", "libx264", "-b:v", str(rate), "-maxrate", str(rate), "-bufsize", str(rate * 2), "-pix_fmt", "yuv420p"]
            args += ["-an"] if preview else ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "96k"]
            args += ["-movflags", "+faststart", "-y", str(temporary)]
            _ffmpeg(args, ffmpeg)
            report = video_info(temporary, ffprobe, limits)
            if report["technical_verified"]:
                # Exclusive creation prevents accidentally replacing a concurrently created file.
                created = False
                try:
                    with temporary.open("rb") as src, target.open("xb") as dst:
                        created = True
                        shutil.copyfileobj(src, dst)
                except OSError:
                    if created:
                        target.unlink(missing_ok=True)
                    raise
                report["path"] = str(target)
                return report
        raise ValueError("Converted video still violates limits: " + ", ".join(report["errors"]))


def preview_frame(image_path, title, kind, out, font=None):
    Image, ImageDraw, ImageFont = pillow()
    if kind == "screenshot_preview" and not image_path:
        raise ValueError("Screenshot preview requires --image")
    if kind == "title_preview" and not title:
        raise ValueError("Title preview requires --title")
    canvas = Image.new("RGB", (960, 720), "#101b26")
    if image_path:
        image_info(image_path)
        with Image.open(image_path) as image:
            image = image.convert("RGB")
            image.thumbnail((960, 650))
            canvas.paste(image, ((960 - image.width) // 2, (650 - image.height) // 2))
    if title and any(ord(character) > 127 for character in title) and not font:
        candidate_font = Path(os.environ.get("ESDE_MEDIA_FONT", "C:/Windows/Fonts/msyh.ttc"))
        if not candidate_font.is_file():
            raise Blocked("Non-ASCII title preview needs a suitable --font or ESDE_MEDIA_FONT")
        font = candidate_font
    chosen_font = ImageFont.truetype(str(font), 28) if font else ImageFont.load_default(size=28)
    draw = ImageDraw.Draw(canvas)
    if title:
        draw.text((32, 280), str(title)[:160], fill="white", font=chosen_font)
    label = "SCREENSHOT PREVIEW - NOT GAMEPLAY FOOTAGE" if kind == "screenshot_preview" else "TITLE PREVIEW - NOT GAMEPLAY FOOTAGE"
    draw.rectangle((0, 650, 960, 720), fill="#203c50")
    draw.text((20, 675), label, fill="white", font=ImageFont.load_default(size=20))
    canvas.save(out, "PNG")


def publish(run, job_id, media_type, report, identity_confirmed=False, identity_note="", video_kind=""):
    if identity_confirmed and not identity_note.strip():
        raise ValueError("Identity confirmation requires an evidence note")
    report["identity"] = {"confirmed": bool(identity_confirmed), "note": identity_note, "confirmed_at": now() if identity_confirmed else None, "method": "operator_review" if identity_confirmed else "unreviewed"}
    report["video_kind"] = video_kind
    report["status"] = "verified" if report.get("technical_verified") and identity_confirmed else "needs_identity_review" if report.get("technical_verified") else "error"
    if run:
        init_run(run)
        if job_id:
            with connect(run) as db:
                row = db.execute("SELECT media FROM jobs WHERE id=?", (job_id,)).fetchone()
                media = json.loads(row[0]) if row else {}
            previous = media.get(media_type)
            previous = previous if isinstance(previous, dict) else {"extras": {"candidates": previous}} if isinstance(previous, list) else {}
            canonical_path = previous.get("path")
            if not canonical_path:
                candidates = previous.get("extras", {}).get("candidates") or []
                canonical_path = candidates[0].get("path") if candidates and isinstance(candidates[0], dict) else None
            stored = {**previous, **report, "local_path": report.get("path")}
            if canonical_path:
                # An Android destination remains an Android destination even
                # when a prepared replacement is available on this computer.
                stored["path"] = canonical_path
            stored["extras"] = {**previous.get("extras", {}), **report.get("extras", {})}
            media[media_type] = redact(stored)
            upsert_job(run, job_id, media=media, video_kind=video_kind or None)
            if report.get("path") and Path(report["path"]).is_file():
                from workbench_media import register_media
                register_media(run, job_id, media_type, report["path"], sha256=report.get("sha256"), origin="prepared_media")
        emit_update(run, phase="media", status="running" if report.get("technical_verified") else "error", phase_status="done" if report.get("technical_verified") else "error", completed=1, total=1, message="Media technically checked; identity " + ("confirmed" if identity_confirmed else "requires review"))
    return redact(report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Produce machine QA receipt; never confirms game identity")
    check.add_argument("--manifest", help="JSON list or {files:[{path,kind,...}]} with local file paths")
    check.add_argument("--root", help="Alternatively inspect image/video files inside a staging media folder")
    check.add_argument("--receipt", required=True)
    check.add_argument("--run")
    check.add_argument("--ffmpeg")
    check.add_argument("--ffprobe")
    check.add_argument("--max-duration", type=float, default=30)
    check.add_argument("--max-bytes", type=int, default=6000000)
    check.add_argument("--max-height", type=int, default=720)
    for command in ("inspect", "transcode", "preview"):
        cli = sub.add_parser(command)
        cli.add_argument("--run")
        cli.add_argument("--job-id")
        cli.add_argument("--media-type", choices=MEDIA_TYPES)
        cli.add_argument("--identity-confirmed", action="store_true")
        cli.add_argument("--identity-note", default="")
        cli.add_argument("--max-duration", type=float, default=30)
        cli.add_argument("--max-bytes", type=int, default=6000000)
        cli.add_argument("--max-height", type=int, default=720)
        cli.add_argument("--ffmpeg")
        cli.add_argument("--ffprobe")
        if command == "inspect":
            cli.add_argument("--file", required=True)
            cli.add_argument("--kind", choices=("image", "video"), required=True)
        elif command == "transcode":
            cli.add_argument("--file", required=True)
            cli.add_argument("--out", required=True)
            cli.add_argument("--relative", required=True)
            cli.add_argument("--duration", type=float, default=30)
        else:
            cli.add_argument("--image")
            cli.add_argument("--title", default="")
            cli.add_argument("--preview-kind", choices=("screenshot_preview", "title_preview"), required=True)
            cli.add_argument("--font")
            cli.add_argument("--out", required=True)
            cli.add_argument("--relative", required=True)
            cli.add_argument("--duration", type=float, default=8)
        if command != "preview":
            cli.add_argument("--video-kind", choices=("unverified_video", "gameplay_video"), default="unverified_video")
    args = parser.parse_args(argv)
    receipt_output = None
    try:
        if args.command == "check":
            receipt_output = Path(args.receipt).resolve()
            receipt_output.parent.mkdir(parents=True, exist_ok=True)
            # Invalidate the previous pass before reading a directory, manifest or dependency.
            pending = {"status": "pending", "checked_at": now(), "checked_files": 0, "passed_files": 0, "checks": {"decode": False, "video_limits": False, "identity": False}, "visual_review_required": True, "files": []}
            receipt_output.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
            if bool(args.manifest) == bool(args.root):
                raise ValueError("Provide exactly one of --manifest or --root")
            if args.manifest:
                manifest = Path(args.manifest).resolve()
                payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
                files = payload.get("files", []) if isinstance(payload, dict) else payload
                if isinstance(files, list):
                    normalized = []
                    for item in files:
                        if not isinstance(item, (str, dict)):
                            raise ValueError("Manifest entries must be paths or objects")
                        item = {"path": item} if isinstance(item, str) else dict(item)
                        path = Path(item.get("path", ""))
                        if not path.is_absolute():
                            item["path"] = str((manifest.parent / path).resolve())
                        normalized.append(item)
                    files = normalized
            else:
                root = Path(args.root).resolve()
                if not root.is_dir():
                    raise ValueError("Staging media root does not exist")
                files = []
                for path in sorted(root.rglob("*")):
                    resolved = path.resolve()
                    if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".mp4", ".mkv", ".mov", ".avi", ".webm"}:
                        try:
                            resolved.relative_to(root)
                        except ValueError as exc:
                            raise ValueError("Media symlink points outside staging root") from exc
                        files.append({"path": str(resolved)})
            receipt = check_files(files, run=args.run, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, limits={key: getattr(args, key) for key in DEFAULT_LIMITS})
            receipt_output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0 if receipt["status"] == "pass" else 3 if receipt["status"] == "blocked" else 2
        if args.identity_confirmed and not args.identity_note.strip():
            raise ValueError("Identity confirmation requires --identity-note")
        if getattr(args, "video_kind", "") == "gameplay_video" and not args.identity_confirmed:
            raise ValueError("Gameplay labeling requires explicit identity confirmation and evidence")
        if args.run:
            init_run(args.run)
            emit_update(args.run, phase="media", status="running", completed=0, total=1, message="Checking media")
        limits = {key: getattr(args, key) for key in DEFAULT_LIMITS}
        if args.command == "inspect":
            report = image_info(args.file) if args.kind == "image" else video_info(args.file, args.ffprobe, limits)
            report["path"] = str(Path(args.file).resolve())
            video_kind = args.video_kind if args.kind == "video" else ""
            media_type = args.media_type or ("videos" if args.kind == "video" else "screenshots")
        elif args.command == "transcode":
            report = encode(args.file, args.out, args.relative, duration=args.duration, limits=limits, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
            report["source_sha256"] = sha256(args.file)
            report["source_path"] = str(Path(args.file).resolve())
            video_kind, media_type = args.video_kind, args.media_type or "videos"
        else:
            with tempfile.TemporaryDirectory() as scratch:
                frame = Path(scratch) / "preview.png"
                preview_frame(args.image, args.title, args.preview_kind, frame, args.font)
                report = encode(frame, args.out, args.relative, preview=True, duration=args.duration, limits=limits, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
            report["source_path"] = str(Path(args.image).resolve()) if args.image else "operator_supplied_title"
            report["label"] = "Preview assembled from still image/title; contains no gameplay footage"
            video_kind, media_type = args.preview_kind, "videos"
        report = publish(args.run, args.job_id, media_type, report, args.identity_confirmed, args.identity_note, video_kind)
        if args.command != "inspect":
            Path(report["path"] + ".provenance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["technical_verified"] else 2
    except (Blocked, ValueError, OSError, subprocess.SubprocessError) as exc:
        status = "blocked" if isinstance(exc, Blocked) else "error"
        if receipt_output is not None:
            failure = {"status": "blocked" if status == "blocked" else "fail", "checked_at": now(), "checked_files": 0, "passed_files": 0, "checks": {"decode": False, "video_limits": False, "identity": False}, "visual_review_required": True, "files": [], "error": redact(str(exc))}
            try:
                receipt_output.write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                print(json.dumps({"status": "fail", "error": "Receipt could not be updated; an earlier receipt must not be used"}), file=sys.stderr)
        if args.run:
            init_run(args.run)
            emit_update(args.run, phase="media_qa" if args.command == "check" else "media", status=status, message=str(exc))
        print(json.dumps({"status": status, "error": redact(str(exc))}, ensure_ascii=False), file=sys.stderr)
        return 3 if status == "blocked" else 2


if __name__ == "__main__":
    sys.exit(main())
