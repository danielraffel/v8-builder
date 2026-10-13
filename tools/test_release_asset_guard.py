#!/usr/bin/env python3
"""Tests for release_asset_guard.py, end to end against a fake ``gh``.

The fake keeps a release in a JSON file and logs every call, so the tests can
assert both the outcome and that nothing was uploaded when a change is refused.
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import release_asset_guard as guard  # noqa: E402

GUARD = HERE / "release_asset_guard.py"
REPO = "owner/v8-builder"
TAG = "v8-m153-15.3.76.5-26cef0256b0e"

FAKE_GH = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import hashlib, json, os, shutil, sys
    state_path = os.environ["FAKE_GH_STATE"]
    state = json.load(open(state_path))
    state["calls"].append(sys.argv[1:])
    def save():
        json.dump(state, open(state_path, "w"), indent=1)
    args = sys.argv[1:]
    rel = state["release"]
    if args[0] == "api" and args[1].startswith("repos/") and "/releases/tags/" in args[1]:
        save()
        if rel is None:
            sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
        print(json.dumps(rel)); sys.exit(0)
    if args[0] == "api" and args[1] == "--method":
        body = json.load(open(args[args.index("--input") + 1]))["body"]
        rel["body"] = body; save(); print("{}"); sys.exit(0)
    if args[:2] == ["release", "upload"]:
        path = args[3]; name = os.path.basename(path)
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        existing = [a for a in rel["assets"] if a["name"] == name]
        if existing and "--clobber" not in args:
            save(); sys.stderr.write("asset under the same name already exists\n"); sys.exit(1)
        if os.environ.get("FAKE_GH_UPLOAD_DOES_NOT_LAND"):
            save(); sys.exit(0)  # gh reports success but the release keeps the old bytes
        rel["assets"] = [a for a in rel["assets"] if a["name"] != name]
        rel["assets"].append({"name": name, "id": 900 + len(state["calls"]),
                              "digest": "sha256:" + digest})
        state["blobs"][name] = open(path, "rb").read().hex()
        save(); sys.exit(0)
    if args[:2] == ["release", "download"]:
        name = args[args.index("--pattern") + 1]; out = args[args.index("--dir") + 1]
        open(os.path.join(out, name), "wb").write(bytes.fromhex(state["blobs"][name]))
        save(); sys.exit(0)
    save(); sys.stderr.write("fake gh: unexpected call %r\n" % args); sys.exit(2)
''').lstrip()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeRelease:
    def __init__(self, tmp: Path, assets: dict[str, bytes] | None, body: str = "notes\n",
                 record_digests: bool = True):
        self.tmp = tmp
        self.state_path = tmp / "state.json"
        self.gh = tmp / "gh"
        self.gh.write_text(FAKE_GH)
        self.gh.chmod(self.gh.stat().st_mode | stat.S_IEXEC)
        release = None
        blobs = {}
        if assets is not None:
            release = {"id": 77, "tag_name": TAG, "body": body, "assets": []}
            for index, (name, data) in enumerate(sorted(assets.items())):
                asset = {"name": name, "id": 100 + index,
                         "digest": ("sha256:" + sha(data)) if record_digests else None}
                release["assets"].append(asset)
                blobs[name] = data.hex()
        self.state_path.write_text(json.dumps({"release": release, "calls": [], "blobs": blobs}))

    @property
    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def uploads(self) -> list[list[str]]:
        return [c for c in self.state["calls"] if c[:2] == ["release", "upload"]]

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, FAKE_GH_STATE=str(self.state_path),
                   GITHUB_STEP_SUMMARY=str(self.tmp / "summary.md"), **extra_env)
        for key in ("GITHUB_RUN_ID", "GITHUB_SHA"):
            env.pop(key, None)
        return subprocess.run(
            [sys.executable, str(GUARD), "--repo", REPO, "--tag", TAG, "--gh", str(self.gh), *args],
            capture_output=True, text=True, env=env, timeout=60)


class GuardEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def local(self, name: str, data: bytes) -> str:
        path = self.out / name
        path.write_bytes(data)
        return str(path)

    def test_changed_digest_is_refused_and_nothing_uploads(self):
        name = "v8-win-arm64-m153-15.3.76.5-26cef0256b0e.zip"
        fake = FakeRelease(self.tmp, {name: b"published bytes"})
        result = fake.run("publish", self.local(name, b"rebuilt bytes"),
                          "--repin-hint", "set {asset} sha256 {old} -> {new}")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("REFUSED", result.stderr)
        self.assertIn("published:  sha256:" + sha(b"published bytes"), result.stderr)
        self.assertIn("this build: sha256:" + sha(b"rebuilt bytes"), result.stderr)
        self.assertIn(f"replace_asset={name}", result.stderr)
        self.assertIn(f"set {name} sha256 {sha(b'published bytes')} -> {sha(b'rebuilt bytes')}",
                      result.stderr)
        self.assertEqual(fake.uploads(), [])
        self.assertEqual(fake.state["release"]["assets"][0]["digest"],
                         "sha256:" + sha(b"published bytes"))

    def test_identical_rerun_passes_without_uploading(self):
        name = "v8-win-arm64-m153-15.3.76.5-26cef0256b0e.zip"
        fake = FakeRelease(self.tmp, {name: b"same bytes"})
        result = fake.run("publish", self.local(name, b"same bytes"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skip-identical", result.stdout)
        self.assertEqual(fake.uploads(), [])

    def test_new_asset_uploads_without_clobber(self):
        fake = FakeRelease(self.tmp, {"other.zip": b"x"})
        result = fake.run("publish", self.local("new.zip", b"new bytes"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(fake.uploads()), 1)
        self.assertNotIn("--clobber", fake.uploads()[0])

    def test_refusal_is_atomic_across_files(self):
        fake = FakeRelease(self.tmp, {"a.zip": b"a-old"})
        result = fake.run("publish", self.local("a.zip", b"a-new"), self.local("b.zip", b"b-new"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(fake.uploads(), [], "a refusal must not leave a partial upload behind")

    def test_override_replaces_and_records_both_digests(self):
        name = "v8-win-arm64-m153-15.3.76.5-26cef0256b0e.zip"
        fake = FakeRelease(self.tmp, {name: b"first build"}, body="Matched V8 notes\n")
        result = fake.run("publish", self.local(name, b"second build"),
                          "--allow-replace", name, "--reason", "rebuilt after a toolchain fix")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("old sha256:" + sha(b"first build"), result.stdout)
        self.assertIn("new sha256:" + sha(b"second build"), result.stdout)
        uploads = fake.uploads()
        self.assertEqual(len(uploads), 1)
        self.assertIn("--clobber", uploads[0])
        rel = fake.state["release"]
        self.assertEqual(rel["assets"][0]["digest"], "sha256:" + sha(b"second build"))
        self.assertTrue(rel["body"].startswith("Matched V8 notes"))
        ledger = guard.extract_ledger(rel["body"])
        self.assertEqual(len(ledger), 1)
        self.assertIn(sha(b"first build"), ledger[0])
        self.assertIn(sha(b"second build"), ledger[0])
        self.assertIn("rebuilt after a toolchain fix", ledger[0])
        self.assertIn("Release asset replaced", (self.tmp / "summary.md").read_text())

    def test_replacement_that_does_not_land_fails_and_records_nothing(self):
        # gh can report success while the release still serves the old bytes
        # (a stale upload, a race with another writer).  The guard must re-read
        # the release and refuse to record a replacement it cannot see.
        name = "a.zip"
        fake = FakeRelease(self.tmp, {name: b"old"}, body="notes\n")
        result = fake.run("publish", self.local(name, b"new"), "--allow-replace", name,
                          "--reason", "r", FAKE_GH_UPLOAD_DOES_NOT_LAND="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("expected sha256:" + sha(b"new"), result.stderr)
        self.assertEqual(len(fake.uploads()), 1)
        reads = [c for c in fake.state["calls"] if c[0] == "api" and "/releases/tags/" in c[1]]
        self.assertEqual(len(reads), 2, "the release must be re-read after the replacement")
        self.assertEqual(guard.extract_ledger(fake.state["release"]["body"]), [])

    def test_override_requires_reason(self):
        name = "a.zip"
        fake = FakeRelease(self.tmp, {name: b"old"})
        result = fake.run("publish", self.local(name, b"new"), "--allow-replace", name)
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires --reason", result.stderr)
        self.assertEqual(fake.uploads(), [])

    def test_override_must_name_a_published_file(self):
        fake = FakeRelease(self.tmp, {"a.zip": b"old"})
        result = fake.run("publish", self.local("a.zip", b"new"),
                          "--allow-replace", "typo.zip", "--reason", "r")
        self.assertEqual(result.returncode, 1)
        self.assertIn("typo.zip", result.stderr)
        self.assertEqual(fake.uploads(), [])

    def test_override_only_covers_the_named_asset(self):
        fake = FakeRelease(self.tmp, {"a.zip": b"a-old", "b.zip": b"b-old"})
        result = fake.run("publish", self.local("a.zip", b"a-new"), self.local("b.zip", b"b-new"),
                          "--allow-replace", "a.zip", "--reason", "r")
        self.assertEqual(result.returncode, 1)
        self.assertIn("b.zip", result.stderr)
        self.assertEqual(fake.uploads(), [])

    def test_dry_run_never_uploads(self):
        fake = FakeRelease(self.tmp, {"a.zip": b"old"})
        result = fake.run("publish", self.local("a.zip", b"new"), self.local("n.zip", b"n"),
                          "--allow-replace", "a.zip", "--reason", "r", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("replace", result.stdout)
        self.assertEqual(fake.uploads(), [])

    def test_missing_release_refused_for_backfill(self):
        fake = FakeRelease(self.tmp, None)
        result = fake.run("publish", self.local("a.zip", b"x"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)

    def test_missing_release_ok_with_defer_new_is_a_noop(self):
        fake = FakeRelease(self.tmp, None)
        result = fake.run("publish", self.local("a.zip", b"x"), "--missing-release-ok", "--defer-new")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fake.uploads(), [])

    def test_asset_without_recorded_digest_is_hashed(self):
        fake = FakeRelease(self.tmp, {"a.zip": b"old"}, record_digests=False)
        result = fake.run("publish", self.local("a.zip", b"old"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skip-identical", result.stdout)
        fake2_dir = self.tmp / "second"
        fake2_dir.mkdir()
        fake2 = FakeRelease(fake2_dir, {"a.zip": b"old"}, record_digests=False)
        result = fake2.run("publish", self.local("a.zip", b"changed"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(fake2.uploads(), [])

    def test_carry_ledger_survives_regenerated_notes(self):
        entry = "- 2026-10-11T22:53:45Z `a.zip` replaced: sha256 `1` -> `2`. Reason: r"
        body = guard.with_ledger("old notes\n", [entry])
        fake = FakeRelease(self.tmp, {"a.zip": b"x"}, body=body)
        notes = self.tmp / "release-notes.md"
        notes.write_text("fresh notes\n")
        result = fake.run("carry-ledger", str(notes))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = notes.read_text()
        self.assertTrue(text.startswith("fresh notes"))
        self.assertEqual(guard.extract_ledger(text), [entry])


class LedgerFormatting(unittest.TestCase):
    def test_with_ledger_is_idempotent_and_appends(self):
        once = guard.with_ledger("notes", ["- one"])
        self.assertEqual(guard.with_ledger(once, ["- one"]), once)
        twice = guard.with_ledger(once, guard.extract_ledger(once) + ["- two"])
        self.assertEqual(guard.extract_ledger(twice), ["- one", "- two"])
        self.assertEqual(twice.count(guard.LEDGER_START), 1)


class WorkflowsUseTheGuard(unittest.TestCase):
    """No workflow may put bytes on a release except through the guard."""

    def test_no_clobber_and_no_raw_release_upload(self):
        workflows = HERE.parent / ".github" / "workflows"
        offenders = []
        for path in sorted(workflows.glob("*.yml")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if "--clobber" in code or "release upload" in code:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "use tools/release_asset_guard.py instead")

    def test_action_gh_release_never_overwrites(self):
        # A text scan rather than a YAML parse keeps this test dependency-free:
        # each action-gh-release step must set overwrite_files: false before
        # the next step starts.
        workflows = HERE.parent / ".github" / "workflows"
        for path in sorted(workflows.glob("*.yml")):
            lines = path.read_text().splitlines()
            for number, line in enumerate(lines):
                if "uses: softprops/action-gh-release" not in line.split("#", 1)[0]:
                    continue
                step = []
                for following in lines[number + 1:]:
                    if following.lstrip().startswith("- "):
                        break
                    step.append(following.split("#", 1)[0].strip())
                with self.subTest(workflow=path.name, line=number + 1):
                    self.assertIn("overwrite_files: false", step)

class ReplaceAssetsSpec(unittest.TestCase):
    """The workflow's replace_assets input is parsed by the guard, not shell."""

    NAMES = ["v8-mac-arm64-x.zip", "v8-win-x64-x.zip", "release-metadata.json"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.fake = FakeRelease(self.tmp, {name: b"old " + name.encode() for name in self.NAMES})
        self.files = []
        for name in self.NAMES:
            path = self.out / name
            path.write_bytes(b"new " + name.encode())
            self.files.append(str(path))

    def tearDown(self):
        self._tmp.cleanup()

    def publish(self, spec, reason="toolchain fix"):
        return self.fake.run("publish", *self.files, "--replace-assets", spec, "--reason", reason)

    def test_all_with_the_right_count_replaces_every_asset_and_names_them(self):
        result = self.publish("ALL:3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("3 asset(s) authorized for replacement", result.stdout)
        for name in self.NAMES:
            self.assertIn(f"  {name}\n", result.stdout)
        self.assertEqual(len(self.fake.uploads()), 3)
        self.assertEqual(len(guard.extract_ledger(self.fake.state["release"]["body"])), 3)

    def test_bare_all_is_refused(self):
        for spec in ("ALL", "all", "ALL:", "ALL:x", "ALLx"):
            with self.subTest(spec=spec):
                result = self.publish(spec)
                self.assertEqual(result.returncode, 1)
                self.assertIn("ALL:<count>", result.stderr)
                self.assertIn("publishes 3 assets", result.stderr)
        self.assertEqual(self.fake.uploads(), [])

    def test_all_with_a_wrong_count_is_refused_and_lists_the_assets(self):
        result = self.publish("ALL:9")
        self.assertEqual(result.returncode, 1)
        for name in self.NAMES:
            self.assertIn(name, result.stderr)
        self.assertEqual(self.fake.uploads(), [])

    def test_named_assets_only_cover_those_assets(self):
        result = self.publish(" v8-win-x64-x.zip , release-metadata.json ")
        self.assertEqual(result.returncode, 1, "the unnamed mac zip still differs")
        self.assertIn("v8-mac-arm64-x.zip", result.stderr)
        self.assertEqual(self.fake.uploads(), [])
        result = self.fake.run("publish", self.files[1], self.files[2], "--replace-assets",
                               "v8-win-x64-x.zip,release-metadata.json", "--reason", "r")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.fake.uploads()), 2)

    def test_a_replacement_needs_a_reason(self):
        result = self.publish("ALL:3", reason="   ")
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires --reason", result.stderr)
        self.assertEqual(self.fake.uploads(), [])

    def test_empty_spec_authorizes_nothing(self):
        result = self.publish("", reason="")
        self.assertEqual(result.returncode, 1)
        self.assertIn("REFUSED", result.stderr)
        self.assertEqual(self.fake.uploads(), [])

    def test_workflow_hands_the_raw_input_to_the_guard(self):
        workflow = (HERE.parent / ".github" / "workflows" / "build-v8.yml").read_text()
        self.assertIn('--replace-assets "$REPLACE_ASSETS"', workflow)
        self.assertNotIn("--allow-replace", workflow)


if __name__ == "__main__":
    unittest.main()
