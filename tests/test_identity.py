"""Identity receipts are earned from measured bytes and sealed source evidence."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import identity


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / "private" / "key.json"
        self.rom = self.root / "fixture.nds"
        # Synthetic fixture bytes, no commercial game or actual user library.
        self.rom.write_bytes(b"fixture game Mario, Japanese revision 2, Disc 1")
        self.fp = identity.fingerprint_file(self.rom)
        self.system, self.file = "nds", "folder/fixture.nds"
        self.metadata = {"name": "马里奥测试", "desc": "合成的测试资料", "publisher": "测试发行商", "players": "1"}
        self.url = "https://api.screenscraper.fr/api2/mediaJeu.php?id=42&type=ss"
        self.record = {"id": "77", "romfilename": "fixture (Japan) (Rev 2) (Disc 1).nds", "romsize": str(self.fp["size"]), "rommd5": self.fp["md5"], "romsha1": self.fp["sha1"], "romcrc": self.fp["crc"].upper(), "region": "jp", "revision": "2", "disc": "1"}
        self.candidate = {
            "provider": "screenscraper", "provider_game_id": "42", "system": {"id": "15"},
            "name": self.metadata["name"], "description_zh": self.metadata["desc"],
            "publisher": self.metadata["publisher"], "players": "1", "rom_evidence": self.record,
            "media_candidates": [{"type": "ss", "url": self.url}],
            "identity_confirmed": False, "review_required": True,
        }
        self.report = {"provider": "screenscraper", "endpoint": "jeuInfos.php", "query": {"systemeid": "15", "romtaille": self.fp["size"], "md5": self.fp["md5"], "sha1": self.fp["sha1"], "crc": self.fp["crc"].upper()}, "candidates": [self.candidate], "identity_confirmed": False}
        self.source = identity.seal_provider_report(self.report, key_path=self.key)

    def authorize(self, source=None, **kwargs):
        arguments = {"system": self.system, "file": self.file, "rom_path": self.rom, "metadata": self.metadata, "key_path": self.key}
        arguments.update(kwargs)
        return identity.authorize_patch(self.source if source is None else source, **arguments)

    def verify(self, receipt, **kwargs):
        arguments = {"system": self.system, "file": self.file, "rom_fingerprint": self.fp, "metadata": self.metadata, "key_path": self.key}
        arguments.update(kwargs)
        return identity.verify_receipt(receipt, **arguments)

    def seal(self, report):
        return identity.seal_provider_report(report, key_path=self.key)

    def media(self):
        target = self.root / "screenshot.png"
        target.write_bytes(b"synthetic image bytes; identity checks provenance, media QA checks decoding")
        measured = identity.fingerprint_file(target)
        provenance = {"provider": "screenscraper", "source_url": self.url, "bytes": measured["size"], "sha256": measured["sha256"], "status": "downloaded_unverified"}
        return {"type": "screenshots", "relative": "nds/screenshots/folder/fixture.png", "path": str(target), "source_url": self.url, "download_receipt": identity.seal_download(provenance, key_path=self.key)}

    def test_exact_rom_source_is_approved_and_minimal_live_sha256_is_sufficient(self):
        receipt = self.authorize()
        payload = self.verify(receipt, rom_fingerprint={"sha256": self.fp["sha256"], "size": self.fp["size"]})
        self.assertEqual(payload["method"], "exact_returned_rom_hash_platform_size")
        self.assertEqual(payload["metadata"], self.metadata)
        self.assertEqual(payload["matched_rom"]["disc"], "1")

    def test_bare_boolean_and_nonempty_explanation_cannot_authorize(self):
        for report in ({"identity_confirmed": True, "reason": "checked"}, self.report, {"signature": "a" * 64, "identity_confirmed": True}):
            with self.subTest(report=list(report)), self.assertRaises(identity.IdentityError):
                self.authorize(report)

    def test_mario_rom_with_zelda_metadata_is_rejected(self):
        with self.assertRaises(identity.IdentityError) as error:
            self.authorize(metadata={**self.metadata, "name": "塞尔达测试", "desc": "另一个作品的资料"})
        self.assertEqual(error.exception.code, "metadata_mismatch")
        self.verify(self.authorize())

    def test_unsupported_platform_wrong_query_platform_and_returned_platform_are_rejected(self):
        with self.assertRaises(identity.IdentityError):
            self.authorize(system="unknown")
        with self.assertRaises(identity.IdentityError):
            self.authorize(system="gba", file="folder/fixture.gba")
        for where in ("query", "candidate"):
            report = copy.deepcopy(self.report)
            if where == "query":
                report["query"]["systemeid"] = "12"
            else:
                report["candidates"][0]["system"]["id"] = "12"
            with self.subTest(where=where), self.assertRaises(identity.IdentityError) as error:
                self.authorize(self.seal(report))
            self.assertEqual(error.exception.code, "platform_mismatch")

    def test_filename_search_and_crc_only_are_not_automatic_confirmation(self):
        searched = copy.deepcopy(self.report)
        searched["endpoint"] = "jeuRecherche.php"
        with self.assertRaises(identity.IdentityError) as error:
            self.authorize(self.seal(searched))
        self.assertEqual(error.exception.code, "non_exact_lookup")
        crc_only = copy.deepcopy(self.report)
        crc_only["query"].pop("md5")
        crc_only["query"].pop("sha1")
        with self.assertRaises(identity.IdentityError) as error:
            self.authorize(self.seal(crc_only))
        self.assertEqual(error.exception.code, "weak_fingerprint")

    def test_request_hash_alone_is_not_returned_rom_evidence(self):
        report = copy.deepcopy(self.report)
        report["candidates"][0]["rom_evidence"] = {}
        with self.assertRaises(identity.IdentityError):
            self.authorize(self.seal(report))
        report["candidates"][0]["rom_evidence"] = {"romcrc": self.fp["crc"], "romsize": self.fp["size"]}
        with self.assertRaises(identity.IdentityError):
            self.authorize(self.seal(report))

    def test_same_title_multiple_candidates_and_multiple_rom_variants_are_rejected(self):
        report = copy.deepcopy(self.report)
        report["candidates"].append(copy.deepcopy(self.candidate))
        with self.assertRaises(identity.IdentityError) as error:
            self.authorize(self.seal(report))
        self.assertEqual(error.exception.code, "ambiguous_candidate")
        for variant in ("region", "revision", "disc"):
            report = copy.deepcopy(self.report)
            alternate = {**self.record, variant: "different"}
            report["candidates"][0]["rom_evidence"] = [self.record, alternate]
            with self.subTest(variant=variant), self.assertRaises(identity.IdentityError):
                self.authorize(self.seal(report))

    def test_region_revision_disc_hash_differences_do_not_match(self):
        for field in ("rommd5", "romsha1", "romcrc", "romsize"):
            report = copy.deepcopy(self.report)
            report["candidates"][0]["rom_evidence"][field] = str(self.fp["size"] + 1) if field == "romsize" else "0" * len(self.record[field])
            with self.subTest(field=field), self.assertRaises(identity.IdentityError):
                self.authorize(self.seal(report))

    def test_conflicting_source_hash_aliases_reject_instead_of_picking_one(self):
        report = copy.deepcopy(self.report)
        report["candidates"][0]["rom_evidence"]["md5"] = "0" * 32
        with self.assertRaises(identity.IdentityError) as error:
            self.authorize(self.seal(report))
        self.assertEqual(error.exception.code, "ambiguous_rom")

    def test_unknown_facts_stay_blank_and_cannot_be_invented(self):
        self.authorize(metadata={**self.metadata, "developer": None, "releasedate": ""})
        for field, value in (("developer", "猜测公司"), ("releasedate", "20000101T000000"), ("genre", "猜测类型")):
            with self.subTest(field=field), self.assertRaises(identity.IdentityError):
                self.authorize(metadata={**self.metadata, field: value})
        with self.assertRaises(identity.IdentityError):
            self.authorize(metadata={"name": None})
        with self.assertRaises(identity.IdentityError):
            self.authorize(metadata={**self.metadata, "favorite": "true"})

    def test_translation_not_present_in_source_needs_review(self):
        with self.assertRaises(identity.IdentityError):
            self.authorize(metadata={**self.metadata, "publisher": "English company translation invented by AI"})

    def test_complete_file_path_platform_and_payload_must_all_match(self):
        receipt = self.authorize()
        for arguments in ({"file": "other/fixture.nds"}, {"file": "folder/fixture (Disc 2).nds"}, {"system": "gba"}, {"metadata": {"name": self.metadata["name"]}}, {"metadata": {**self.metadata, "players": "2"}}):
            with self.subTest(arguments=arguments), self.assertRaises(identity.IdentityError):
                self.verify(receipt, **arguments)

    def test_same_size_rom_replacement_invalidates_old_receipt(self):
        receipt = self.authorize()
        changed = bytearray(self.rom.read_bytes())
        changed[-1] ^= 1
        self.rom.write_bytes(changed)
        live = identity.fingerprint_file(self.rom)
        self.assertEqual(live["size"], self.fp["size"])
        with self.assertRaises(identity.IdentityError) as error:
            self.verify(receipt, rom_fingerprint=live)
        self.assertEqual(error.exception.code, "rom_changed")
        with self.assertRaises(identity.IdentityError):
            self.authorize()

    def test_tampering_source_approval_or_signature_is_rejected(self):
        source = copy.deepcopy(self.source)
        source["payload"]["candidates"][0]["name"] = "塞尔达测试"
        with self.assertRaises(identity.IdentityError) as error:
            self.authorize(source, metadata={"name": "塞尔达测试"})
        self.assertEqual(error.exception.code, "identity_evidence_tampered")
        receipt = self.authorize()
        for field, value in (("file", "another.nds"), ("metadata", {"name": "塞尔达测试"}), ("rom_fingerprint", {**self.fp, "sha256": "a" * 64})):
            altered = copy.deepcopy(receipt)
            altered["payload"][field] = value
            with self.subTest(field=field), self.assertRaises(identity.IdentityError):
                self.verify(altered)

    def test_wrong_installation_or_missing_key_cannot_reuse_receipts(self):
        receipt = self.authorize()
        other = self.root / "other-key.json"
        with self.assertRaises(identity.IdentityError) as error:
            self.verify(receipt, key_path=other)
        self.assertEqual(error.exception.code, "missing_identity_key")
        self.assertFalse(other.exists())
        identity.seal_payload({"fixture": "only"}, "fixture", key_path=other)
        with self.assertRaises(identity.IdentityError):
            self.verify(receipt, key_path=other)

    def test_receipt_seal_detaches_mutable_inputs(self):
        receipt = self.authorize()
        self.source["payload"]["candidates"][0]["name"] = "changed later"
        payload = self.verify(receipt)
        payload["metadata"]["name"] = "return value also detached"
        self.verify(receipt)

    def test_real_download_bytes_kind_full_stem_and_source_are_bound(self):
        item = self.media()
        receipt = self.authorize(media=[item])
        self.verify(receipt, media=[item])
        canonical = receipt["payload"]["media"]
        self.verify(receipt, media=canonical)
        for arguments in ({"media": []}, {"media": [{**item, "relative": "nds/screenshots/other/fixture.png"}]}, {"media": [{**item, "type": "covers", "relative": "nds/covers/folder/fixture.png"}]}, {"media": [{**item, "source_url": self.url + "-other"}]}):
            with self.subTest(arguments=str(arguments)[:80]), self.assertRaises(identity.IdentityError):
                self.verify(receipt, **arguments)

    def test_arbitrary_url_and_copied_cover_cannot_be_blessed_with_a_boolean(self):
        item = self.media()
        for fields in ({"download_receipt": None, "identity_confirmed": True, "evidence": "same title"}, {"source_url": self.url + "-other"}, {"type": "covers", "relative": "nds/covers/folder/fixture.png"}):
            with self.subTest(fields=list(fields)), self.assertRaises(identity.IdentityError):
                self.authorize(media=[{**item, **fields}])
        Path(item["path"]).write_bytes(b"different game's picture")
        with self.assertRaises(identity.IdentityError):
            self.authorize(media=[item])

    def test_changed_download_or_media_receipt_is_rejected_at_reverification(self):
        item = self.media()
        receipt = self.authorize(media=[item])
        Path(item["path"]).write_bytes(b"altered after authorization")
        with self.assertRaises(identity.IdentityError):
            self.verify(receipt, media=[item])
        changed = copy.deepcopy(item)
        changed["download_receipt"]["payload"]["sha256"] = "0" * 64
        with self.assertRaises(identity.IdentityError):
            self.authorize(media=[changed])

    def test_derived_media_without_observed_download_lineage_is_rejected(self):
        item = self.media()
        Path(item["path"]).write_bytes(b"newly transcoded/generated image")
        with self.assertRaises(identity.IdentityError):
            self.authorize(media=[item])

    def test_no_path_traversal_root_alias_or_compound_launcher_identity(self):
        for file in ("../fixture.nds", "folder/../fixture.nds", "/fixture.nds", "C:/fixture.nds", "./fixture.nds", "folder\\fixture.nds", "folder//fixture.nds", "folder/fixture\n.nds", "folder/fixture.m3u", "folder/fixture.cue"):
            with self.subTest(file=file), self.assertRaises(identity.IdentityError):
                self.authorize(file=file)

    def test_source_booleans_are_ignored_and_no_freeform_approval_is_needed(self):
        for confirmed in (True, False):
            report = copy.deepcopy(self.report)
            report["identity_confirmed"] = confirmed
            report["candidates"][0]["identity_confirmed"] = confirmed
            self.verify(self.authorize(self.seal(report)))

    def test_empty_rom_and_missing_measurements_cannot_be_authorized(self):
        self.rom.write_bytes(b"")
        with self.assertRaises(identity.IdentityError):
            self.authorize()
        for fp in ({"identity_confirmed": True}, {"size": self.fp["size"], "sha256": self.fp["sha256"]}, {**self.fp, "size": True}):
            with self.subTest(fp=list(fp)), self.assertRaises(identity.IdentityError):
                self.authorize(rom_path=None, rom_fingerprint=fp)

    def test_preparation_plan_and_finish_proofs_cannot_be_cross_used_or_mutated(self):
        payload = {"sha256": "a" * 64, "entries": [{"system": self.system, "file": self.file}]}
        preparation = identity.seal_preparation(payload, key_path=self.key)
        self.assertEqual(identity.verify_preparation(preparation, key_path=self.key), payload)
        for kind in ("deployment_plan", "deployment_verification", "provider_report", "game_identity"):
            with self.subTest(kind=kind), self.assertRaises(identity.IdentityError):
                identity.verify_payload(preparation, kind, key_path=self.key)
        preparation["payload"]["sha256"] = "b" * 64
        with self.assertRaises(identity.IdentityError):
            identity.verify_preparation(preparation, key_path=self.key)

    def test_key_is_reused_without_ever_being_embedded_in_receipt(self):
        before = self.key.read_bytes()
        receipt = self.authorize()
        self.authorize()
        self.assertEqual(self.key.read_bytes(), before)
        self.assertNotIn(str(self.key), json.dumps(receipt))
        stored = json.loads(before)
        self.assertNotIn(stored["blob"], json.dumps(receipt))
        if os.name != "nt":
            self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)

    def test_environment_key_override_only_affects_trusted_runtime_not_catalog(self):
        with patch.dict(os.environ, {identity.KEY_ENV: str(self.key)}):
            receipt = identity.authorize_patch(self.source, system=self.system, file=self.file, rom_path=self.rom, metadata=self.metadata)
            identity.verify_receipt(receipt, system=self.system, file=self.file, rom_fingerprint=self.fp, metadata=self.metadata)
        # Extra envelope fields claiming a key path are rejected, not followed.
        receipt["key_path"] = str(self.key)
        with self.assertRaises(identity.IdentityError):
            self.verify(receipt)

    def test_fingerprint_stream_measures_all_actual_bytes_without_network(self):
        self.assertEqual(identity.fingerprint_stream(io.BytesIO(self.rom.read_bytes())), self.fp)
        with self.assertRaises(identity.IdentityError):
            identity.fingerprint_stream(io.StringIO("not binary"))

    def cli_fixture(self, patch_value=None, source=None):
        source_path = self.root / "query.json"
        patch_path = self.root / "patch.json"
        source_path.write_text(json.dumps({"identity_source": self.source if source is None else source}), encoding="utf-8")
        patch_path.write_text(json.dumps({"games": [{"file": self.file, "metadata": self.metadata}]} if patch_value is None else patch_value), encoding="utf-8")
        return ["authorize", "--source", str(source_path), "--system", self.system, "--file", self.file,
                "--rom", str(self.rom), "--patch", str(patch_path), "--identity-key", str(self.key),
                "--out", str(self.root / "catalog.json")]

    def test_local_authorize_cli_has_a_complete_usable_signed_path(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(identity.main(self.cli_fixture()), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "approved")
        catalog = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(set(catalog), {"schema_version", "entries"})
        self.assertEqual(len(catalog["entries"]), 1)
        entry = catalog["entries"][0]
        self.assertEqual(entry["rom_fingerprint"], self.fp)
        self.verify(entry["receipt"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(identity.main(self.cli_fixture()), 3)

    def test_cli_wrong_game_and_unsigned_report_never_create_catalog(self):
        wrong = {"file": self.file, "metadata": {"name": "塞尔达测试"}}
        for inputs in ({"patch_value": wrong}, {"source": self.report}):
            arguments = self.cli_fixture(**inputs)
            with contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(identity.main(arguments), 3)
            self.assertEqual(json.loads(output.getvalue())["status"], "pending_identity")
            self.assertFalse((self.root / "catalog.json").exists())

    def test_catalog_builder_rejects_duplicate_or_tampered_receipts(self):
        receipt = self.authorize()
        with self.assertRaises(identity.IdentityError):
            identity.build_catalog([receipt, receipt], key_path=self.key)
        receipt["payload"]["metadata"]["name"] = "塞尔达测试"
        with self.assertRaises(identity.IdentityError):
            identity.build_catalog([receipt], key_path=self.key)


if __name__ == "__main__":
    unittest.main()
