#!/usr/bin/env python3
"""Publish release assets without replacing published bytes.

Consumers (Pulp's ``tools/deps/manifest.json``) pin every asset of a published
release by sha256.  Re-uploading an asset with ``gh release upload --clobber``
changes those bytes in place, and every consumer that pinned the old digest
fails with a sha256 mismatch the next time it fetches, with nothing upstream to
say why.  This guard is the only path the workflows use to put a file on a
release.  For each file it compares the local sha256 with the asset already on
the release:

* asset absent                -> upload it (a new asset, nothing pinned yet)
* asset present, same digest  -> skip it (an idempotent re-run is fine)
* asset present, new digest   -> REFUSE. Publish under a new release tag or
  asset name and update consumer pins in the same change.

Every decision is made before the first upload, so a refusal leaves the
release untouched.  ``--dry-run`` prints the plan and uploads nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

UPLOAD = "upload"
SKIP = "skip-identical"
REFUSE = "refuse"
DEFER = "defer-new"

IMMUTABLE_MESSAGE = ("Published assets are immutable; publish the corrected build under a "
                     "new release tag (or a new asset name) and update consumer pins "
                     "in the same change.")


class GuardError(Exception):
    """A refusal or misuse; the message is what the operator needs."""


@dataclass
class Action:
    name: str
    path: Path
    kind: str
    new_digest: str
    old_digest: str | None = None
    asset_id: int | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_digest(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(":", 1)[1] if value.startswith("sha256:") else value


def plan(local: dict[str, tuple[Path, str]], existing: dict[str, dict],
         defer_new: bool = False) -> list[Action]:
    """Decide what to do with each local file.  Pure: no network, no I/O.

    ``local`` maps asset name -> (path, sha256 hex).  ``existing`` maps asset
    name -> {"digest": hex, "id": int} for assets already on the release.
    """
    actions = []
    for name in sorted(local):
        path, new = local[name]
        current = existing.get(name)
        if current is None:
            actions.append(Action(name, path, DEFER if defer_new else UPLOAD, new))
            continue
        old = current["digest"]
        kind = SKIP if old == new else REFUSE
        actions.append(Action(name, path, kind, new, old, current.get("id")))
    return actions


def refusal_message(refused: list[Action], tag: str, repin_hint: str) -> str:
    lines = [
        f"release-asset-guard: REFUSED. {len(refused)} asset(s) on published release "
        f"{tag!r} would change bytes in place:",
    ]
    for action in refused:
        lines += [
            f"  asset:      {action.name}",
            f"  published:  sha256:{action.old_digest}",
            f"  this build: sha256:{action.new_digest}",
        ]
    lines += [
        "",
        IMMUTABLE_MESSAGE,
        "Nothing was uploaded.",
        "",
        "If this is an idempotent re-run you expected to be identical, the build is",
        "not byte-reproducible; keep the published asset and discard this one.",
        "",
    ]
    if repin_hint:
        lines += ["", "Then repin consumers in the same change:"]
        for action in refused:
            lines.append("  " + repin_hint.format(
                asset=action.name, old=action.old_digest, new=action.new_digest, tag=tag))
    return "\n".join(lines)


class Gh:
    """Thin ``gh`` wrapper so tests can substitute a fake executable."""

    def __init__(self, repo: str, gh: str = "gh"):
        self.repo = repo
        self.gh = gh

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run([self.gh, *args], capture_output=True, text=True)
        if check and result.returncode != 0:
            raise GuardError(f"gh {' '.join(args)} failed ({result.returncode}): "
                             f"{result.stderr.strip() or result.stdout.strip()}")
        return result

    def release(self, tag: str) -> dict | None:
        result = self._run("api", f"repos/{self.repo}/releases/tags/{tag}", check=False)
        if result.returncode != 0:
            if "404" in result.stderr or "Not Found" in result.stderr:
                return None
            raise GuardError(f"could not read release {tag!r}: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def asset_digest(self, tag: str, asset: dict) -> str:
        digest = normalize_digest(asset.get("digest"))
        if digest:
            return digest
        # Assets uploaded before GitHub recorded digests: hash the bytes.
        with tempfile.TemporaryDirectory() as tmp:
            self._run("release", "download", tag, "--repo", self.repo,
                      "--pattern", asset["name"], "--dir", tmp)
            return sha256_file(Path(tmp) / asset["name"])

    def upload(self, tag: str, path: Path) -> None:
        self._run("release", "upload", tag, str(path), "--repo", self.repo)


def existing_assets(gh: Gh, tag: str, release: dict | None) -> dict[str, dict]:
    if not release:
        return {}
    return {asset["name"]: {"digest": gh.asset_digest(tag, asset), "id": asset["id"]}
            for asset in release.get("assets", [])}


def _summary(text: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def cmd_publish(args: argparse.Namespace) -> int:
    if args.allow_replace or args.replace_assets or args.reason:
        raise GuardError(IMMUTABLE_MESSAGE)
    local = {}
    for raw in args.files:
        path = Path(raw)
        if not path.is_file() or path.stat().st_size == 0:
            raise GuardError(f"missing or empty file: {path}")
        if path.name in local:
            raise GuardError(f"two files publish as the same asset name: {path.name}")
        local[path.name] = (path, sha256_file(path))

    gh = Gh(args.repo, args.gh)
    release = gh.release(args.tag)
    if release is None and not args.missing_release_ok:
        raise GuardError(f"release {args.tag!r} does not exist in {args.repo}; "
                         "a backfill must target an existing published release.")
    actions = plan(local, existing_assets(gh, args.tag, release), args.defer_new)
    for action in actions:
        old = f" published sha256:{action.old_digest}" if action.old_digest else ""
        print(f"release-asset-guard: {action.kind:<15} {action.name} "
              f"new sha256:{action.new_digest}{old}")

    refused = [a for a in actions if a.kind == REFUSE]
    if refused:
        message = refusal_message(refused, args.tag, args.repin_hint or "")
        _summary("```\n" + message + "\n```")
        raise GuardError(message)
    if args.dry_run:
        print("release-asset-guard: dry run; nothing uploaded.")
        return 0

    for action in actions:
        if action.kind == UPLOAD:
            gh.upload(args.tag, action.path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--gh", default=os.environ.get("RELEASE_ASSET_GUARD_GH", "gh"))
    sub = parser.add_subparsers(dest="command", required=True)

    publish = sub.add_parser("publish", help="upload files under the immutability rules")
    publish.add_argument("files", nargs="+")
    publish.add_argument("--allow-replace", action="append", default=[],
                         help="obsolete; always refuses because published assets are immutable")
    publish.add_argument("--replace-assets", default="",
                         help="obsolete; always refuses because published assets are immutable")
    publish.add_argument("--reason", default="",
                         help="obsolete; always refuses because published assets are immutable")
    publish.add_argument("--dry-run", action="store_true")
    publish.add_argument("--missing-release-ok", action="store_true",
                         help="treat a missing release as empty (a new release is created later)")
    publish.add_argument("--defer-new", action="store_true",
                         help="leave absent assets for a later step to upload")
    publish.add_argument("--repin-hint", default="",
                         help="consumer repin instruction; {asset} {old} {new} {tag} are filled in")
    publish.set_defaults(func=cmd_publish)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GuardError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
