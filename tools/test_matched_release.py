#!/usr/bin/env python3
import unittest

import matched_release as mr


class MatchedReleaseTests(unittest.TestCase):
    def test_selects_highest_canonical_published_milestone(self):
        releases = [
            {"tagName": "chrome/m9", "isDraft": False, "isPrerelease": False},
            {"tagName": "chrome/m153-test", "isDraft": False, "isPrerelease": False},
            {"tagName": "chrome/m154", "isDraft": True, "isPrerelease": False},
            {"tagName": "chrome/m152", "isDraft": False, "isPrerelease": False},
        ]
        self.assertEqual(mr.select_latest_skia(releases), {"milestone": 152, "tag": "chrome/m152"})

    def test_only_matching_active_run_suppresses_dispatch(self):
        runs = [
            {"displayTitle": "target", "status": "failure"},
            {"displayTitle": "other", "status": "in_progress"},
        ]
        self.assertFalse(mr.has_active_target(runs, "target"))
        runs.append({"displayTitle": "target", "status": "queued"})
        self.assertTrue(mr.has_active_target(runs, "target"))

    def test_partial_or_mismatched_release_is_not_complete(self):
        expected = {"milestone": 153, "v8": "v" * 40, "built_skia": "s" * 40,
                    "built_dawn": "d" * 40, "skia_release_tag": "chrome/m153"}
        names = [f"{prefix}-release.zip" for prefix in mr.EXPECTED_ASSET_PREFIXES]
        manifests = [{"pair": dict(expected)} for _ in names]
        metadata = {"assets": names, "pair": dict(expected), "manifests": manifests}
        release = {"assets": [{"name": name} for name in names] + [{"name": "release-metadata.json"}]}
        self.assertTrue(mr.release_is_complete(release, metadata, expected))
        self.assertFalse(mr.release_is_complete(
            {"assets": release["assets"][:-2] + [{"name": "release-metadata.json"}]},
            metadata, expected))
        bad = {**metadata, "pair": {**expected, "v8": "wrong"}}
        self.assertFalse(mr.release_is_complete(release, bad, expected))


if __name__ == "__main__":
    unittest.main()
