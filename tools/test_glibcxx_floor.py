#!/usr/bin/env python3
import unittest

from check_glibcxx_floor import versions_from_text


class GlibcxxFloorTests(unittest.TestCase):
    def test_versions_sort_numerically_and_deduplicate(self):
        text = "GLIBCXX_3.4.9 x GLIBCXX_3.4.29 y GLIBCXX_3.4.9"
        self.assertEqual(versions_from_text(text), ["3.4.9", "3.4.29"])


if __name__ == "__main__":
    unittest.main()
