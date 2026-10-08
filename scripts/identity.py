"""Fail-closed game identity and exact approved-write receipts.

This module performs no network or device operations.  Provider and download
seals may ONLY be issued by the real HTTPS response handlers, not by loading a
user-supplied report.  They attest to a response observed by this installation;
they are not ScreenScraper signatures, nor protection from a local user who can
modify Python or read this user's private key.  There is deliberately no CLI for
signing arbitrary reports or turning a boolean into approval.
"""
from __future__ import annotations

import base64
import argparse
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import screenscraper_credentials as private_store

SCHEMA_VERSION = 1
KEY_ENV = "TUBELIU_IDENTITY_KEY_FILE"
METADATA_FIELDS = {"name", "desc", "developer", "publisher", "genre", "players", "releasedate", "rating", "scrapername"}
# Unverified systems intentionally have no automatic identity route.  IDs were
# checked against https://www.screenscraper.fr/systemeinfos.php?plateforme=ID.
SCREENSCRAPER_SYSTEMS = {
    "nes": "3", "famicom": "3", "snes": "4", "sfc": "4",
    "gb": "9", "gbc": "10", "gba": "12", "gc": "13", "n64": "14",
    "nds": "15", "wii": "16", "n3ds": "17", "psx": "57", "ps2": "58",
    "psp": "61", "dreamcast": "23", "megadrive": "1", "saturn": "22",
    "mastersystem": "2", "gamegear": "21", "switch": "225",
}
SOURCE_MEDIA_TYPES = {
    "box-2d": "covers", "box2d": "covers", "covers": "covers",
    "box-3d": "3dboxes", "3dboxes": "3dboxes",
    "box-2d-back": "backcovers", "backcovers": "backcovers",
    "ss": "screenshots", "screenshot": "screenshots", "screenshots": "screenshots",
    "sstitle": "titlescreens", "titlescreen": "titlescreens", "titlescreens": "titlescreens",
    "wheel": "marquees", "wheel-hd": "marquees", "marquee": "marquees", "marquees": "marquees",
    "mixrbv1": "miximages", "mixrbv2": "miximages", "miximages": "miximages",
    "video": "videos", "video-normalized": "videos", "videos": "videos",
    "fanart": "fanart", "support": "physicalmedia", "physicalmedia": "physicalmedia",
}
_DIGESTS = {"sha256": 64, "sha1": 40, "md5": 32, "crc": 8}


class IdentityError(ValueError):
    """A proposed write remains unapproved; diagnostics contain no secrets."""

    def __init__(self, message, code="identity_unconfirmed"):
        super().__init__(message)
        self.code = code


def _fail(message, code="identity_unconfirmed"):
    raise IdentityError(message, code)


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail("Identity evidence must contain ordinary JSON values", "invalid_evidence")


def _object(value, name):
    if not isinstance(value, dict):
        _fail(name + " must be a JSON object", "invalid_evidence")
    return value


