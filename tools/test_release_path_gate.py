#!/usr/bin/env python3
"""Checks the release-path gate: its workflows, the rule-presence script, and the
reviewer policy in .shipyard/config.toml. The CODEOWNERS checks use fixtures,
and run against the real file only once it holds a release-path rule, so the
gate passes with no release-path entries.

The rule itself is Vellum's scripts/release_path_review.py, which the
workflow checks out at a pinned commit; its own tests live in Vellum. These
tests catch the mistakes that would make it refuse every pull request or pass
one it should not: a CODEOWNERS entry it rejects or that names no file, an
unpinned or widened rule checkout, a trigger that would run a pull request's
copy, or an empty reviewer list.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import release_path_rule  # noqa: E402

CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
WORKFLOW = ROOT / ".github" / "workflows" / "release-path-review.yml"
TRIGGER = ROOT / ".github" / "workflows" / "release-path-review-trigger.yml"
CONFIG = ROOT / ".shipyard" / "config.toml"


def entries(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if fields:
            found.append(fields[0])
    return found


def refusal(pattern: str) -> str | None:
    """The rule's own acceptance test (release_path_review.protected_paths)."""
    if not pattern.startswith("/"):
        return "not anchored"
    if pattern.endswith("/"):
        return "a directory"
    if any(c in pattern for c in "*?[]!\\"):
        return "a glob"
    return None


def real_codeowners() -> str:
    return CODEOWNERS.read_text(encoding="utf-8") if CODEOWNERS.is_file() else ""


def triggers(text: str) -> set[str]:
    block = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"^  ([a-z_]+):", block, re.MULTILINE))


FIXTURE = """\
# a comment
/.github/CODEOWNERS @owner
/tools/a.py @owner   # trailing comment

/tools/b.py @owner
"""


class RulePresenceTest(unittest.TestCase):
    """Driven by fixtures, so it holds whether or not CODEOWNERS has entries."""

    def test_has_rule_reads_entries_not_comments(self) -> None:
        self.assertTrue(release_path_rule.has_rule(FIXTURE))
        self.assertFalse(release_path_rule.has_rule(""))
        self.assertFalse(release_path_rule.has_rule("# only a comment\n\n   \n  # another\n"))

    def run_script(self, codeowners: str | None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if codeowners is not None:
                (root / ".github").mkdir()
                (root / ".github" / "CODEOWNERS").write_text(codeowners, encoding="utf-8")
            out = subprocess.run(
                [sys.executable, "-I", str(ROOT / "tools" / "release_path_rule.py")],
                cwd=root, capture_output=True, text=True, check=True,
            )
        return out.stdout.strip()

    def test_the_script_passes_a_repository_with_no_rule(self) -> None:
        self.assertEqual(self.run_script(None), "has_rule=false")
        self.assertEqual(self.run_script("# nothing yet\n"), "has_rule=false")

    def test_the_script_sees_a_rule(self) -> None:
        self.assertEqual(self.run_script(FIXTURE), "has_rule=true")

    def test_entries_ignore_comments_and_owners(self) -> None:
        self.assertEqual(entries(FIXTURE), ["/.github/CODEOWNERS", "/tools/a.py", "/tools/b.py"])

    def test_the_entry_check_refuses_what_the_rule_refuses(self) -> None:
        for good in ("/tools/a.py", "/.github/workflows/x.yml"):
            self.assertIsNone(refusal(good), good)
        for bad in ("tools/a.py", "/tools/", "/tools/*.py", "/tools/[a].py", "/a\\b", "/a!b", "/a?"):
            self.assertIsNotNone(refusal(bad), bad)


class CodeOwnersTest(unittest.TestCase):
    """Checks the real CODEOWNERS once it holds a release-path rule."""

    def setUp(self) -> None:
        self.text = real_codeowners()
        if not release_path_rule.has_rule(self.text):
            self.skipTest("CODEOWNERS has no release-path rule yet")

    def test_every_entry_is_an_anchored_file_that_exists(self) -> None:
        for pattern in entries(self.text):
            with self.subTest(pattern=pattern):
                self.assertIsNone(refusal(pattern))
                self.assertTrue((ROOT / pattern.lstrip("/")).is_file(), "no such file")

    def test_the_gate_protects_its_own_inputs(self) -> None:
        names = set(entries(self.text))
        for path in ("/.github/CODEOWNERS", "/.shipyard/config.toml", "/tools/test_release_path_gate.py"):
            self.assertIn(path, names)

    def test_no_entry_is_listed_twice(self) -> None:
        names = entries(self.text)
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
        # The pull request's head is a status target, never a checkout.
        for line in text.splitlines():
            if line.strip().startswith("ref:"):
                self.assertNotIn("pull_request", line)

    def test_the_workflows_run_only_the_base_copy(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(triggers(text), {"pull_request_target", "issue_comment", "workflow_run"})
        self.assertIn("workflows: [Release path review trigger]", text)
        trigger = TRIGGER.read_text(encoding="utf-8")
        self.assertIn("\nname: Release path review trigger\n", trigger)
        self.assertIn("\npermissions: {}\n", trigger)

    def test_the_rule_runs_only_when_codeowners_has_one(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("run: python3 -I tools/release_path_rule.py >> \"$GITHUB_OUTPUT\"", text)
        self.assertIn("if: steps.rule.outputs.has_rule != 'true'", text)
        for name in ("Fetch the merge-queue commit", "Decide release-path-review", "Post release-path-review"):
            step = text.split(f"- name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
            self.assertIn("steps.rule.outputs.has_rule == 'true'", step, name)
        # The no-rule pass reports success, and nothing else.
        step = text.split("- name: Pass without a release-path rule\n", 1)[1].split("\n      # ", 1)[0]
        self.assertIn("-f state=success -f context=release-path-review", step)

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
