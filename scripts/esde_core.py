"""ES-DE XML and inventory operations; no device writes or network requests."""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET

CORE_MEDIA = ("covers", "screenshots", "titlescreens", "marquees", "miximages", "videos")
PROTECTED_FIELDS = ("altemulator", "sortname", "playcount", "playtime", "lastplayed", "favorite", "kidgame", "hidden", "broken", "nomultiscrape", "folderlink", "controller", "physicalmedia")
METADATA_FIELDS = ("name", "desc", "developer", "publisher", "genre", "players", "releasedate", "rating", "scrapername")
MEDIA_XML_FIELDS = ("image", "thumbnail", "marquee", "video", "fanart", "titlescreen", "manual")
IGNORED_DIRS = {"media", "images", "videos", "covers", "screenshots", "titlescreens", "marquees", "miximages", "assets", ".git", ".es-de"}
EXTENSIONS = {
    "famicom": ".zip .7z .nes .fds .unf .unif", "nes": ".zip .7z .nes .fds .unf .unif",
    "fbneo": ".zip .7z .neo", "mame": ".zip .7z .cmd", "arcade": ".zip .7z",
    "dreamcast": ".cdi .chd .cue .gdi .iso .m3u", "gb": ".zip .7z .gb .sgb",
    "gbc": ".zip .7z .gbc .gb .sgb", "gba": ".zip .7z .gba",
    "gc": ".ciso .gcm .gcz .iso .json .rvz .tgc .wad .wbfs .wia .m3u",
    "wii": ".ciso .gcz .iso .rvz .wad .wbfs .wia .m3u", "j2me": ".jar .jlmod .7z .zip",
    "n3ds": ".3ds .3dsx .app .cci .cxi .7z .zip", "nds": ".nds .7z .zip",
    "ps3": ".ps3 .iso", "switch": ".nsp .xci .nro .nca", "wiiu": ".rpx .tmd .wua .wud .wuhb .wux",
    "psx": ".bin .chd .cue .ecm .iso .m3u .mdf .pbp .zip .7z", "ps2": ".bin .chd .ciso .cso .dump .gz .img .iso .m3u .mdf .nrg",
    "psp": ".chd .cso .iso .pbp .7z .zip", "sfc": ".smc .sfc .swc .zip .7z", "snes": ".smc .sfc .swc .zip .7z",
    "megadrive": ".bin .gen .md .smd .zip .7z", "saturn": ".chd .cue .iso .m3u .mdf .zip .7z",
    "n64": ".n64 .v64 .z64 .zip .7z", "wonderswan": ".ws .zip .7z", "wonderswancolor": ".wsc .ws .zip .7z",
    "gamegear": ".gg .zip .7z", "mastersystem": ".sms .zip .7z", "pcengine": ".pce .sgx .zip .7z",
}
EXTENSIONS = {k: set(v.split()) for k, v in EXTENSIONS.items()}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def parse_document(data):
    """ES-DE may have several top-level XML nodes; preserve all of them."""
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise ValueError("DTD/entity declarations are unsupported in gamelists")
    data = re.sub(r"<\?xml\b.*?\?>", "", data, flags=re.S)
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    document = ET.fromstring("<esdeDocument>" + data + "</esdeDocument>", parser=parser)
    lists = document.findall("gameList")
    if len(lists) != 1:
        raise ValueError("Expected exactly one gameList node")
    return document


def serialize_document(document):
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + b"\n".join(ET.tostring(node, encoding="utf-8") for node in document) + b"\n"


def cjk(value):
    return any("\u3400" <= c <= "\u9fff" for c in (value or ""))


def clean_relative(path):
    path = str(path).replace("\\", "/")
    if "\x00" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return None
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts) if parts else None


