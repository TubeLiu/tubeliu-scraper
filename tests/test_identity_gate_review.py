"""Independent negative cases for writes that must fail before any game changes."""
from __future__ import annotations
import unittest
import test_identity_deploy as fixtures


class IdentityGateReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.IdentityDeploymentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_new_record_cannot_copy_another_games_metadata(self):
        case = self.fixture
        (case.rom.parent / "Zelda.nds").write_bytes(b"a different synthetic ROM")
        case.source.write_bytes(case.original.replace(b"</gameList>", b"<game><path>./Zelda.nds</path><name>Super Mario</name></game></gameList>"))
        self.assertEqual(case.plan(), 2)
        self.assertEqual(case.target_xml.read_bytes(), case.original)
        self.assertFalse((case.run / "deployment_plan.json").exists())

    def test_prepared_source_changed_after_plan_is_rejected_before_write(self):
        case = self.fixture
        self.assertEqual(case.plan(), 0)
        case.source.write_bytes(case.source.read_bytes().replace(b"Super Mario", b"Wrong Zelda"))
        self.assertEqual(case.command("apply", "--run", str(case.run)), 2)
        self.assertEqual(case.target_xml.read_bytes(), case.original)

    def test_later_changed_media_blocks_an_earlier_valid_gamelist_in_same_plan(self):
        case = self.fixture
        cover = case.root / "cover.png"
        cover.write_bytes(b"approved synthetic cover, not real game artwork")
        relative = "downloaded_media/nds/covers/Mario.png"
        entry = fixtures.approved_entry(case.rom, "nds", "Mario.nds", {"name": "Super Mario"},
                                        [{"local": str(cover), "relative": relative}])
        fixtures.write_json(case.catalog, {"schema_version": 1, "entries": [entry]})
        fixtures.write_json(case.manifest, {"identity_catalog": str(case.catalog), "files": [
            {"local": str(case.source), "relative": case.relative},
            {"local": str(cover), "relative": relative},
        ]})
        self.assertEqual(case.plan(), 0)
        cover.write_bytes(b"another game's synthetic cover bytes")
        self.assertEqual(case.command("apply", "--run", str(case.run)), 2)
        self.assertEqual(case.target_xml.read_bytes(), case.original)
        self.assertFalse((case.target / relative).exists())


if __name__ == "__main__":
    unittest.main()
