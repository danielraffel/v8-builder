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
        self.assertIn("Published assets are immutable", result.stderr)
        self.assertIn("new release tag (or a new asset name)", result.stderr)
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

    def test_override_refuses_changed_digest_without_uploading(self):
        name = "v8-win-arm64-m153-15.3.76.5-26cef0256b0e.zip"
        fake = FakeRelease(self.tmp, {name: b"first build"}, body="Matched V8 notes\n")
        result = fake.run("publish", self.local(name, b"second build"),
                          "--allow-replace", name, "--reason", "toolchain fix")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Published assets are immutable", result.stderr)
        self.assertIn("new release tag (or a new asset name)", result.stderr)
        self.assertEqual(fake.uploads(), [])
        self.assertEqual(fake.state["release"]["assets"][0]["digest"],
                         "sha256:" + sha(b"first build"))

    def test_override_refuses_even_when_digest_is_identical(self):
        fake = FakeRelease(self.tmp, {"a.zip": b"same"})
        result = fake.run("publish", self.local("a.zip", b"same"),
                          "--allow-replace", "a.zip")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Published assets are immutable", result.stderr)
        self.assertEqual(fake.uploads(), [])

    def test_legacy_replace_assets_refuses(self):
        fake = FakeRelease(self.tmp, {"a.zip": b"old"})
        result = fake.run("publish", self.local("a.zip", b"new"),
                          "--replace-assets", "ALL:1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Published assets are immutable", result.stderr)
        self.assertEqual(fake.uploads(), [])

    def test_dry_run_never_uploads(self):
        fake = FakeRelease(self.tmp, {"a.zip": b"old"})
        result = fake.run("publish", self.local("a.zip", b"old"), self.local("n.zip", b"n"),
                          "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skip-identical", result.stdout)
        self.assertIn("upload", result.stdout)
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

    def test_workflow_exposes_no_replacement_inputs_or_args(self):
        workflow = (HERE.parent / ".github" / "workflows" / "build-v8.yml").read_text()
        self.assertNotIn("replace_assets", workflow)
        self.assertNotIn("replace_reason", workflow)
        self.assertNotIn("--allow-replace", workflow)


if __name__ == "__main__":
    unittest.main()