def canonical_reference(path, system_root, actual=None):
    """Resolve only beneath the caller's explicit root; never infer another device."""
    path = str(path or "").replace("\\", "/")
    root = str(system_root).replace("\\", "/").rstrip("/")
    absolute = path.startswith("/") or bool(re.match(r"^[A-Za-z]:/", path))
    if absolute:
        prefix = root + "/"
        if path.startswith(prefix):
            relative = path[len(prefix):]
        else:
            # Windows paths are case-insensitive. Android system directory names
            # may vary in case (PS3/ps3); do not case-fold the file identity.
            path_parent, _, _ = root.rpartition("/")
            candidate_root = path[:len(root)]
            same_root = candidate_root.casefold() == root.casefold() if re.match(r"^[A-Za-z]:/", root) else candidate_root.rpartition("/")[0] == path_parent and candidate_root.rpartition("/")[2].casefold() == root.rpartition("/")[2].casefold()
            if not same_root or path[len(root):len(root) + 1] != "/":
                return None
            relative = path[len(root) + 1:]
    else:
        relative = path
    relative = clean_relative(relative)
    if not relative:
        return None
    if actual is None or relative in actual:
        return relative
    # Repair a case discrepancy only when there is one unambiguous actual file.
    matches = [file for file in actual if file.casefold() == relative.casefold()]
    return matches[0] if len(matches) == 1 else None


def extension_map(overrides=None, esde_root=None):
    result = {key: set(value) for key, value in EXTENSIONS.items()}
    if esde_root:
        path = Path(esde_root) / "custom_systems" / "es_systems.xml"
        if path.is_file():
            data = path.read_bytes().decode("utf-8-sig")
            if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
                raise ValueError("DTD/entity declarations unsupported in es_systems.xml")
            root = ET.fromstring(data)
            for node in root.findall("system"):
                name = node.findtext("name", "").strip()
                values = node.findtext("extension", "").split()
                if name and values:
                    result[name.casefold()] = {value.casefold() for value in values}
    for system, values in (overrides or {}).items():
        if isinstance(values, str):
            values = values.replace(",", " ").split()
        if not isinstance(values, list) and not isinstance(values, set):
            raise ValueError("Extensions must be a string or list for " + system)
        normalized = {str(value).casefold() for value in values}
        if any(not value.startswith(".") or "/" in value or "\\" in value for value in normalized):
            raise ValueError("Invalid extension override for " + system)
        result[system.casefold()] = normalized
    return result


def walk_files(root):
    root = Path(root)
    if not root.is_dir():
        raise ValueError("Directory does not exist: " + str(root))
    for parent, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d.casefold() not in IGNORED_DIRS and not Path(parent, d).is_symlink())
        for name in sorted(names):
            path = Path(parent, name)
            if not path.is_symlink():
                yield path


def actual_files(system_root, extensions):
    return sorted(path.relative_to(system_root).as_posix() for path in walk_files(system_root) if path.suffix.casefold() in extensions)


def metadata_issues(fields, language="zh"):
    result = []
    for tag in METADATA_FIELDS:
        if tag in ("rating", "scrapername"):
            continue
        value = fields.get(tag, "").strip()
        if not value:
            # Missing historical facts remain unknown, rather than encouraging
            # guessed dates/companies/player counts merely to pass validation.
            if tag in ("name", "desc"):
                result.append("missing_" + tag)
        elif value.casefold() in {"未知", "不详", "待核实", "未确认", "unknown"}:
            if tag in ("name", "desc"):
                result.append("unverified_" + tag)
        elif language == "zh" and tag == "desc" and not cjk(fields[tag]):
            result.append("non_chinese_desc")
        elif language == "zh" and tag in ("name", "developer", "publisher", "genre") and re.search("[A-Za-z]", fields[tag]) and not cjk(fields[tag]):
            result.append("non_chinese_" + tag)
    return result


def group_games(document, system_root, actual):
    grouped = collections.defaultdict(list)
    unresolved = []
    for index, node in enumerate(document.find("gameList").findall("game")):
        path = node.findtext("path", "")
        key = canonical_reference(path, system_root, actual)
        if key is None or key not in actual:
            unresolved.append({"index": index, "path": path, "reason": "outside_explicit_root_or_missing_actual_file"})
        else:
            grouped[key].append(node)
    return grouped, unresolved


