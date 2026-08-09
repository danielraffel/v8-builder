#!/usr/bin/env python3
import base64
import io
import json
import unittest
from unittest.mock import patch

import milestone_pin as mp


class Response(io.BytesIO):
    pass


class MilestonePinTests(unittest.TestCase):
    def test_resolves_branch_head_and_exposes_dawn_mismatch(self):
        deps = """
          'skia_revision': '1111111111111111111111111111111111111111',
          'v8_revision': '2222222222222222222222222222222222222222',
          'dawn_revision': '3333333333333333333333333333333333333333',
        """

        def fake_open(url, timeout=30):
            if "chromiumdash" in url:
                return Response(json.dumps([{"milestone": 152, "chromium_branch": "7977"}]).encode())
            if "refs/branch-heads/7977?format=JSON" in url:
                return Response(b")]}'\n{\"commit\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}")
            if "DEPS?format=JSON" in url:
                return Response(b")]}'\n{\"id\":\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\"}")
            if "DEPS?format=TEXT" in url:
                return Response(base64.b64encode(deps.encode()))
            raise AssertionError(url)

        with patch.object(mp.urllib.request, "urlopen", fake_open):
            lock = mp.milestone_lock(152, expected_skia="1" * 40, built_dawn="4" * 40)
        self.assertEqual(lock["v8"], "2" * 40)
        self.assertEqual(lock["dawn"], "3" * 40)
        self.assertEqual(lock["built_dawn"], "4" * 40)
        self.assertFalse(lock["dawn_matches_chromium"])

    def test_rejects_release_whose_skia_is_not_on_milestone_branch(self):
        deps = "\n".join(
            f"'{name}_revision': '{sha * 40}',"
            for name, sha in (("skia", "1"), ("v8", "2"), ("dawn", "3"))
        )
        fake_json = lambda url: {"commit": "a" * 40} if "branch-heads" in url else {"id": "b" * 40}
        with patch.object(mp, "_milestone_info", lambda milestone: {"chromium_branch": "7977"}), \
             patch.object(mp, "_json_url", fake_json), \
             patch.object(mp.urllib.request, "urlopen", lambda *a, **k: Response(base64.b64encode(deps.encode()))):
            with self.assertRaisesRegex(SystemExit, "does not match published Skia"):
                mp.milestone_lock(152, expected_skia="9" * 40)

    def test_rejects_supplied_pins_that_disagree_with_skia_release(self):
        with patch.object(mp, "_skia_release_pins", return_value=("1" * 40, "4" * 40)):
            with self.assertRaisesRegex(SystemExit, "supplied Skia .* does not match"):
                mp.milestone_lock(152, expected_skia="9" * 40, skia_release_tag="chrome/m152")
            with self.assertRaisesRegex(SystemExit, "supplied built Dawn .* does not match"):
                mp.milestone_lock(152, built_dawn="9" * 40, skia_release_tag="chrome/m152")


if __name__ == "__main__":
    unittest.main()
