#!/usr/bin/env python3
"""Checks the release-path gate's inputs: .github/CODEOWNERS, the workflows and
the reviewer policy in .shipyard/config.toml.

The rule itself is Vellum's scripts/release_path_review.py, which the
workflow checks out at a pinned commit; its own tests live in Vellum. These
tests catch the mistakes that would make it refuse every pull request or pass
one it should not: a CODEOWNERS entry it rejects or that names no file, an
unpinned or widened rule checkout, a trigger that would run a pull request's
copy, or an empty reviewer list.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
WORKFLOW = ROOT / ".github" / "workflows" / "release-path-review.yml"
TRIGGER = ROOT / ".github" / "workflows" / "release-path-review-trigger.yml"
CONFIG = ROOT / ".shipyard" / "config.toml"


def entries() -> list[str]:
    found = []
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if fields:
            found.append(fields[0])
    return found


def triggers(text: str) -> set[str]:
    block = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"^  ([a-z_]+):", block, re.MULTILINE))


class CodeOwnersTest(unittest.TestCase):
    def test_every_entry_is_an_anchored_file_that_exists(self) -> None:
        names = entries()
        self.assertTrue(names, "CODEOWNERS names no files")
        for pattern in names:
            with self.subTest(pattern=pattern):
                # The rule's own acceptance test (release_path_review.protected_paths).
                self.assertTrue(pattern.startswith("/"), "not anchored")
                self.assertFalse(pattern.endswith("/"), "a directory")
                self.assertFalse(any(c in pattern for c in "*?[]!\\"), "a glob")
                self.assertTrue((ROOT / pattern.lstrip("/")).is_file(), "no such file")

    def test_the_gate_protects_its_own_inputs(self) -> None:
        names = set(entries())
        for path in ("/.github/CODEOWNERS", "/.shipyard/config.toml", "/tools/test_release_path_gate.py"):
            self.assertIn(path, names)

    def test_no_entry_is_listed_twice(self) -> None:
        names = entries()
        self.assertEqual(len(names), len(set(names)))


class WorkflowTest(unittest.TestCase):
    def test_the_rule_is_vellums_at_a_pinned_commit(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("repository: Generous-Corp/vellum\n", text)
        self.assertRegex(text, r"\n          ref: [0-9a-f]{40}\n          path: \.release-path-rule\n")
        sparse = text.split("sparse-checkout: |\n", 1)[1].split("sparse-checkout-cone-mode", 1)[0]
        self.assertEqual(sparse.split(), ["scripts/release_path_review.py", "scripts/counted_verdicts.py"])
        self.assertIn("run: python3 -I scripts/release_path_review.py", text)

    def test_the_base_checkout_is_the_default_branch(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", text)
        self.assertNotIn("github.event.pull_request.head", text)

    def test_the_workflows_run_only_the_base_copy(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(triggers(text), {"pull_request_target", "issue_comment", "workflow_run"})
        self.assertIn("workflows: [Release path review trigger]", text)
        trigger = TRIGGER.read_text(encoding="utf-8")
        self.assertIn("\nname: Release path review trigger\n", trigger)
        self.assertIn("\npermissions: {}\n", trigger)

    def test_no_secret_and_every_checkout_drops_credentials(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("secrets.", text)
        self.assertEqual(text.count("uses: actions/checkout@"), text.count("persist-credentials: false"))
        for action in re.findall(r"^\s*(?:- )?uses: (\S+)", text, re.MULTILINE):
            self.assertRegex(action, r"@[0-9a-f]{40}$")


class ReviewerPolicyTest(unittest.TestCase):
    def test_two_signers_and_a_session_glob_are_named(self) -> None:
        policy = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["auto_merge"]
        signers = policy["reviewer_signers"]
        self.assertGreaterEqual(len(set(signers)), 2, "two independent APPROVEs need two signers")
        self.assertTrue(policy["reviewer_stamp_sessions"])


if __name__ == "__main__":
    unittest.main()