def ranked_nodes(nodes, relative):
    """Prefer live canonical relative history, then newest lastplayed, never add."""
    indexed = list(enumerate(nodes))
    return [node for _, node in sorted(indexed, key=lambda pair: (pair[1].findtext("path", "") != "./" + relative, tuple(-ord(c) for c in pair[1].findtext("lastplayed", "").ljust(32, "\x00")), pair[0]))]


def merged_game(nodes, relative):
    sources = ranked_nodes(nodes, relative)
    result = ET.Element("game")
    ET.SubElement(result, "path").text = "./" + relative
    conflicts = []
    attributes = {}
    for source in reversed(sources):
        attributes.update(source.attrib)
    result.attrib.update(attributes)
    tags = list(dict.fromkeys(child.tag for node in sources for child in node if isinstance(child.tag, str) and child.tag != "path"))
    if sources:
        result.extend(copy.deepcopy(child) for child in sources[0] if not isinstance(child.tag, str))
    for tag in tags:
        candidates = [child for node in sources for child in node if child.tag == tag]
        nonempty = [child for child in candidates if (child.text or "").strip() or len(child) or child.attrib]
        chosen = (nonempty or candidates)[0]
        known = tag in {*METADATA_FIELDS, *MEDIA_XML_FIELDS, *PROTECTED_FIELDS}
        # Unknown/private elements can legitimately repeat. Preserve the whole
        # group from the highest-priority source instead of silently dropping it.
        chosen_group = [chosen]
        if not known:
            chosen_source = next(node for node in sources if any(child is chosen for child in node))
            chosen_group = [child for child in chosen_source if child.tag == tag]
        result.extend(copy.deepcopy(child) for child in chosen_group)
        values = list(dict.fromkeys(ET.tostring(child, encoding="unicode") for child in nonempty))
        if len(values) > 1:
            conflicts.append({"file": relative, "tag": tag, "chosen": ET.tostring(chosen, encoding="unicode"), "values": values, "policy": "canonical_relative_then_latest_lastplayed_first_nonempty; never summed"})
    for attribute in set(key for node in sources for key in node.attrib):
        values = list(dict.fromkeys(node.attrib[attribute] for node in sources if attribute in node.attrib))
        if len(values) > 1:
            conflicts.append({"file": relative, "tag": "@" + attribute, "chosen": result.attrib[attribute], "values": values})
    return result, conflicts


def normalize_document(document, system_root, actual, patches=None):
    document = copy.deepcopy(document)
    root = document.find("gameList")
    grouped, unresolved = group_games(document, system_root, actual)
    report = {"actual_files": len(actual), "created": 0, "merged_duplicate_nodes": 0, "normalized_references": 0, "conflicts": [], "unresolved_entries": unresolved, "protected_fields": list(PROTECTED_FIELDS), "patched": 0}
    patch_map = {}
    for patch in patches or []:
        file = clean_relative(patch.get("file", ""))
        if file not in actual:
            raise ValueError("Patch must identify an existing actual ROM file: " + str(patch.get("file")))
        if file in patch_map:
            raise ValueError("Duplicate patch file: " + file)
        metadata = patch.get("metadata", {})
        if not isinstance(metadata, dict) or set(metadata) - set(METADATA_FIELDS):
            raise ValueError("Patch metadata permits only factual metadata fields: " + file)
        if any(value is not None and not isinstance(value, str) for value in metadata.values()):
            raise ValueError("Patch metadata values must be strings or null: " + file)
        patch_map[file] = metadata
    for relative in sorted(actual):
        nodes = grouped.get(relative, [])
        new, conflicts = merged_game(nodes, relative) if nodes else (ET.Element("game"), [])
        if not nodes:
            ET.SubElement(new, "path").text = "./" + relative
            report["created"] += 1
        else:
            report["merged_duplicate_nodes"] += max(0, len(nodes) - 1)
            report["normalized_references"] += sum(node.findtext("path", "") != "./" + relative for node in nodes)
        report["conflicts"].extend(conflicts)
        for tag, value in patch_map.get(relative, {}).items():
            node = new.find(tag)
            if node is None:
                node = ET.SubElement(new, tag)
            node.text = value
        report["patched"] += relative in patch_map
        if nodes:
            position = list(root).index(nodes[0])
            for node in nodes:
                root.remove(node)
            root.insert(position, new)
        else:
            root.append(new)
    return document, report


