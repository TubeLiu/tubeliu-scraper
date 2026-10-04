#!/usr/bin/env python3
"""Plan, apply, verify and roll back finite ES-DE file changes with immutable backups."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from esde import Adb, AdbError, Reporter, now, remote_root
from esde_core import CORE_MEDIA, PROTECTED_FIELDS, METADATA_FIELDS, canonical_reference, merged_game, parse_document, sha256, write_json, load_json


class DeploymentError(RuntimeError):
    pass


class ThemeReadOnlyError(DeploymentError):
    pass


def identity_api():
    # Import lazily so inspections do not create or read an identity key.
    import identity
    return identity


def catalog_entries(data):
    from esde_core import identity_catalog_entries
    try:
        validated = identity_catalog_entries(data)
    except (ValueError, TypeError, AttributeError) as error:
        raise DeploymentError("Identity catalog has an unsupported or ambiguous schema") from error
    result = {}
    for entry in validated.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("receipt"), dict):
            raise DeploymentError("Identity catalog entries require signed receipts")
        payload = entry["receipt"].get("payload", {})
        if not isinstance(payload, dict):
            raise DeploymentError("Identity receipt payload must be an object")
        for field in ("system", "file", "rom_fingerprint"):
            if entry.get(field) != payload.get(field):
                raise DeploymentError("Catalog wrapper does not match its signed identity")
        key = (entry["system"], entry["file"])
        if key in result:
            raise DeploymentError("Identity catalog has ambiguous duplicate games")
        relative_path(entry["system"] + "/" + entry["file"])
        result[key] = entry
    return result


def xml_node_value(node):
    tag = node.tag if isinstance(node.tag, str) else "#comment"
    text = node.text or ""
    if len(node) and not text.strip():
        text = ""
    return (tag, tuple(sorted(node.attrib.items())), text, tuple(xml_node_value(child) for child in node))


def xml_fields(node):
    fields = {}
    for child in node:
        if not isinstance(child.tag, str):
            fields.setdefault("#comments", []).append(xml_node_value(child))
            continue
        if child.tag in {*METADATA_FIELDS, *PROTECTED_FIELDS, "path"}:
            if child.tag in fields or len(child) or child.attrib:
                raise DeploymentError("Ambiguous or nested factual/history fields require a separate explicit repair")
            fields[child.tag] = child.text or ""
        else:
            # Repeated private elements are preserved as complete groups.
            fields.setdefault(child.tag, []).append(xml_node_value(child))
    return fields


def xml_changes(original, prepared, system, rom_root, actual=None):
    """Compare content, not formatting; paths can normalize within one ROM root.

    No source manifest can classify an arbitrary changed title as structural.
    Protected history and unknown game fields cannot be changed by deployment.
    """
    old = parse_document(original) if original is not None else ET.fromstring("<esdeDocument><gameList /></esdeDocument>")
    new = parse_document(prepared)
    root = str(rom_root or "").rstrip("/\\") + "/" + system
    def games(document, merge=False):
        grouped = {}
        for node in document.find("gameList").findall("game"):
            file = canonical_reference(node.findtext("path", ""), root, actual)
            if not file:
                raise DeploymentError("Game path is outside the sealed ROM root")
            relative_path(file)
            grouped.setdefault(file, []).append(node)
        values = {}
        for file, nodes in grouped.items():
            if len(nodes) > 1 and not merge:
                raise DeploymentError("Prepared output still has duplicate game identities")
            node = merged_game(nodes, file)[0] if merge else nodes[0]
            values[file] = (node, xml_fields(node))
        return values
    before, after = games(old, True), games(new)
    if set(before) - set(after):
        raise DeploymentError("Deployment cannot drop original game or history records")
    # Preserve non-game nodes, including alternativeEmulator and folder records.
    def side_nodes(document):
        values = []
        for node in document:
            if node.tag == "gameList":
                values.extend(xml_node_value(child) for child in node if child.tag != "game")
            else:
                values.append(xml_node_value(node))
        return values
    if side_nodes(old) != side_nodes(new):
        raise DeploymentError("Unrelated XML records cannot change during game deployment")
    changes = []
    for file, (node, fields) in after.items():
        previous_node, previous = before.get(file, (None, {}))
        if previous_node is not None and previous_node.attrib != node.attrib:
            raise DeploymentError("Game XML attributes cannot change without an explicit supported identity mapping")
        delta = {name: fields.get(name, "") for name in set(previous) | set(fields) if name != "path" and fields.get(name, "") != previous.get(name, "")}
        if any(name in PROTECTED_FIELDS for name in delta):
            raise DeploymentError("Prepared XML changed protected gameplay history/settings")
        if any(name not in METADATA_FIELDS for name in delta):
            raise DeploymentError("Unknown/media XML fields cannot be altered without supported identity lineage")
        if delta:
            changes.append({"system": system, "file": file, "metadata": delta, "after": fields})
        elif previous_node is None and set(fields) - {"path"}:
            # A new record with copied fields still requires identity approval.
            changes.append({"system": system, "file": file, "metadata": {name: value for name, value in fields.items() if name != "path"}, "after": fields})
    return changes


def rom_fingerprint(target_config, target, system, file):
    root = target_config.get("rom_root")
    if not root:
        raise DeploymentError("Changed game metadata/media requires an explicit --rom-root")
    relative_path(system + "/" + file)
    if target_config["type"] == "local":
        root = Path(root).resolve()
        path = root
        for part in (system + "/" + file).split("/"):
            path /= part
            if path.is_symlink():
                raise DeploymentError("ROM identity path or parent cannot be a symlink")
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise DeploymentError("Identity ROM is absent from the explicit target library")
        return identity_api().fingerprint_file(path)
    # Only read the exact ROM from the already selected serial. Never copy ROMs
    # into the run directory or infer a different device/library.
    path = root + "/" + system + "/" + file
    parts = " ".join(shlex.quote(part) for part in (system + "/" + file).split("/"))
    check = "set -eu; test ! -L " + shlex.quote(root) + "; test -d " + shlex.quote(root) + "; test \"$(realpath " + shlex.quote(root) + ")\" = " + shlex.quote(root) + "; p=" + shlex.quote(root) + "; for part in " + parts + '; do p="$p/$part"; test ! -L "$p" || exit 31; done; '
    # Hashes returned by Android are recomputed from the target, not copied from
    # the candidate report. sha256+size is enough to detect a stale ROM receipt.
    quoted = shlex.quote(path)
    check += "test -f " + quoted + "; test \"$(realpath " + quoted + ")\" = " + quoted + "; before=$(stat -c '%d:%i:%s:%Y:%y' " + quoted + "); digest=$(sha256sum " + quoted + "); size=$(wc -c < " + quoted + "); after=$(stat -c '%d:%i:%s:%Y:%y' " + quoted + "); test \"$before\" = \"$after\"; printf '%s\\n%s\\n%s\\n%s\\n' \"$before\" \"${digest%% *}\" \"$size\" \"$after\""
    result = target.adb.shell_bytes(check).decode("ascii").splitlines()
    if len(result) != 4 or result[0] != result[3] or not re.fullmatch(r"[0-9a-f]{64}", result[1]) or not result[2].strip().isdigit() or result[0].split(":", 3)[2] != result[2].strip():
        raise DeploymentError("Selected Android ROM fingerprint could not be read")
    return {"sha256": result[1], "size": int(result[2])}


def rom_inventory(target_config, target, system):
    """Actual platform files, used to reject a media stem shared by two ROMs."""
    if not target_config.get("rom_root"):
        return None
    from esde_core import extension_map, walk_files
    extensions = extension_map().get(system.casefold())
    if not extensions:
        raise DeploymentError("Identity deployment requires a known explicit platform extension set")
    root = str(target_config["rom_root"]).replace("\\", "/").rstrip("/") + "/" + system
    if target_config["type"] == "local":
        root = Path(root)
        return sorted(path.relative_to(root).as_posix() for path in walk_files(root) if path.suffix.casefold() in extensions)
    result = []
    for entry in target.adb.inventory(root):
        path = entry.get("path", "")
        if not path.startswith(root + "/"):
            raise DeploymentError("Android ROM inventory escaped the sealed platform root")
        file = path[len(root) + 1:]
        relative_path(file)
        if PurePosixPath(file).suffix.casefold() in extensions:
            result.append(file)
    return sorted(set(result))


def build_identity_gate(run, plan, target, catalog_path=None, key_path=None):
    """Authorize every changed game and each exact media file before any write."""
    changes, media_files, inventories = [], [], {}
    for item in plan["files"]:
        if item["scope"] != "esde" or item["original"]["sha256"] == item["desired_sha256"]:
            continue
        if item["relative"].startswith("gamelists/"):
            original = (Path(run) / item["backup"]).read_bytes() if item["original"]["exists"] else None
            system = item["relative"].split("/")[1]
            if system not in inventories:
                inventories[system] = rom_inventory(plan["target"], target, system)
            actual = inventories[system]
            changes.extend(xml_changes(original, Path(item["local"]).read_bytes(), system, plan["target"].get("rom_root"), actual))
        else:
            media_files.append(item)
    if not changes and not media_files:
        return {"schema_version": 1, "status": "pass", "mode": "structure_or_theme_only", "games": [], "media": [], "key_path": str(Path(key_path).resolve()) if key_path else None}
    if not catalog_path:
        raise DeploymentError("Changed game metadata/media requires a signed identity catalog; name similarity and operator notes do not authorize writes")
    catalog_path = Path(catalog_path).resolve()
    catalog_hash = file_hash(catalog_path)
    entries = catalog_entries(load_json(catalog_path))
    requested = {(change["system"], change["file"]): change for change in changes}
    bindings = []
    for item in media_files:
        _, system, kind, *suffix = item["relative"].split("/")
        stem = str(PurePosixPath("/".join(suffix)).with_suffix(""))
        if system not in inventories:
            inventories[system] = rom_inventory(plan["target"], target, system)
        matching_roms = [file for file in (inventories[system] or []) if str(PurePosixPath(file).with_suffix("")).casefold() == stem.casefold()]
        if len(matching_roms) != 1:
            raise DeploymentError("Media stem is absent or shared by different actual ROM files; resolve it before writing: " + item["relative"])
        matches = [(key, entry) for key, entry in entries.items() if key[0] == system and str(PurePosixPath(key[1]).with_suffix("")).casefold() == stem.casefold()]
        if len(matches) != 1:
            raise DeploymentError("Media target does not unambiguously match one signed ROM stem: " + item["relative"])
        key, entry = matches[0]
        if key[1] != matching_roms[0]:
            raise DeploymentError("Signed media ROM differs from the actual inventory file")
        payload = entry["receipt"].get("payload", {})
        candidates = [candidate for candidate in payload.get("media", []) if candidate.get("type", candidate.get("kind")) == kind and candidate.get("relative") == item["relative"].removeprefix("downloaded_media/") and candidate.get("sha256") == item["desired_sha256"] and candidate.get("size") == item["size"]]
        if len(candidates) != 1:
            raise DeploymentError("Media bytes/kind/target lack matching signed download lineage: " + item["relative"])
        bindings.append({"system": key[0], "file": key[1], "relative": item["relative"], "type": kind, "sha256": item["desired_sha256"], "size": item["size"]})
        requested.setdefault(key, {"system": key[0], "file": key[1], "metadata": {}, "after": {}})
    games = []
    for key, change in requested.items():
        entry = entries.get(key)
        if entry is None:
            raise DeploymentError("Changed metadata has no signed ROM identity: " + ":".join(key))
        payload = entry["receipt"].get("payload", {})
        approved = payload.get("metadata", {})
        if any(name not in approved or (approved[name] or "") != value for name, value in change["metadata"].items()):
            raise DeploymentError("Prepared game facts differ from the approved identity receipt: " + ":".join(key))
        fingerprint = rom_fingerprint(plan["target"], target, *key)
        try:
            verified = identity_api().verify_receipt(entry["receipt"], system=key[0], file=key[1], rom_fingerprint=fingerprint, metadata=approved, media=payload.get("media", []), key_path=key_path)
        except (ValueError, OSError) as error:
            raise DeploymentError("Identity verification blocked " + ":".join(key) + ": " + str(error)) from error
        games.append({"system": key[0], "file": key[1], "rom_fingerprint": fingerprint, "metadata": change["metadata"], "receipt_sha256": sha256(json.dumps(entry["receipt"], sort_keys=True, ensure_ascii=False).encode("utf-8"))})
    return {"schema_version": 1, "status": "pass", "mode": "signed_identity", "catalog": str(catalog_path), "catalog_sha256": catalog_hash, "key_path": str(Path(key_path).resolve()) if key_path else None, "games": games, "media": bindings}


def recheck_identity_gate(run, plan, target):
    gate = plan.get("identity_gate")
    if not isinstance(gate, dict) or gate.get("status") != "pass":
        raise DeploymentError("This deployment plan has no machine identity gate; replan with signed identity evidence")
    catalog = gate.get("catalog")
    if catalog and (not Path(catalog).is_file() or file_hash(catalog) != gate.get("catalog_sha256")):
        raise DeploymentError("Frozen identity catalog changed or disappeared after planning")
    checked = build_identity_gate(run, plan, target, catalog, gate.get("key_path"))
    if checked != gate:
        raise DeploymentError("Identity coverage or actual ROM fingerprint changed after planning")
    return checked


def install_identity_commit_guard(plan, target):
    def check(items):
        selected = set()
        inventory_cache = {}
        for item in items:
            if item["scope"] != "esde":
                continue
            if item["relative"].startswith("gamelists/"):
                system = item["relative"].split("/")[1]
                selected.update((game["system"], game["file"]) for game in plan["identity_gate"]["games"] if game["system"] == system)
            else:
                for binding in plan["identity_gate"]["media"]:
                    if binding["relative"] != item["relative"]:
                        continue
                    system = binding["system"]
                    if system not in inventory_cache:
                        inventory_cache[system] = rom_inventory(plan["target"], target, system)
                    stem = str(PurePosixPath(binding["file"]).with_suffix(""))
                    matches = [file for file in inventory_cache[system] if str(PurePosixPath(file).with_suffix("")).casefold() == stem.casefold()]
                    if matches != [binding["file"]]:
                        raise DeploymentError("ROM inventory now has an ambiguous or missing media stem; no media will be committed")
                    selected.add((system, binding["file"]))
        for game in plan["identity_gate"]["games"]:
            key = (game["system"], game["file"])
            if key in selected and rom_fingerprint(plan["target"], target, *key) != game["rom_fingerprint"]:
                raise DeploymentError("ROM changed immediately before a game write; no further targets will be installed")
    target.identity_before_commit = check


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def batches(values, size=50):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def describe_all(target, items):
    return target.describe_many(items) if hasattr(target, "describe_many") else [target.describe(item) for item in items]


def start_deadline(process, seconds=600):
    """Bound streaming writes/reads too, not just communicate after the stream."""
    def terminate():
        try:
            process.kill()
        except OSError:
            pass
    timer = threading.Timer(seconds, terminate)
    timer.daemon = True
    timer.start()
    return timer


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or any(c in value for c in ("\x00", "\n", "\r")):
        raise DeploymentError("Relative targets must be finite POSIX paths without control characters")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or value.startswith("/") or ":" in parts[0]:
        raise DeploymentError("Absolute paths and parent traversal are forbidden in manifest relative paths")
    return parts


def validate_relative(value, scope="esde"):
    parts = relative_path(value)
    if scope == "theme":
        if any(part.startswith(".") for part in parts) or PurePosixPath(value).suffix.casefold() not in {".xml", ".json", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".mp4", ".webm", ".ttf", ".otf", ".woff", ".woff2"}:
            raise DeploymentError("Theme manifests permit only explicit theme XML/data, image, video and font files")
        if parts[-1].casefold() == "gamelist.xml" or any(part.casefold() in {"gamelists", "downloaded_media"} for part in parts):
            raise DeploymentError("Game metadata/media destinations cannot be classified as theme files")
    elif scope == "esde":
        gamelist = len(parts) == 3 and parts[0] == "gamelists" and parts[2] == "gamelist.xml"
        media = len(parts) >= 4 and parts[0] == "downloaded_media" and parts[2] in CORE_MEDIA
        if not gamelist and not media:
            raise DeploymentError("Only gamelists/SYSTEM/gamelist.xml and six core downloaded_media paths may be deployed")
        if parts[1].startswith("."):
            raise DeploymentError("Invalid system directory")
    else:
        raise DeploymentError("Scope must be esde or theme")
    return value


def backup_path(run, relative):
    root = Path(run).resolve()
    parts = relative_path(relative)
    path = root
    for part in parts:
        path /= part
        if path.is_symlink():
            raise DeploymentError("Original backup and its parents cannot be symlinks")
    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        raise DeploymentError("Original backup resolves outside its run") from error
    return path


def guard_theme_destination(target, target_config, item):
    if item["scope"] != "theme":
        return
    path = str(target.path(item)).replace("\\", "/").rstrip("/")
    parts = path.split("/")
    if any(part.casefold() in {"gamelists", "downloaded_media"} for part in parts) or parts[-1].casefold() == "gamelist.xml":
        raise DeploymentError("Theme destination crosses a game metadata/media directory")
    base = str(target_config["roots"]["esde"]).replace("\\", "/").rstrip("/")
    if target_config["type"] == "local":
        path, base = path.casefold(), base.casefold()
    for name in ("gamelists", "downloaded_media"):
        forbidden = base + "/" + name
        if path == forbidden or path.startswith(forbidden + "/"):
            raise DeploymentError("Theme scope cannot bypass game identity in a managed directory")


class LocalTarget:
    def __init__(self, roots):
        self.roots = {scope: Path(root).resolve() for scope, root in roots.items()}
        for root in self.roots.values():
            if not root.is_dir():
                raise DeploymentError("Explicit target root must be an existing directory: " + str(root))

    def path(self, item):
        root = self.roots[item["scope"]]
        path = root / Path(item["relative"])
        current = root
        for part in item["relative"].split("/"):
            current /= part
            if current.is_symlink():
                raise DeploymentError("Managed target or a parent cannot be a symlink: " + item["relative"])
        try:
            path.resolve().relative_to(root)
        except ValueError as error:
            raise DeploymentError("Managed target resolves outside its explicit root") from error
        return path

    def describe(self, item):
        path = self.path(item)
        if not path.exists():
            return {"exists": False, "sha256": None}
        if not path.is_file():
            raise DeploymentError("Managed target is not a regular file: " + item["relative"])
        stat = path.stat()
        return {"exists": True, "sha256": file_hash(path), "size": stat.st_size, "mode": stat.st_mode & 0o777, "mtime_ns": stat.st_mtime_ns}

    def backup(self, item, local):
        shutil.copyfile(self.path(item), local)

    def assert_writable(self, item):
        path = self.path(item)
        parent = path.parent
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK):
            if item["scope"] == "theme":
                raise ThemeReadOnlyError("Theme directory is read-only; clone the theme into a writable theme root and select that clone")
            raise DeploymentError("Target directory is not writable: " + str(parent))

    def put(self, item, source, expected_current, expected_source):
        path = self.path(item)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".esde-workbench-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                with Path(source).open("rb") as stream:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if file_hash(temporary) != expected_source:
                raise DeploymentError("Staged source hash changed during transfer")
            if hasattr(self, "identity_before_commit"):
                self.identity_before_commit([item])
            if self.describe(item)["sha256"] != expected_current:
                raise DeploymentError("Target changed during transfer; prepared file was not installed")
            if item["original"].get("mode") is not None:
                os.chmod(temporary, item["original"]["mode"])
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def remove(self, item, expected_current):
        if self.describe(item)["sha256"] != expected_current:
            raise DeploymentError("Target changed before removal; refusing to overwrite user changes")
        self.path(item).unlink()

    def restore_attributes(self, item):
        original = item["original"]
        path = self.path(item)
        if original.get("mode") is not None:
            os.chmod(path, original["mode"])
        if original.get("mtime_ns") is not None:
            os.utime(path, ns=(original["mtime_ns"], original["mtime_ns"]))


class AndroidTarget:
    def __init__(self, roots, adb="adb", serial=None, package="org.es_de.frontend"):
        self.roots = {scope: remote_root(root) for scope, root in roots.items()}
        self.adb = Adb(adb, serial)
        if not re.fullmatch(r"[A-Za-z0-9_.]+", package):
            raise DeploymentError("ES-DE package must be an Android package identifier")
        self.package = package
        self.adb.require_device()
        for root in self.roots.values():
            self.adb.shell_bytes("test -d " + shlex.quote(root))

    def path(self, item):
        return self.roots[item["scope"]] + "/" + item["relative"]

    def assert_quiescent(self):
        script = "command -v pidof >/dev/null || exit 33; if pidof " + shlex.quote(self.package) + " >/dev/null 2>&1; then printf 'RUNNING'; else printf 'STOPPED'; fi"
        if self.adb.shell_bytes(script).strip() != b"STOPPED":
            raise DeploymentError("Close ES-DE normally before applying or rolling back files, then rerun; its running process may save stale gamelist data")

    def _safe_path_script(self, item):
        root = self.roots[item["scope"]]
        parts = " ".join(shlex.quote(part) for part in item["relative"].split("/"))
        # A shell loop keeps deep Unicode paths from expanding quadratically in
        # every batch command while checking every exact descendant for links.
        return "esde_guard_path=" + shlex.quote(root) + "; for esde_guard_part in " + parts + '; do esde_guard_path="$esde_guard_path/$esde_guard_part"; test ! -L "$esde_guard_path" || exit 31; done'

    def _groups(self, entries, kind):
        group, budget = [], 0
        for entry in entries:
            item = entry if isinstance(entry, dict) else entry[0]
            length = len(shlex.quote(self.path(item)).encode("utf-8"))
            cost = {"hash": 450 + 5 * length, "transfer": 900 + 8 * length, "backup": 150 + length, "remove": 600 + 5 * length}[kind]
            if cost > 96 * 1024:
                raise DeploymentError("Android target path is too long for a bounded safe batch command")
            if group and (len(group) == 50 or budget + cost > 96 * 1024):
                yield group
                group, budget = [], 0
            group.append(entry)
            budget += cost
        if group:
            yield group

    def describe(self, item):
        return self.describe_many([item])[0]

    def describe_many(self, items):
        """One hash request per at most 50 exact targets, including absences."""
        results = []
        for group in self._groups(items, "hash"):
            lines = ["set -eu"]
            for index, item in enumerate(group):
                path = shlex.quote(self.path(item))
                lines.append("(" + self._safe_path_script(item) + ") || exit 31")
                lines.append("if test -f " + path + "; then printf '" + str(index) + " '; sha256sum " + path + " | cut -d ' ' -f 1; elif test -e " + path + "; then exit 32; else printf '" + str(index) + " ABSENT\\n'; fi")
            output = self.adb.shell_bytes("; ".join(lines)).decode("ascii").splitlines()
            parsed = {}
            for line in output:
                fields = line.split()
                if len(fields) != 2 or not fields[0].isdigit():
                    raise DeploymentError("Android batch hash returned invalid output")
                index, digest = int(fields[0]), fields[1]
                if index in parsed or index >= len(group):
                    raise DeploymentError("Android batch hash returned duplicate or out-of-range index")
                if digest != "ABSENT" and not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise DeploymentError("Android batch hash returned an invalid digest")
                parsed[index] = {"exists": digest != "ABSENT", "sha256": None if digest == "ABSENT" else digest}
            if len(parsed) != len(group):
                raise AdbError("Android batch hash incomplete; reconnect and resume")
            results.extend(parsed[index] for index in range(len(group)))
        return results

    def backup(self, item, local):
        data = self.adb.read(self.path(item))
        Path(local).write_bytes(data)

    def backup_many(self, entries):
        """Stream only existing managed originals; never extract arbitrary paths."""
        for scope in dict.fromkeys(item["scope"] for item, _ in entries):
            selected = [(item, local) for item, local in entries if item["scope"] == scope]
            for group in self._groups(selected, "backup"):
                expected = {item["relative"]: (item, Path(local)) for item, local in group}
                script = "tar -cf - -C " + shlex.quote(self.roots[scope]) + " " + " ".join(shlex.quote(relative) for relative in expected)
                command = [self.adb.executable, "-s", self.adb.serial, "exec-out", "sh", "-c", shlex.quote(script)]
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                deadline = start_deadline(process)
                seen = set()
                try:
                    with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
                        for member in archive:
                            name = member.name[2:] if member.name.startswith("./") else member.name
                            if name not in expected or name in seen or not member.isfile():
                                raise DeploymentError("Android backup archive included an unexpected, duplicate or non-file member")
                            item, local = expected[name]
                            temporary = local.with_name(local.name + ".part")
                            local.parent.mkdir(parents=True, exist_ok=True)
                            with archive.extractfile(member) as source, temporary.open("wb") as output:
                                shutil.copyfileobj(source, output, length=1024 * 1024)
                                output.flush()
                                os.fsync(output.fileno())
                            if file_hash(temporary) != item["original"]["sha256"]:
                                temporary.unlink(missing_ok=True)
                                raise DeploymentError("Original changed during Android backup streaming: " + name)
                            if local.exists():
                                if file_hash(local) != item["original"]["sha256"]:
                                    raise DeploymentError("Original backup already exists with another hash")
                                temporary.unlink(missing_ok=True)
                            else:
                                temporary.replace(local)
                            seen.add(name)
                    _, stderr = process.communicate(timeout=600)
                    if process.returncode or seen != set(expected):
                        raise AdbError("Android backup stream incomplete; reconnect and resume: " + stderr.decode("utf-8", errors="replace")[:300])
                except (OSError, tarfile.TarError, subprocess.TimeoutExpired) as error:
                    process.kill()
                    process.communicate()
                    raise AdbError("Android backup stream interrupted; completed immutable backups retained") from error
                except Exception:
                    process.kill()
                    process.communicate()
                    raise
                finally:
                    deadline.cancel()

    def assert_writable(self, item):
        known = getattr(self, "_writable_scopes", set())
        if item["scope"] in known:
            return
        root = self.roots[item["scope"]]
        output = self.adb.shell_bytes("if test -w " + shlex.quote(root) + "; then printf 'WRITABLE'; else printf 'READONLY'; fi")
        if output.strip() != b"WRITABLE":
            if item["scope"] == "theme":
                raise ThemeReadOnlyError("Android theme root is read-only. Clone the theme into a writable user theme directory, then select that clone in ES-DE before planning this theme change")
            raise DeploymentError("Android ES-DE root is not writable by the authorized ADB shell")
        self._writable_scopes = known | {item["scope"]}

    def _current_guard(self, item, expected):
        path = shlex.quote(self.path(item))
        if expected is None:
            return "test ! -e " + path
        return 'test "$(sha256sum ' + path + ' | cut -d " " -f 1)" = ' + shlex.quote(expected)

    def put(self, item, source, expected_current, expected_source):
        path = self.path(item)
        parent = path.rsplit("/", 1)[0]
        temporary_name = ".esde-workbench-" + expected_source[:24] + ".tmp"
        temporary = parent + "/" + temporary_name
        transfer_script = self._safe_path_script(item) + " && mkdir -p " + shlex.quote(parent) + " && test ! -L " + shlex.quote(temporary) + " && tar -xf - -C " + shlex.quote(parent)
        # exec-in transports raw tar bytes to Android. adb shell stdin is a
        # terminal-oriented path and is deliberately never used for tar data.
        command = [self.adb.executable, "-s", self.adb.serial, "exec-in", "sh", "-c", shlex.quote(transfer_script)]
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as error:
            raise AdbError("ADB binary transfer could not start") from error
        deadline = start_deadline(process)
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                info = tarfile.TarInfo(temporary_name)
                info.size = Path(source).stat().st_size
                info.mode = 0o644
                with Path(source).open("rb") as stream:
                    archive.addfile(info, stream)
            process.stdin.close()
            process.stdin = None
            _, stderr = process.communicate(timeout=600)
            if process.returncode:
                raise AdbError("Android binary transfer failed; reconnect and resume: " + stderr.decode("utf-8", errors="replace")[:300])
        except (BrokenPipeError, subprocess.TimeoutExpired, OSError, tarfile.TarError) as error:
            process.kill()
            process.communicate()
            raise AdbError("Android binary transfer interrupted; reconnect and resume") from error
        finally:
            deadline.cancel()
        if hasattr(self, "identity_before_commit"):
            self.identity_before_commit([item])
        script = "set -eu; (" + self._safe_path_script(item) + ") || exit 31; " + self._current_guard(item, expected_current) + '; test "$(sha256sum ' + shlex.quote(temporary) + ' | cut -d " " -f 1)" = ' + shlex.quote(expected_source) + "; mv " + shlex.quote(temporary) + " " + shlex.quote(path)
        self.adb.shell_bytes(script)

    def put_many(self, entries, on_done):
        """Stage a whole tar batch, verify every stage hash, then atomic replaces."""
        for scope in dict.fromkeys(entry[0]["scope"] for entry in entries):
            selected = [entry for entry in entries if entry[0]["scope"] == scope]
            for group in self._groups(selected, "transfer"):
                staged = []
                prep = ["set -eu"]
                for item, source, current, desired in group:
                    parent_rel = item["relative"].rsplit("/", 1)[0]
                    name = ".esde-workbench-" + sha256((scope + ":" + item["relative"]).encode("utf-8"))[:16] + "-" + desired[:16] + ".tmp"
                    relative = parent_rel + "/" + name
                    path = self.roots[scope] + "/" + relative
                    prep += ["(" + self._safe_path_script(item) + ") || exit 31", "mkdir -p " + shlex.quote(self.roots[scope] + "/" + parent_rel), "test ! -L " + shlex.quote(path)]
                    staged.append((item, Path(source), current, desired, relative, path))
                prep.append("tar -xf - -C " + shlex.quote(self.roots[scope]))
                command = [self.adb.executable, "-s", self.adb.serial, "exec-in", "sh", "-c", shlex.quote("; ".join(prep))]
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                deadline = start_deadline(process)
                try:
                    with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                        for _, source, _, _, relative, _ in staged:
                            info = tarfile.TarInfo(relative)
                            info.size, info.mode = source.stat().st_size, 0o644
                            with source.open("rb") as stream:
                                archive.addfile(info, stream)
                    process.stdin.close()
                    process.stdin = None
                    _, stderr = process.communicate(timeout=600)
                    if process.returncode:
                        raise AdbError("Android batch transfer failed; reconnect and resume: " + stderr.decode("utf-8", errors="replace")[:300])
                except (OSError, tarfile.TarError, subprocess.TimeoutExpired) as error:
                    process.kill()
                    process.communicate()
                    raise AdbError("Android batch transfer interrupted; reconnect and resume") from error
                finally:
                    deadline.cancel()
                if hasattr(self, "identity_before_commit"):
                    self.identity_before_commit([entry[0] for entry in staged])
                # No target is replaced until *all* staged files and all target
                # preconditions pass. Same-directory staging keeps mv atomic.
                commit = ["set -eu"]
                for item, _, current, desired, _, path in staged:
                    commit += ["(" + self._safe_path_script(item) + ") || exit 31", self._current_guard(item, current), 'test "$(sha256sum ' + shlex.quote(path) + ' | cut -d " " -f 1)" = ' + shlex.quote(desired)]
                for index, (item, _, current, desired, _, path) in enumerate(staged):
                    commit += [self._current_guard(item, current), "mv " + shlex.quote(path) + " " + shlex.quote(self.path(item)), self._current_guard(item, desired), "printf 'DONE " + str(index) + "\\n'"]
                self._commit_events("; ".join(commit), [entry[0] for entry in staged], on_done)

    def _commit_events(self, script, items, on_done):
        command = [self.adb.executable, "-s", self.adb.serial, "exec-out", "sh", "-c", shlex.quote(script)]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = start_deadline(process)
        seen = set()
        try:
            for line in process.stdout:
                fields = line.decode("ascii").strip().split()
                if len(fields) != 2 or fields[0] != "DONE" or not fields[1].isdigit():
                    raise DeploymentError("Android commit progress returned invalid output")
                index = int(fields[1])
                if index in seen or index >= len(items):
                    raise DeploymentError("Android commit progress index is invalid")
                seen.add(index)
                on_done(items[index])
            _, stderr = process.communicate(timeout=600)
            if process.returncode or len(seen) != len(items):
                raise AdbError("Android atomic commit interrupted; original/desired hashes allow safe resume: " + stderr.decode("utf-8", errors="replace")[:300])
        except Exception:
            process.kill()
            process.communicate()
            raise
        finally:
            deadline.cancel()

    def remove(self, item, expected_current):
        self.adb.shell_bytes("set -eu; (" + self._safe_path_script(item) + ") || exit 31; " + self._current_guard(item, expected_current) + "; rm -f " + shlex.quote(self.path(item)))

    def remove_many(self, entries, on_done):
        for group in self._groups(entries, "remove"):
            lines = ["set -eu"]
            for item, current in group:
                lines += ["(" + self._safe_path_script(item) + ") || exit 31", self._current_guard(item, current)]
            for index, (item, current) in enumerate(group):
                lines += [self._current_guard(item, current), "rm -f " + shlex.quote(self.path(item)), "test ! -e " + shlex.quote(self.path(item)), "printf 'DONE " + str(index) + "\\n'"]
            self._commit_events("; ".join(lines), [item for item, _ in group], on_done)

    def restore_attributes(self, item):
        # Shared Android storage permissions are controlled by the OS; byte
        # restoration is checked but arbitrary ownership changes are excluded.
        pass


def create_target(target, adb_override=None):
    if target["type"] == "local":
        return LocalTarget(target["roots"])
    if target["type"] == "android":
        from adb_runtime import resolve_adb, AdbResolutionError
        try:
            executable = resolve_adb(adb_override or target.get("adb"))
        except AdbResolutionError as error:
            raise DeploymentError(str(error)) from error
        result = AndroidTarget(target["roots"], executable, target["serial"], target.get("esde_package", "org.es_de.frontend"))
        if target.get("canonical_roots"):
            for root in result.roots.values():
                actual = result.adb.shell_bytes("test -d " + shlex.quote(root) + " && realpath " + shlex.quote(root)).decode("utf-8").strip()
                if actual != root:
                    raise DeploymentError("A sealed Android root changed its physical location; no writes are permitted")
        return result
    raise DeploymentError("Unsupported target type in sealed plan")


def save_state(run, state):
    state["updated_at"] = now()
    write_json(Path(run) / "deployment_state.json", state)


def seal_plan(run, plan):
    path = Path(run) / "deployment_plan.json"
    if path.exists() or (Path(run) / "deployment_plan.sha256").exists():
        raise DeploymentError("Run already has a deployment plan; use a separate run for a new baseline")
    data = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(data)
    (Path(run) / "deployment_plan.sha256").write_text(sha256(data) + "\n", encoding="ascii")
    write_json(Path(run) / "deployment_plan.signature.json", identity_api().seal_payload({"sha256": sha256(data)}, kind="deployment_plan", key_path=plan.get("identity_gate", {}).get("key_path")))


def load_plan(run, key_path=None):
    run = Path(run).resolve()
    data = (run / "deployment_plan.json").read_bytes()
    expected = (run / "deployment_plan.sha256").read_text(encoding="ascii").strip()
    if sha256(data) != expected:
        raise DeploymentError("Sealed deployment plan changed; refusing to use a mutable baseline")
    plan = json.loads(data.decode("utf-8"))
    if plan.get("schema_version") != 1:
        raise DeploymentError("Unsupported deployment plan version")
    try:
        bound = identity_api().verify_payload(load_json(run / "deployment_plan.signature.json"), kind="deployment_plan", key_path=key_path)
    except (ValueError, OSError) as error:
        raise DeploymentError("Deployment plan signature is missing or invalid; replan before applying") from error
    if bound != {"sha256": sha256(data)}:
        raise DeploymentError("Deployment plan does not match its private-key signature")
    if plan.get("identity_gate", {}).get("key_path") != (str(Path(key_path).resolve()) if key_path else None):
        raise DeploymentError("Use the same explicitly configured identity key as planning; the plan cannot choose a trust key")
    seen = set()
    for item in plan["files"]:
        validate_relative(item["relative"], item["scope"])
        key = (item["scope"], item["relative"])
        if key in seen:
            raise DeploymentError("Deployment plan contains duplicate targets")
        seen.add(key)
        if item["scope"] not in plan["target"]["roots"]:
            raise DeploymentError("Plan scope has no explicit target root")
        if item["original"]["exists"]:
            backup = backup_path(run, item["backup"])
            if not backup.is_file() or file_hash(backup) != item["original"]["sha256"]:
                raise DeploymentError("Immutable original backup is missing or changed: " + item["relative"])
    return plan


def failure(reporter, error, state=None):
    status = "blocked" if isinstance(error, (AdbError, DeploymentError)) else "error"
    count = state.get("counts", {}).get(reporter.phase.removeprefix("deployment_")) if state else None
    reporter.update(status, str(error), completed=count, total=state.get("planned_files") if state else None, connection={"status": "disconnected_or_unavailable"} if isinstance(error, AdbError) else None)
    if state is not None:
        state["status"] = status
        state["last_error"] = str(error)
        save_state(reporter.out, state)
    return 2


def plan_command(args):
    run = Path(args.out).resolve()
    reporter = Reporter(run, "deployment_plan", args)
    try:
        if (run / "deployment_plan.json").exists() or (run / "deployment_plan.sha256").exists():
            raise DeploymentError("Run already has a deployment plan; its baseline is immutable")
        manifest_path = Path(args.manifest).resolve()
        manifest = load_json(manifest_path)
        source_files = manifest.get("files") if isinstance(manifest, dict) else manifest
        if not isinstance(source_files, list) or not source_files:
            raise DeploymentError("Source manifest requires a nonempty files list")
        roots = {"esde": str(Path(args.esde_root).resolve()) if args.target == "local" else remote_root(args.esde_root)}
        if args.theme_root:
            roots["theme"] = str(Path(args.theme_root).resolve()) if args.target == "local" else remote_root(args.theme_root)
        target_config = {"type": args.target, "roots": roots}
        if args.rom_root:
            target_config["rom_root"] = str(Path(args.rom_root).resolve()) if args.target == "local" else remote_root(args.rom_root)
        if args.target == "android":
            if not args.serial:
                raise DeploymentError("Android plans require explicit --serial")
            target_config.update({"serial": args.serial, "adb": args.adb, "esde_package": args.esde_package})
        target = create_target(target_config)
        if args.target == "android" and hasattr(target, "adb"):
            target_config["adb"] = target.adb.executable
            for scope, literal in list(target_config["roots"].items()):
                resolved = target.adb.shell_bytes("test -d " + shlex.quote(literal) + " && realpath " + shlex.quote(literal)).decode("utf-8").strip()
                target_config["roots"][scope] = remote_root(resolved)
                target.roots[scope] = target_config["roots"][scope]
            target_config["canonical_roots"] = True
            if target_config.get("rom_root"):
                # Android exposes /sdcard as a standard alias. Resolve only the
                # caller's explicit root once and freeze its actual boundary.
                literal = target_config["rom_root"]
                resolved = target.adb.shell_bytes("test -d " + shlex.quote(literal) + " && realpath " + shlex.quote(literal)).decode("utf-8").strip()
                target_config["rom_root"] = remote_root(resolved)
        if args.target == "android":
            reporter.update("running", "Authorized Android target selected", connection={"status": "connected", "serial": args.serial})
        files, seen, destinations = [], set(), set()
        reporter.update("running", "Collecting original backup baselines", completed=0, total=len(source_files))
        for index, source_item in enumerate(source_files):
            if not isinstance(source_item, dict) or not isinstance(source_item.get("local"), str) or not source_item["local"]:
                raise DeploymentError("Each manifest entry requires explicit local and relative strings")
            scope = source_item.get("scope", "esde")
            relative = validate_relative(source_item.get("relative"), scope)
            if scope not in roots:
                raise DeploymentError("Theme entries require an explicit --theme-root")
            key = (scope, relative)
            if key in seen:
                raise DeploymentError("Two manifest entries share a target path: " + relative)
            seen.add(key)
            source = Path(source_item.get("local", ""))
            source = source if source.is_absolute() else manifest_path.parent / source
            source = source.resolve()
            if not source.is_file() or source.stat().st_size == 0:
                raise DeploymentError("Source must be a nonzero regular file: " + str(source))
            if relative.startswith("gamelists/") and scope == "esde":
                parse_document(source.read_bytes())
            if scope == "theme" and source.suffix.casefold() == ".xml" and re.search(rb"<\s*gameList(?:\s|>)", source.read_bytes(), re.I):
                raise DeploymentError("ES-DE gameList content cannot be written through independent theme scope")
            item = {"scope": scope, "relative": relative, "local": str(source), "desired_sha256": file_hash(source), "size": source.stat().st_size}
            guard_theme_destination(target, target_config, item)
            destination = str(target.path(item))
            destination = os.path.normcase(destination) if args.target == "local" else destination
            if destination in destinations:
                raise DeploymentError("Manifest scopes overlap at the same physical destination: " + relative)
            destinations.add(destination)
            files.append(item)
        originals = describe_all(target, files)
        pending_backups = []
        for item, original in zip(files, originals):
            scope, relative = item["scope"], item["relative"]
            item["original"] = original
            item["backup"] = "deployment-backups/" + scope + "/" + relative if original["exists"] else None
            if original["exists"]:
                backup = backup_path(run, item["backup"])
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    # Interrupted planning can resume only with the same saved
                    # original; never overwrite a previous backup baseline.
                    if file_hash(backup) != original["sha256"]:
                        raise DeploymentError("Existing interrupted-plan backup differs from target; use a new run")
                else:
                    pending_backups.append((item, backup))
        for group in batches(pending_backups):
            if hasattr(target, "backup_many"):
                target.backup_many(group)
            else:
                for item, backup in group:
                    target.backup(item, backup)
            reporter.event("backup_batch", "Original backup batch collected", {"files": len(group)})
        after_backup = describe_all(target, files)
        for index, (item, current) in enumerate(zip(files, after_backup), 1):
            original = item["original"]
            if current["sha256"] != original["sha256"] or original["exists"] and file_hash(run / item["backup"]) != original["sha256"]:
                raise DeploymentError("Target changed while its original backup was being collected")
            if index % 50 == 0 or index == len(files):
                reporter.update("running", "Sealed original baselines: " + str(index) + "/" + str(len(files)), completed=index, total=len(files))
        plan = {"schema_version": 1, "created_at": now(), "source_manifest": str(manifest_path), "target": target_config, "files": files}
        catalog = args.identity_catalog or (manifest.get("identity_catalog") if isinstance(manifest, dict) else None)
        if catalog and not Path(catalog).is_absolute():
            catalog = str((manifest_path.parent / catalog).resolve())
        key = args.identity_key
        configured_key = Path(key) if key else identity_api().default_key_path()
        if configured_key.resolve().is_relative_to(run):
            raise DeploymentError("Identity key must be outside the mutable run directory")
        plan["identity_gate"] = build_identity_gate(run, plan, target, catalog, key)
        # A structure-only plan also needs an immutable authorization boundary:
        # otherwise editing both JSON and its neighboring SHA could remove gates.
        plan["identity_gate"]["key_path"] = str(Path(key).resolve()) if key else None
        seal_plan(run, plan)
        save_state(run, {"schema_version": 1, "status": "planned", "counts": {"apply": 0, "rollback": 0, "verify": 0}, "planned_files": len(files), "checkpoint_store": "deployment_journal.sqlite"})
        reporter.update("running", "Deployment plan prepared; original backups and target hashes sealed", completed=len(files), total=len(files), phase_status="done")
        return 0
    except (OSError, ValueError, ET.ParseError, DeploymentError, AdbError, tarfile.TarError) as error:
        return failure(reporter, error)


def action_command(args):
    run = Path(args.run).resolve()
    reporter = Reporter(run, "deployment_" + args.command, args)
    state = None
    try:
        write_json(run / "deployment_verification.json", {"schema_version": 1, "checked_at": now(), "status": "pending", "checked": 0, "files": [], "visual_qa": "pending", "media_decode_qa": "pending"})
        plan = load_plan(run, args.identity_key)
        state = load_json(run / "deployment_state.json")
        state.setdefault("counts", {})[args.command] = 0
        state["active_action"] = args.command
        save_state(run, state)
        target = create_target(plan["target"], args.adb)
        if args.command in ("apply", "rollback") and hasattr(target, "assert_quiescent"):
            target.assert_quiescent()
        if plan["target"]["type"] == "android":
            reporter.update("running", "Authorized sealed Android target selected", connection={"status": "connected", "serial": plan["target"]["serial"]})
        files = plan["files"]
        identity_gate = recheck_identity_gate(run, plan, target) if args.command != "rollback" else None
        if args.command == "apply":
            install_identity_commit_guard(plan, target)
        reporter.update("running", "Checking sealed backups and current target hashes", completed=0, total=len(files))
        # Validate every item before mutating any. A later user's changed file
        # blocks both installation and rollback instead of being overwritten.
        descriptions = describe_all(target, files)
        for item, description in zip(files, descriptions):
            original, desired = item["original"]["sha256"], item["desired_sha256"]
            current = description["sha256"]
            if current not in (original, desired):
                raise DeploymentError("Target changed after planning; refusing to overwrite: " + item["relative"])
            if args.command == "apply":
                if not Path(item["local"]).is_file() or file_hash(item["local"]) != desired:
                    raise DeploymentError("Prepared source changed after planning: " + item["relative"])
                if current != desired:
                    target.assert_writable(item)
            elif args.command == "rollback" and current != original:
                target.assert_writable(item)
        results, completed = [], 0
        journal = sqlite3.connect(run / "deployment_journal.sqlite", timeout=10)
        journal.execute("PRAGMA journal_mode=WAL")
        journal.execute("CREATE TABLE IF NOT EXISTS checkpoints(scope TEXT,relative TEXT,action TEXT,sha256 TEXT,checked_at TEXT,PRIMARY KEY(scope,relative,action))")
        journal.commit()
        unset = object()
        def done(item, digest=unset):
            nonlocal completed
            if digest is unset:
                digest = item["original"]["sha256"] if args.command == "rollback" else item["desired_sha256"]
            with journal:
                journal.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?) ON CONFLICT(scope,relative,action) DO UPDATE SET sha256=excluded.sha256,checked_at=excluded.checked_at", (item["scope"], item["relative"], args.command, digest, now()))
            completed += 1
            state["status"] = "applying" if args.command == "apply" else "rolling_back" if args.command == "rollback" else "verifying"
            state.setdefault("counts", {})[args.command] = completed
            state["last_checked"] = {"scope": item["scope"], "relative": item["relative"], "sha256": digest}
            save_state(run, state)
            if completed % 50 == 0 or completed == len(files):
                reporter.update("running", args.command.capitalize() + " checked " + str(completed) + "/" + str(len(files)) + ": " + item["relative"], completed=completed, total=len(files))
        try:
            if args.command == "verify":
                for item, description in zip(files, descriptions):
                    current = description["sha256"]
                    matches = current == item["desired_sha256"]
                    results.append({"scope": item["scope"], "relative": item["relative"], "actual_sha256": current, "desired_sha256": item["desired_sha256"], "matches": matches})
                    done(item, current)
            elif hasattr(target, "put_many"):
                transfers, removals = [], []
                for item, description in zip(files, descriptions):
                    current = description["sha256"]
                    desired = item["desired_sha256"] if args.command == "apply" else item["original"]["sha256"]
                    if current == desired:
                        done(item)
                    elif args.command == "rollback" and not item["original"]["exists"]:
                        removals.append((item, current))
                    else:
                        source = item["local"] if args.command == "apply" else run / item["backup"]
                        transfers.append((item, source, current, desired))
                target.put_many(transfers, done)
                if removals:
                    target.remove_many(removals, done)
                checked = describe_all(target, files)
                for item, description in zip(files, checked):
                    expected = item["desired_sha256"] if args.command == "apply" else item["original"]["sha256"]
                    if description["sha256"] != expected:
                        raise DeploymentError("Post-action target hash mismatch: " + item["relative"])
            else:
                for item, description in zip(files, descriptions):
                    current = target.describe(item)["sha256"]
                    if args.command == "apply":
                        if current != item["desired_sha256"]:
                            target.put(item, item["local"], current, item["desired_sha256"])
                        if target.describe(item)["sha256"] != item["desired_sha256"]:
                            raise DeploymentError("Installed hash differs from prepared source: " + item["relative"])
                    else:
                        if current != item["original"]["sha256"]:
                            if item["original"]["exists"]:
                                target.put(item, run / item["backup"], current, item["original"]["sha256"])
                                target.restore_attributes(item)
                            else:
                                target.remove(item, current)
                        if target.describe(item)["sha256"] != item["original"]["sha256"]:
                            raise DeploymentError("Rollback hash differs from original baseline: " + item["relative"])
                    done(item)
        finally:
            journal.close()
        if args.command == "verify":
            matched = all(result["matches"] for result in results)
            verification = {"schema_version": 1, "checked_at": now(), "status": "pass" if matched else "needs_work", "checked": len(results), "files": results, "visual_qa": "pending", "media_decode_qa": "pending", "identity_gate": identity_gate, "plan_sha256": file_hash(run / "deployment_plan.json")}
            if matched:
                verification["identity_verification"] = identity_api().seal_payload({"plan_sha256": verification["plan_sha256"], "identity_gate": identity_gate, "files": results}, kind="deployment_verification", key_path=plan["identity_gate"].get("key_path"))
            write_json(run / "deployment_verification.json", verification)
            if not matched:
                raise DeploymentError("One or more planned files are not installed; apply or resume before verification")
            state["status"] = "awaiting_visual_qa"
        elif args.command == "apply":
            state["status"] = "installed_pending_verification"
        else:
            state["status"] = "rolled_back"
        state.pop("last_error", None)
        save_state(run, state)
        reporter.update("running", "Managed files rolled back to their original baselines" if args.command == "rollback" else "Deployment hashes verified; awaiting media and visual QA" if args.command == "verify" else "Managed files installed; run deployment verification next", completed=len(files), total=len(files), phase_status="done")
        if args.command == "verify" and reporter.store:
            reporter.store.emit_update(run, phase="awaiting_visual_qa", status="running", phase_status="pending", message="Deployment hashes passed; media decoding and visual identity QA remain")
        return 0
    except (OSError, ValueError, ET.ParseError, DeploymentError, AdbError, tarfile.TarError) as error:
        return failure(reporter, error, state)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Create immutable original backups and exact target hash baselines")
    plan.add_argument("--manifest", required=True)
    plan.add_argument("--target", choices=("local", "android"), required=True)
    plan.add_argument("--esde-root", required=True)
    plan.add_argument("--rom-root", help="Explicit ROM library root; changed game metadata/media requires freshly read ROM fingerprints")
    plan.add_argument("--identity-catalog", help="Signed provider/ROM identity receipts authorizing the changed facts and exact media bytes")
    plan.add_argument("--identity-key", help="Optional private test/configuration key outside the mutable run; never read from the catalog")
    plan.add_argument("--theme-root", help="Optional explicit writable theme root, used only by scope=theme entries")
    plan.add_argument("--adb", help="Explicit ADB executable; otherwise resolve configured/PATH/Android SDK tools and validate adb version")
    plan.add_argument("--serial", help="Explicit Android serial; authorization is checked")
    plan.add_argument("--esde-package", default="org.es_de.frontend", help="Package whose running ES-DE process blocks Android writes")
    plan.add_argument("--out", required=True, help="Durable workbench run directory")
    for name in ("apply", "verify", "rollback"):
        action = commands.add_parser(name)
        action.add_argument("--run", required=True)
        action.add_argument("--adb", help="Override ADB executable; sealed device serial and target roots stay fixed")
        action.add_argument("--identity-key", help="Same explicitly configured private key used for planning; omitted uses the private user configuration")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return plan_command(args) if args.command == "plan" else action_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
