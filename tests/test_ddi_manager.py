"""离线单测：DDI 芯片身份匹配（对齐 AutoPilot）。"""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from backend_function.ddi_manager import (
    ddi_identity_matches,
    identity_payload_paths,
    list_local_ddi_restore_dirs,
    local_ddi_summary,
    parse_personalization_ids,
    _mount_output_ok,
)


class DdiManagerTests(unittest.TestCase):
    def test_identity_payload_paths_follow_matching_chip(self):
        data = {
            "BuildIdentities": [
                {
                    "ApChipID": "0x8140",
                    "ApBoardID": "0x4",
                    "Manifest": {
                        "PersonalizedDMG": {"Info": {"Path": "Image.dmg"}},
                        "LoadableTrustCache": {"Info": {"Path": "Image.dmg.trustcache"}},
                    },
                },
                {
                    "ApChipID": "0x8010",
                    "ApBoardID": "0x0C",
                    "Manifest": {
                        "PersonalizedDMG": {"Info": {"Path": "022-old.dmg"}},
                    },
                },
            ]
        }
        self.assertEqual(
            identity_payload_paths(data, 0x8140, 0x4),
            ["Image.dmg", "Image.dmg.trustcache"],
        )
        self.assertEqual(identity_payload_paths(data, 0x8150, 0x8), [])

    def test_parse_chip_from_findidentity_error(self):
        text = (
            'MountImage: could not find identity for identifiers '
            '{BoardId:4 ChipID:33088 SecurityDomain:1}: '
            'findIdentity: failed to find identity for ApBoardId 0x4 and ApChipId 0x8140'
        )
        self.assertEqual(parse_personalization_ids(text), (0x8140, 0x4))
        self.assertEqual(parse_personalization_ids("BoardId:14 ChipID:33088"), (33088, 14))
        self.assertIsNone(parse_personalization_ids("no identity here"))

    def test_mount_output_rejects_findidentity(self):
        bad = 'msg="findIdentity: failed ... ApChipId 0x8140"'
        self.assertFalse(_mount_output_ok(bad))
        self.assertTrue(_mount_output_ok("success mounting image"))
        self.assertTrue(_mount_output_ok("already mounted"))

    def test_list_and_match_against_bundled_ddi(self):
        root = Path(__file__).resolve().parents[1] / "IOSPrechecker" / "devimages"
        if not root.is_dir():
            self.skipTest("no IOSPrechecker/devimages")
        dirs = list_local_ddi_restore_dirs(root)
        # 至少应能看到 ddi-15F31d/Restore（若仓库带了该包）
        if not dirs:
            self.skipTest("no Restore/BuildManifest under devimages")
        # 0x8140（A18 / 新 iPad 一类）不应被旧 ddi-15F31d 误匹配
        for restore in dirs:
            if restore.parent.name.lower() == "ddi-15f31d":
                self.assertFalse(
                    ddi_identity_matches(restore, 0x8140, 0x4),
                    "ddi-15F31d must not match ApChipId 0x8140",
                )
                self.assertFalse(
                    ddi_identity_matches(restore, 0x8150, 0x8),
                    "ddi-15F31d must not match ApChipId 0x8150",
                )

        summary = local_ddi_summary(root)
        self.assertGreaterEqual(summary["count"], 1)

    def test_identity_matches_synthetic_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            restore = Path(td) / "ddi-fake" / "Restore"
            restore.mkdir(parents=True)
            manifest = {
                "BuildIdentities": [
                    {
                        "ApChipID": "0x8140",
                        "ApBoardID": "0x4",
                        "Info": {},
                    }
                ]
            }
            (restore / "BuildManifest.plist").write_bytes(plistlib.dumps(manifest))
            self.assertTrue(ddi_identity_matches(restore, 0x8140, 0x4))
            self.assertFalse(ddi_identity_matches(restore, 0x8150, 0x8))
            listed = list_local_ddi_restore_dirs(td)
            self.assertEqual(listed, [restore])


if __name__ == "__main__":
    unittest.main()