def preserved_snapshot(document, system_root, actual):
    groups, _ = group_games(document, system_root, actual)
    result = {}
    metadata_tags = {"path", *METADATA_FIELDS, *MEDIA_XML_FIELDS}
    for file, nodes in groups.items():
        merged, _ = merged_game(nodes, file)
        fields = {}
        for child in merged:
            if isinstance(child.tag, str) and child.tag not in metadata_tags:
                fields[child.tag] = fields.get(child.tag, "") + ET.tostring(child, encoding="unicode")
        result[file] = {"attributes": dict(merged.attrib), "fields": fields}
    return result


def audit_system(system, actual, system_root, gamelist, media_index, checks=("structure", "metadata", "media"), language="zh"):
    document = parse_document(Path(gamelist).read_bytes()) if Path(gamelist).is_file() else None
    grouped, unresolved = group_games(document, system_root, actual) if document is not None else ({}, [])
    records = []
    for file in sorted(actual):
        nodes = grouped.get(file, [])
        node = merged_game(nodes, file)[0] if nodes else None
        fields = {tag: node.findtext(tag, "") or "" if node is not None else "" for tag in METADATA_FIELDS}
        issues = metadata_issues(fields, language) if "metadata" in checks else []
        if "structure" in checks and not nodes:
            issues.insert(0, "missing_entry")
        elif "structure" in checks and len(nodes) != 1:
            issues.insert(0, "duplicate_actual_reference")
        if "structure" in checks and any(n.findtext("path", "") != "./" + file for n in nodes):
            issues.append("noncanonical_reference")
        stem = str(PurePosixPath(file).with_suffix(""))
        media = {kind: media_index.get((system.casefold(), kind, stem), []) for kind in CORE_MEDIA}
        missing = [kind for kind, candidates in media.items() if not any(candidate.get("size", 0) > 0 for candidate in candidates)]
        unknown_fields = [tag for tag, value in fields.items() if tag not in ("rating", "scrapername") and (not value.strip() or value.strip().casefold() in {"未知", "不详", "待核实", "未确认", "unknown"})]
        records.append({"id": system + ":" + file, "system": system, "file": file, "path": "./" + file, "actual_reference_count": len(nodes), **fields, "issues": issues, "unknown_fields": unknown_fields, "missing_media": missing if "media" in checks else [], "observed_missing_media": missing, "media": media, "check_scope": sorted(checks), "protected": {tag: node.findtext(tag) for tag in PROTECTED_FIELDS} if node is not None else {}})
    counter = collections.Counter(issue for record in records for issue in record["issues"])
    counter.update("media_" + kind for record in records for kind in record["missing_media"])
    return records, {"system": system, "games": len(records), "with_issues": sum(bool(r["issues"] or r["missing_media"]) for r in records), "unresolved_entries": unresolved, "issue_counts": dict(counter)}


def index_media(files, media_root):
    index = collections.defaultdict(list)
    prefix = str(media_root).replace("\\", "/").rstrip("/") + "/"
    for item in files:
        path = str(item["path"]).replace("\\", "/")
        if not path.startswith(prefix):
            continue
        parts = path[len(prefix):].split("/", 2)
        if len(parts) != 3 or parts[1] not in CORE_MEDIA:
            continue
        system, kind, relative = parts
        key = (system.casefold(), kind, str(PurePosixPath(relative).with_suffix("")))
        index[key].append({"path": path, "size": item.get("size", 0)})
    return index
