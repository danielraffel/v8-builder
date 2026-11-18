#!/usr/bin/env python3
"""zizmor is report only: no step that runs it may be able to fail the job.

A job-level continue-on-error keeps the workflow run green, but GitHub still
reports the job's own check as `failure` when a step fails, and an approved
PR then reads UNSTABLE. So every step that runs zizmor must itself carry
continue-on-error, so the job concludes success.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "zizmor.yml"


def steps(text):
    body = text.split("\n    steps:\n", 1)[1]
    return [s for s in re.split(r"(?m)^      - ", body) if s.strip()]


class ZizmorReportOnly(unittest.TestCase):
    def test_every_zizmor_step_cannot_fail_the_job(self):
        zsteps = [s for s in steps(WORKFLOW.read_text()) if "zizmor" in s.lower()]
        self.assertGreaterEqual(len(zsteps), 1)
        for s in zsteps:
            self.assertRegex(
                s, r"(?m)^        continue-on-error: true\s*$",
                "zizmor step can fail the job:\n" + s)


if __name__ == "__main__":
    unittest.main()
