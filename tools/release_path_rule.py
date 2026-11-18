#!/usr/bin/env python3
"""Says whether the default branch's .github/CODEOWNERS holds a release-path rule.

The release-path gate (.github/workflows/release-path-review.yml) runs the
pinned Vellum rule, which refuses a CODEOWNERS that names no files. Until the
repository's CODEOWNERS lists release-path files, the gate has nothing to
enforce and passes without running the rule. The workflow runs this script
from its checkout of the default branch, never from the pull request's tree.

A rule exists when CODEOWNERS has at least one entry: a line that is neither
blank nor a comment. Prints `has_rule=true` or `has_rule=false` (the form
GITHUB_OUTPUT takes). Exit 0 either way.
"""

from __future__ import annotations

import sys
from pathlib import Path

CODEOWNERS = Path(".github") / "CODEOWNERS"


def has_rule(text: str) -> bool:
    return any(line.split("#", 1)[0].strip() for line in text.splitlines())


def main(root: Path = Path(".")) -> int:
    try:
        text = (root / CODEOWNERS).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    print(f"has_rule={'true' if has_rule(text) else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