def _relative(value, name="File"):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        _fail(name + " must be a complete canonical relative path", "invalid_path")
    parts = value.split("/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value) or any(part in ("", ".", "..") for part in parts):
        _fail(name + " must not escape its explicit root", "invalid_path")
    return value


def _system(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        _fail("An explicit canonical platform is required", "invalid_platform")
    return value


def default_key_path():
    # Separate from game runs, prepared manifests, repositories and credentials.
    explicit = os.environ.get(KEY_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return private_store.default_path().parent.parent / "identity" / "receipt-key.json"


def _key(key_path=None, *, create=False):
    try:
        path = private_store._path(default_key_path() if key_path is None else key_path)
        if not path.exists():
            if not create:
                _fail("The original installation's private identity key is unavailable", "missing_identity_key")
            path.parent.mkdir(parents=True, exist_ok=True)
            private_store._private(path.parent, set_permissions=True, directory=True)
            material = secrets.token_bytes(32)
            protection = "windows_dpapi" if os.name == "nt" else "owner_only_file"
            blob = private_store._dpapi(material) if os.name == "nt" else material
            data = _canonical({"schema_version": SCHEMA_VERSION, "protection": protection, "blob": base64.b64encode(blob).decode("ascii")})
            # A fully written private temp file is linked atomically, never
            # replacing another process's existing signing key.
            descriptor, temporary = tempfile.mkstemp(prefix=".identity-key-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                private_store._private(Path(temporary), set_permissions=True)
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                Path(temporary).unlink(missing_ok=True)
        private_store._private(path)
        if path.stat().st_size > 16384:
            _fail("The private identity key store is invalid", "invalid_identity_key")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            _fail("The private identity key schema is unsupported", "invalid_identity_key")
        material = base64.b64decode(data["blob"], validate=True)
        if os.name == "nt":
            if data.get("protection") != "windows_dpapi":
                _fail("An encrypted current-user identity key is required", "invalid_identity_key")
            material = private_store._dpapi(material, decrypt=True)
        elif data.get("protection") != "owner_only_file":
            _fail("The identity key belongs to another platform", "invalid_identity_key")
        if len(material) != 32:
            _fail("The private identity key has invalid size", "invalid_identity_key")
        return material
    except IdentityError:
        raise
    except (OSError, ValueError, TypeError, KeyError, private_store.CredentialError):
        _fail("Cannot safely use the current user's private identity key", "invalid_identity_key")


def seal_payload(payload, kind, key_path=None):
    """Internal trust-boundary API, never exposed as an arbitrary sign command."""
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind):
        _fail("Unsupported identity seal purpose", "invalid_evidence")
    _object(payload, "Sealed payload")
    detached = json.loads(_canonical(payload))
    unsigned = {"schema_version": SCHEMA_VERSION, "kind": kind, "payload": detached}
    material = _key(key_path, create=True)
    return {**unsigned, "signature": hmac.new(material, _canonical(unsigned), hashlib.sha256).hexdigest()}


def verify_payload(envelope, kind, key_path=None):
    envelope = _object(envelope, "Sealed identity evidence")
    if set(envelope) != {"schema_version", "kind", "payload", "signature"} or envelope.get("schema_version") != SCHEMA_VERSION or envelope.get("kind") != kind:
        _fail("Missing or unsupported identity seal", "invalid_identity_seal")
    _object(envelope["payload"], "Identity payload")
    signature = envelope.get("signature")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        _fail("Identity evidence has no valid signature", "invalid_identity_seal")
    unsigned = {key: envelope[key] for key in ("schema_version", "kind", "payload")}
    expected = hmac.new(_key(key_path), _canonical(unsigned), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        _fail("Identity evidence was modified or belongs to another installation", "identity_evidence_tampered")
    return copy.deepcopy(envelope["payload"])


def seal_preparation(payload, key_path=None):
    return seal_payload(payload, "preparation", key_path)


def verify_preparation(envelope, key_path=None):
    return verify_payload(envelope, "preparation", key_path)


def fingerprint_stream(handle):
    """Measure actual bytes (including size), never trust a supplied JSON hash."""
    hashes = {name: getattr(hashlib, name)() for name in ("sha256", "sha1", "md5")}
    crc, size = 0, 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        if not isinstance(chunk, bytes):
            _fail("ROM measurement requires binary bytes", "invalid_fingerprint")
        size += len(chunk)
        for value in hashes.values():
            value.update(chunk)
        crc = zlib.crc32(chunk, crc)
    return {"size": size, **{name: value.hexdigest() for name, value in hashes.items()}, "crc": format(crc & 0xffffffff, "08x")}


def fingerprint_file(path):
    path = Path(path)
    try:
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
            _fail("A concrete regular ROM/media file is required", "invalid_fingerprint")
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            result = fingerprint_stream(handle)
            after = os.fstat(handle.fileno())
        final = path.stat()
        marker = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if marker(before) != marker(after) or marker(after) != marker(final) or result["size"] != final.st_size:
            _fail("File changed while its identity was measured", "rom_changed")
        return result
    except OSError:
        _fail("The selected file cannot be measured", "invalid_fingerprint")


def _fingerprint(value, *, complete=False):
    value = _object(value, "Actual ROM fingerprint")
    size = value.get("size")
    if type(size) is not int or size <= 0:
        _fail("Actual ROM byte size is required", "invalid_fingerprint")
    result = {"size": size}
    for name, length in _DIGESTS.items():
        if name not in value:
            if complete or name == "sha256":
                _fail("Actual ROM hashes are incomplete", "invalid_fingerprint")
            continue
        digest = value[name]
        if not isinstance(digest, str) or not re.fullmatch("[0-9A-Fa-f]{" + str(length) + "}", digest):
            _fail("Actual ROM hash has invalid format", "invalid_fingerprint")
        result[name] = digest.lower()
    return result


def seal_provider_report(report, key_path=None):
    """Called by the official HTTPS query handler after credential redaction."""
    return seal_payload(report, "provider_report", key_path)


def seal_download(provenance, key_path=None):
    """Called only after the official HTTPS download handler measured bytes."""
    return seal_payload(provenance, "provider_download", key_path)


def _url(value):
    if not isinstance(value, str):
        _fail("The source media URL is missing", "media_source_unconfirmed")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not (host == "screenscraper.fr" or host.endswith(".screenscraper.fr")) or parts.username or parts.password or parts.port not in (None, 443) or parts.fragment:
            _fail("Source media must belong to the observed HTTPS provider", "media_source_unconfirmed")
    except ValueError:
        _fail("Source media URL is invalid", "media_source_unconfirmed")
    return value


def _rom_records(evidence):
    if isinstance(evidence, list):
        return [item for item in evidence if isinstance(item, dict)]
    if isinstance(evidence, dict):
        if any(key in evidence for key in ("md5", "sha1", "crc", "rommd5", "romsha1", "romcrc")):
            return [evidence]
        # ScreenScraper can wrap several returned ROMs in a `rom` member.
        if "rom" in evidence:
            return _rom_records(evidence["rom"])
    return []


def _returned_rom(record):
    result = {}
    for key, aliases in {
        "size": ("size", "romsize", "romtaille", "taille"),
        "md5": ("md5", "rommd5"), "sha1": ("sha1", "romsha1"), "crc": ("crc", "romcrc"),
    }.items():
        available = [record[name] for name in aliases if name in record and record[name] not in (None, "")]
        if len(available) > 1 and len({str(value).lower() for value in available}) != 1:
            _fail("Provider returned conflicting ROM fields", "ambiguous_rom")
        if available:
            if key == "size":
                raw = available[0]
                if isinstance(raw, bool) or not re.fullmatch(r"[0-9]+", str(raw)):
                    _fail("Provider ROM size is invalid", "invalid_source_evidence")
                result[key] = int(raw)
            else:
                raw = available[0]
                if not isinstance(raw, str) or not re.fullmatch("[0-9A-Fa-f]{" + str(_DIGESTS[key]) + "}", raw):
                    _fail("Provider ROM hash is invalid", "invalid_source_evidence")
                result[key] = raw.lower()
    return result


def _matched_candidate(source_envelope, system, measured, key_path=None):
    source = verify_payload(source_envelope, "provider_report", key_path)
    expected = SCREENSCRAPER_SYSTEMS.get(system)
    if expected is None:
        _fail("This platform has no verified automatic provider mapping; keep it pending", "unsupported_platform")
    if source.get("provider") != "screenscraper" or source.get("endpoint") != "jeuInfos.php":
        _fail("Name search or an untrusted source cannot confirm a ROM identity", "non_exact_lookup")
    query = _object(source.get("query"), "Observed provider query")
    if str(query.get("systemeid")) != expected:
        _fail("Provider query platform differs from the selected ROM platform", "platform_mismatch")
    if type(query.get("romtaille")) is not int or query.get("romtaille") != measured["size"]:
        _fail("Provider query size differs from the actual ROM", "rom_mismatch")
    crypto_query = False
    for name in ("md5", "sha1", "crc"):
        if query.get(name) not in (None, ""):
            if str(query[name]).lower() != measured.get(name):
                _fail("Provider query fingerprint differs from the actual ROM", "rom_mismatch")
            crypto_query |= name in ("md5", "sha1")
    if not crypto_query:
        _fail("CRC or filename-only queries require review, not automatic approval", "weak_fingerprint")
    candidates = source.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
        _fail("A unique exact-ROM candidate is required", "ambiguous_candidate")
    candidate = candidates[0]
    if candidate.get("provider") != "screenscraper" or not str(candidate.get("provider_game_id") or "").strip():
        _fail("Provider game ID is missing", "invalid_source_evidence")
    platform = _object(candidate.get("system"), "Returned provider platform")
    if str(platform.get("id")) != expected:
        _fail("Returned game belongs to another platform", "platform_mismatch")
    matching = []
    for raw in _rom_records(candidate.get("rom_evidence")):
        record = _returned_rom(raw)
        if record.get("size") != measured["size"] or not any(record.get(name) for name in ("md5", "sha1")):
            continue
        if all(value == measured.get(name) for name, value in record.items()):
            matching.append(raw)
    if len(matching) != 1:
        _fail("Returned ROM evidence is absent, ambiguous or differs from the actual file", "rom_mismatch")
    return source, candidate, matching[0]


def _metadata(candidate, proposed):
    proposed = _object(proposed, "Approved metadata")
    if set(proposed) - METADATA_FIELDS:
        _fail("Only factual ES-DE metadata fields can be authorized", "metadata_mismatch")
    source_fields = {
        "name": candidate.get("name"), "desc": candidate.get("description_zh", candidate.get("desc")),
        **{field: candidate.get(field) for field in ("developer", "publisher", "genre", "players", "releasedate", "rating")},
        "scrapername": "screenscraper",
    }
    result = {}
    for field, value in proposed.items():
        if value is not None and not isinstance(value, str):
            _fail("Approved metadata values must be strings or null", "metadata_mismatch")
        observed = source_fields.get(field)
        if value not in (None, ""):
            if not isinstance(observed, str) or not observed.strip() or value != observed:
                _fail("Proposed " + field + " is not the confirmed source's exact value; separate review is required", "metadata_mismatch")
        elif observed not in (None, ""):
            # Clearing a known fact is a changed payload and needs review too.
            _fail("Clearing a confirmed source fact is not automatically authorized", "metadata_mismatch")
        result[field] = value
    return result


def _approved_metadata(candidate, metadata, review, source, system, file, fingerprint, matched, key_path, target=None):
    if review is None:
        return _metadata(candidate, metadata)
    from translations import reviewed_metadata
    translations = reviewed_metadata(review, candidate, source=source, system=system, file=file,
                                    fingerprint=fingerprint, matched=matched, key_path=key_path, target=target)
    metadata = _object(metadata, "Approved metadata")
    if any(metadata.get(field) != text for field, text in translations.items()):
        _fail("Metadata differs from independently reviewed translation", "metadata_mismatch")
    facts = _metadata(candidate, {field: value for field, value in metadata.items() if field not in translations})
    return {**facts, **translations}


def _candidate_urls(candidate, kind):
    values = candidate.get("media_candidates", [])
    if not isinstance(values, list):
        _fail("Returned media candidates have invalid format", "invalid_source_evidence")
    return {item.get("url") for item in values if isinstance(item, dict) and SOURCE_MEDIA_TYPES.get(str(item.get("type", "")).lower()) == kind}


def _media_item(item, system, file, *, candidate=None, key_path=None, require_download=False):
    item = _object(item, "Approved media")
    kind = item.get("type", item.get("kind"))
    if kind not in set(SOURCE_MEDIA_TYPES.values()):
        _fail("Unknown media kind cannot be approved", "media_mismatch")
    relative = _relative(item.get("relative"), "Media destination")
    parts = relative.split("/", 2)
    if len(parts) != 3 or parts[:2] != [system, kind] or str(PurePosixPath(parts[2]).with_suffix("")) != str(PurePosixPath(file).with_suffix("")):
        _fail("Media destination is not this platform and full ROM stem", "media_mismatch")
    url = _url(item.get("source_url"))
    if candidate is not None and url not in _candidate_urls(candidate, kind):
        _fail("Selected media was not returned for this confirmed game and kind", "media_source_unconfirmed")
    if item.get("path") is not None:
        actual = fingerprint_file(item["path"])
        size, digest = actual["size"], actual["sha256"]
        if item.get("sha256") is not None and item["sha256"] != digest or item.get("size") is not None and item["size"] != size:
            _fail("Media changed after it was measured", "media_changed")
    else:
        actual = _fingerprint({"size": item.get("size", item.get("bytes")), "sha256": item.get("sha256")})
        size, digest = actual["size"], actual["sha256"]
        if require_download:
            _fail("Actual media bytes must be measured before authorization", "media_unmeasured")
    if size <= 0:
        _fail("Empty media cannot be approved", "media_mismatch")
    result = {"type": kind, "relative": relative, "source_url": url, "size": size, "sha256": digest}
    if require_download:
        proof = item.get("download_receipt", item.get("identity_download"))
        provenance = verify_payload(proof, "provider_download", key_path)
        if provenance.get("provider") != "screenscraper" or provenance.get("source_url") != url or provenance.get("sha256") != digest or provenance.get("bytes", provenance.get("size")) != size:
            _fail("Media bytes are not the observed provider download", "media_source_unconfirmed")
        result["download_receipt"] = copy.deepcopy(proof)
    return result


def _media(items, system, file, **kwargs):
    if not isinstance(items, list):
        _fail("Approved media must be a list", "media_mismatch")
    result = [_media_item(item, system, file, **kwargs) for item in items]
    identities = [(item["type"], item["relative"]) for item in result]
    if len(set(identities)) != len(identities):
        _fail("Duplicate media destinations are ambiguous", "media_mismatch")
    return sorted(result, key=lambda item: (item["type"], item["relative"]))


def authorize_patch(source_envelope, *, system, file, metadata, media=None, rom_path=None, rom_fingerprint=None, key_path=None, translation_review=None):
    """Bind exact fetched facts/media to a freshly measured, concrete ROM.

    ``rom_fingerprint`` is an internal alternative for a trusted ADB measurement
    boundary.  It must never be populated from a patch/catalog JSON claim.
    """
    system, file = _system(system), _relative(file)
    if PurePosixPath(file).suffix.lower() in {".m3u", ".cue", ".gdi", ".json"}:
        _fail("Playlist/descriptor identities require constituent-file review", "compound_rom_unconfirmed")
    measured = _fingerprint(fingerprint_file(rom_path), complete=True) if rom_path is not None else _fingerprint(rom_fingerprint, complete=True)
    if rom_path is not None and rom_fingerprint is not None and _fingerprint(rom_fingerprint, complete=True) != measured:
        _fail("Supplied fingerprint differs from actual ROM bytes", "rom_mismatch")
    _, candidate, matched = _matched_candidate(source_envelope, system, measured, key_path)
    approved = _approved_metadata(candidate, metadata, translation_review, source_envelope,
                                  system, file, measured, matched, key_path)
    approved_media = _media(media or [], system, file, candidate=candidate, key_path=key_path, require_download=True)
    if not approved and not approved_media:
        _fail("There is no concrete metadata/media payload to approve", "empty_approval")
    return seal_payload({
        "system": system, "file": file, "rom_fingerprint": measured,
        "provider": "screenscraper", "provider_game_id": str(candidate["provider_game_id"]),
        "method": "exact_returned_rom_hash_platform_size", "source": source_envelope,
        "matched_rom": matched, "metadata": approved, "media": approved_media,
        **({"translation_review": translation_review} if translation_review is not None else {}),
    }, "game_identity", key_path)


def verify_receipt(receipt, *, system, file, rom_fingerprint, metadata, media=None, key_path=None, target_binding=None):
    """Recheck seal, observed source, live ROM identity and exact approved writes."""
    system, file = _system(system), _relative(file)
    payload = verify_payload(receipt, "game_identity", key_path)
    if payload.get("system") != system or payload.get("file") != file:
        _fail("Identity receipt belongs to another platform or complete file path", "receipt_target_mismatch")
    frozen = _fingerprint(payload.get("rom_fingerprint"), complete=True)
    live = _fingerprint(rom_fingerprint)
    if any(frozen.get(key) != value for key, value in live.items()):
        _fail("ROM bytes changed; the old identity receipt cannot be reused", "rom_changed")
    _, candidate, matched = _matched_candidate(payload.get("source"), system, frozen, key_path)
    if payload.get("provider_game_id") != str(candidate["provider_game_id"]) or payload.get("provider") != "screenscraper" or payload.get("method") != "exact_returned_rom_hash_platform_size" or payload.get("matched_rom") != matched:
        _fail("Identity receipt no longer corresponds to its observed source", "invalid_identity_seal")
    approved = _approved_metadata(candidate, payload.get("metadata"), payload.get("translation_review"),
                                  payload.get("source"), system, file, frozen, matched, key_path, target_binding)
    if approved != metadata:
        _fail("Metadata write differs from the exact approved payload", "metadata_mismatch")
    signed_media = _media(payload.get("media"), system, file, candidate=candidate)
    for original, normalized in zip(sorted(payload["media"], key=lambda item: (item["type"], item["relative"])), signed_media):
        provenance = verify_payload(original.get("download_receipt"), "provider_download", key_path)
        if provenance.get("provider") != "screenscraper" or provenance.get("source_url") != normalized["source_url"] or provenance.get("sha256") != normalized["sha256"] or provenance.get("bytes", provenance.get("size")) != normalized["size"]:
            _fail("Approved media source evidence is no longer valid", "media_source_unconfirmed")
    actual_media = _media(media or [], system, file)
    if signed_media != actual_media:
        _fail("Media write differs from the exact approved bytes/destination", "media_mismatch")
    return payload


def build_catalog(receipts, key_path=None):
    """Convert already verified exact receipts to the shared strict catalog."""
    if not isinstance(receipts, list) or not receipts:
        _fail("At least one exact identity receipt is required", "invalid_catalog")
    entries, seen = [], set()
    for receipt in receipts:
        payload = verify_payload(receipt, "game_identity", key_path)
        payload = verify_receipt(receipt, system=payload.get("system"), file=payload.get("file"),
                                 rom_fingerprint=payload.get("rom_fingerprint"),
                                 metadata=payload.get("metadata"), media=payload.get("media"), key_path=key_path)
        target = (payload["system"], payload["file"])
        if target in seen:
            _fail("Duplicate identity catalog target", "invalid_catalog")
        seen.add(target)
        entries.append({"system": payload["system"], "file": payload["file"],
                        "rom_fingerprint": payload["rom_fingerprint"], "receipt": copy.deepcopy(receipt)})
    return {"schema_version": SCHEMA_VERSION, "entries": sorted(entries, key=lambda item: (item["system"], item["file"]))}


def _load_json(path):
    path = Path(path)
    if path.stat().st_size > 24000000:
        _fail("Identity input exceeds the supported JSON size", "invalid_evidence")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        _fail("Identity input must be valid UTF-8 JSON", "invalid_evidence")


def load_exact_patch(path, file):
    patch = _load_json(path)
    if isinstance(patch, dict) and set(patch) == {"games"}:
        patch = patch["games"]
    if isinstance(patch, list):
        if len(patch) != 1:
            _fail("Authorize one concrete ROM at a time", "invalid_patch")
        patch = patch[0]
    if not isinstance(patch, dict) or set(patch) != {"file", "metadata"} or patch.get("file") != file:
        _fail("Patch must name only this complete ROM file and its exact metadata", "invalid_patch")
    return _object(patch["metadata"], "Patch metadata")


def load_exact_media(path):
    if path is None:
        return []
    path = Path(path)
    media = _load_json(path)
    if isinstance(media, dict) and set(media) == {"files"}:
        media = media["files"]
    if not isinstance(media, list):
        _fail("Media input must be a list or a files manifest", "media_mismatch")
    result = copy.deepcopy(media)
    for item in result:
        _object(item, "Media item")
        if item.get("path") is not None:
            target = Path(item["path"])
            if not target.is_absolute():
                target = path.parent / target
            item["path"] = str(target)
    return result


def write_catalog(path, catalog):
    """Exclusive new output; a failed approval never overwrites an old receipt."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        _fail("Existing identity catalog must not be replaced; use a separate output", "output_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(catalog, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Approve exact fetched ES-DE facts for measured ROM bytes; missing evidence stays pending")
    commands = parser.add_subparsers(dest="command", required=True)
    authorize = commands.add_parser("authorize", help="Create an exact write catalog from an already signed official provider response")
    authorize.add_argument("--source", required=True, help="Signed query JSON produced by the provider's real HTTPS response handler")
    authorize.add_argument("--system", required=True)
    authorize.add_argument("--file", required=True, help="Complete relative ROM path under its platform directory")
    authorize.add_argument("--rom", required=True, help="Actual local regular ROM file to measure, never a name/hash JSON")
    authorize.add_argument("--patch", required=True, help="One exact {file,metadata} patch, or a one-game games list")
    authorize.add_argument("--translation-review", help="Independent translation review envelope; never provider evidence")
    authorize.add_argument("--media", help="Selected media list with actual files and signed provider download receipts")
    authorize.add_argument("--identity-key", help="Trusted private key path; never accepted from provider/patch/catalog JSON")
    authorize.add_argument("--out", required=True, help="New identity catalog output; existing files are not replaced")
    args = parser.parse_args(argv)
    try:
        source = _load_json(args.source)
        if isinstance(source, dict) and "identity_source" in source:
            source = source["identity_source"]
        metadata = load_exact_patch(args.patch, args.file)
        media = load_exact_media(args.media)
        receipt = authorize_patch(source, system=args.system, file=args.file, rom_path=args.rom,
                                  metadata=metadata, media=media, key_path=args.identity_key,
                                  translation_review=_load_json(args.translation_review) if args.translation_review else None)
        catalog = build_catalog([receipt], key_path=args.identity_key)
        write_catalog(args.out, catalog)
        print(json.dumps({"status": "approved", "system": args.system, "file": args.file,
                          "method": receipt["payload"]["method"], "catalog": str(Path(args.out)),
                          "approved_metadata_fields": sorted(metadata), "approved_media": len(media)}, ensure_ascii=False))
        return 0
    except (IdentityError, OSError, ValueError) as error:
        print(json.dumps({"status": "pending_identity", "code": getattr(error, "code", "invalid_identity_input"),
                          "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
