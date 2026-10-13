#!/usr/bin/env python3
"""Publish release assets without silently replacing published bytes.

Consumers (Pulp's ``tools/deps/manifest.json``) pin every asset of a published
release by sha256.  Re-uploading an asset with ``gh release upload --clobber``
changes those bytes in place, and every consumer that pinned the old digest
fails with a sha256 mismatch the next time it fetches, with nothing upstream to
say why.  This guard is the only path the workflows use to put a file on a
release.  For each file it compares the local sha256 with the asset already on
the release:

* asset absent                -> upload it (a new asset, nothing pinned yet)
* asset present, same digest  -> skip it (an idempotent re-run is fine)
* asset present, new digest   -> REFUSE, unless the asset is named in
  ``--allow-replace`` with a ``--reason``.  A deliberate replacement prints the
  old and new digests, re-reads the release to prove the new digest landed,
  and appends a ledger line to the release notes so consumers can repin in the
  same change.

Every decision is made before the first upload, so a refusal leaves the
release untouched.  ``--dry-run`` prints the plan and uploads nothing.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

LEDGER_START = "<!-- release-asset-replacements:start -->"
LEDGER_END = "<!-- release-asset-replacements:end -->"
LEDGER_TITLE = "### Asset replacements"

UPLOAD = "upload"
SKIP = "skip-identical"
REPLACE = "replace"
REFUSE = "refuse"
DEFER = "defer-new"


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
         allow_replace: set[str], defer_new: bool = False) -> list[Action]:
    """Decide what to do with each local file.  Pure: no network, no I/O.

    ``local`` maps asset name -> (path, sha256 hex).  ``existing`` maps asset
    name -> {"digest": hex, "id": int} for assets already on the release.
    """
    unknown = sorted(allow_replace - set(local))
    if unknown:
        raise GuardError(
            "--allow-replace names an asset that is not being published: "
            + ", ".join(unknown)
            + ". The override must name the exact asset file it is meant to replace.")
    actions = []
    for name in sorted(local):
        path, new = local[name]
        current = existing.get(name)
        if current is None:
            actions.append(Action(name, path, DEFER if defer_new else UPLOAD, new))
            continue
        old = current["digest"]
        if old == new:
            kind = SKIP
        elif name in allow_replace:
            kind = REPLACE
        else:
            kind = REFUSE
        actions.append(Action(name, path, kind, new, old, current.get("id")))
    return actions


def parse_replace_spec(spec: str, published: list[str]) -> list[str]:
    """Resolve a ``replace_assets`` value against the asset names being published.

    ``spec`` is a comma-separated list of exact asset names, or ``ALL:<count>``
    where ``count`` must equal the number of assets being published, so a
    blanket override states its blast radius and cannot silently grow when an
    asset is added.  An empty spec authorizes nothing.
    """
    spec = (spec or "").strip()
    if not spec:
        return []
    if spec.upper().startswith("ALL"):
        count = spec[3:]
        if not count.startswith(":") or not count[1:].strip().isdigit():
            raise GuardError(f"replace_assets={spec!r}: write ALL:<count>; this release "
                             f"publishes {len(published)} assets: {', '.join(published)}")
        if int(count[1:]) != len(published):
            raise GuardError(f"replace_assets={spec!r} but this release publishes "
                             f"{len(published)} assets: {', '.join(published)}")
        return list(published)
    names = [name.strip() for name in spec.split(",") if name.strip()]
    if not names:
        raise GuardError(f"replace_assets={spec!r} names no asset")
    return names


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
        "Published assets are pinned by digest downstream; replacing them in place",
        "breaks every consumer that pinned the old bytes. Nothing was uploaded.",
        "",
        "If this is an idempotent re-run you expected to be identical, the build is",
        "not byte-reproducible; keep the published asset and discard this one.",
        "",
        "To replace deliberately, re-dispatch the workflow with:",
    ]
    for action in refused:
        lines.append(f"  replace_asset={action.name}")
    lines += [
        "  replace_reason='<why the published bytes are wrong>'",
        "The replacement prints both digests and records them in the release notes.",
    ]
    if repin_hint:
        lines += ["", "Then repin consumers in the same change:"]
        for action in refused:
            lines.append("  " + repin_hint.format(
                asset=action.name, old=action.old_digest, new=action.new_digest, tag=tag))
    return "\n".join(lines)


def ledger_line(action: Action, reason: str, when: str, run_url: str, commit: str) -> str:
    provenance = []
    if run_url:
        provenance.append(f"run {run_url}")
    if commit:
        provenance.append(f"commit {commit}")
    tail = f" ({', '.join(provenance)})" if provenance else ""
    return (f"- {when} `{action.name}` replaced: sha256 `{action.old_digest}` -> "
            f"`{action.new_digest}`. Reason: {reason}{tail}")


def extract_ledger(body: str) -> list[str]:
    if LEDGER_START not in body or LEDGER_END not in body:
        return []
    inner = body.split(LEDGER_START, 1)[1].split(LEDGER_END, 1)[0]
    return [line for line in inner.splitlines() if line.startswith("- ")]


def with_ledger(body: str, entries: list[str]) -> str:
    """Return ``body`` with the ledger block holding exactly ``entries``."""
    if LEDGER_START in body and LEDGER_END in body:
        head = body.split(LEDGER_START, 1)[0].rstrip()
        tail = body.split(LEDGER_END, 1)[1].lstrip("\n")
    else:
        head, tail = body.rstrip(), ""
    if not entries:
        return head + ("\n\n" + tail if tail else "") + "\n"
    block = "\n".join([LEDGER_START, LEDGER_TITLE, "", *entries, LEDGER_END])
    parts = [head, block] + ([tail.rstrip()] if tail.strip() else [])
    return "\n\n".join(part for part in parts if part) + "\n"


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

    def upload(self, tag: str, path: Path, clobber: bool) -> None:
        args = ["release", "upload", tag, str(path), "--repo", self.repo]
        if clobber:
            args.append("--clobber")
        self._run(*args)

    def set_body(self, release_id: int, body: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as handle:
            json.dump({"body": body}, handle)
            payload = handle.name
        try:
            self._run("api", "--method", "PATCH", f"repos/{self.repo}/releases/{release_id}",
                      "--input", payload)
        finally:
            os.unlink(payload)


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
    local = {}
    for raw in args.files:
        path = Path(raw)
        if not path.is_file() or path.stat().st_size == 0:
            raise GuardError(f"missing or empty file: {path}")
        if path.name in local:
            raise GuardError(f"two files publish as the same asset name: {path.name}")
        local[path.name] = (path, sha256_file(path))

    allow = set(args.allow_replace or [])
    allow.update(parse_replace_spec(args.replace_assets, sorted(local)))
    allow.discard("")
    if allow and not (args.reason or "").strip():
        raise GuardError("replacing published assets requires --reason: "
                         "say why the published bytes are wrong.")
    if allow:
        print(f"release-asset-guard: {len(allow)} asset(s) authorized for replacement on {args.tag}:")
        for name in sorted(allow):
            print(f"  {name}")

    gh = Gh(args.repo, args.gh)
    release = gh.release(args.tag)
    if release is None and not args.missing_release_ok:
        raise GuardError(f"release {args.tag!r} does not exist in {args.repo}; "
                         "a backfill must target an existing published release.")
    actions = plan(local, existing_assets(gh, args.tag, release), allow, args.defer_new)
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
            gh.upload(args.tag, action.path, clobber=False)
    replaced = [a for a in actions if a.kind == REPLACE]
    for action in replaced:
        print(f"release-asset-guard: REPLACING {action.name} on {args.tag}\n"
              f"  old sha256:{action.old_digest}\n  new sha256:{action.new_digest}\n"
              f"  reason: {args.reason}")
        gh.upload(args.tag, action.path, clobber=True)

    if replaced:
        after = gh.release(args.tag) or {}
        landed = existing_assets(gh, args.tag, after)
        for action in replaced:
            got = landed.get(action.name, {}).get("digest")
            if got != action.new_digest:
                raise GuardError(f"{action.name}: release now reports sha256:{got}, "
                                 f"expected sha256:{action.new_digest}")
        when = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_url = ""
        if os.environ.get("GITHUB_RUN_ID"):
            run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                       f"{os.environ.get('GITHUB_REPOSITORY', args.repo)}/actions/runs/"
                       f"{os.environ['GITHUB_RUN_ID']}")
        new_lines = [ledger_line(a, args.reason.strip(), when, run_url,
                                 os.environ.get("GITHUB_SHA", "")) for a in replaced]
        body = after.get("body") or ""
        gh.set_body(after["id"], with_ledger(body, extract_ledger(body) + new_lines))
        summary = ["### Release asset replaced", ""] + new_lines
        if args.repin_hint:
            summary += ["", "Repin consumers:"] + [
                "- " + args.repin_hint.format(asset=a.name, old=a.old_digest,
                                              new=a.new_digest, tag=args.tag)
                for a in replaced]
        _summary("\n".join(summary))
        print("\n".join(summary))
    return 0


def cmd_carry_ledger(args: argparse.Namespace) -> int:
    """Keep the replacement ledger when release notes are regenerated."""
    gh = Gh(args.repo, args.gh)
    release = gh.release(args.tag)
    entries = extract_ledger((release or {}).get("body") or "")
    notes = Path(args.notes)
    notes.write_text(with_ledger(notes.read_text(encoding="utf-8"), entries), encoding="utf-8")
    print(f"release-asset-guard: carried {len(entries)} ledger entr"
          f"{'y' if len(entries) == 1 else 'ies'} into {notes}")
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
                         help="asset name whose published bytes may be replaced")
    publish.add_argument("--replace-assets", default="",
                         help="comma-separated asset names, or ALL:<count> (count = assets published)")
    publish.add_argument("--reason", default="",
                         help="required with --allow-replace/--replace-assets")
    publish.add_argument("--dry-run", action="store_true")
    publish.add_argument("--missing-release-ok", action="store_true",
                         help="treat a missing release as empty (a new release is created later)")
    publish.add_argument("--defer-new", action="store_true",
                         help="leave absent assets for a later step to upload")
    publish.add_argument("--repin-hint", default="",
                         help="consumer repin instruction; {asset} {old} {new} {tag} are filled in")
    publish.set_defaults(func=cmd_publish)

    carry = sub.add_parser("carry-ledger", help="copy the replacement ledger into notes")
    carry.add_argument("notes")
    carry.set_defaults(func=cmd_carry_ledger)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GuardError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
