# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for scripts/ci/tf_*.sh, run with POSIX sh against a stub terraform."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

_CI_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ci"
_SH = shutil.which("sh")

# Records each call (one line per call) in $STUB_DIR/calls. `plan -out=F`
# creates F in the -chdir directory; `show -json` prints $STUB_DIR/plan.json;
# $STUB_DIR/fail_<subcommand> makes that subcommand fail.
_STUB = textwrap.dedent("""\
    #!/bin/sh
    echo "$*" >> "$STUB_DIR/calls"
    dir=.
    case "$1" in -chdir=*) dir="${1#-chdir=}"; shift ;; esac
    [ -f "$STUB_DIR/fail_$1" ] && { echo "terraform $1 failed" >&2; exit 1; }
    case "$1" in
      plan)
        for a in "$@"; do
          case "$a" in -out=*) : > "$dir/${a#-out=}" ;; esac
        done ;;
      show) cat "$STUB_DIR/plan.json" ;;
    esac
    exit 0
    """)


@unittest.skipIf(_SH is None, "sh is required")
class TerraformScriptsTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.root = pathlib.Path(tmp.name)
    self.stub_dir = self.root / "stub"
    bin_dir = self.stub_dir / "bin"
    bin_dir.mkdir(parents=True)
    tf = bin_dir / "terraform"
    tf.write_text(_STUB)
    tf.chmod(0o755)
    # python3 must resolve to this interpreter for the guard step.
    (bin_dir / "python3").symlink_to(sys.executable)
    self.tf_dir = self.root / "stack"
    self.tf_dir.mkdir()
    self.env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "STUB_DIR": str(self.stub_dir),
        "TF_STATE_BUCKET": "state-bucket",
        "TF_STATE_PREFIX": "cm/gcp",
        "TF_DIR": str(self.tf_dir),
    }
    self._plan([])

  def _plan(self, changes):
    (self.stub_dir / "plan.json").write_text(json.dumps({"resource_changes": changes}))

  def _calls(self):
    path = self.stub_dir / "calls"
    return path.read_text().splitlines() if path.exists() else []

  def _run(self, script, *args, **env):
    return subprocess.run(
        [_SH, str(_CI_DIR / script), *args],
        env={**self.env, **env}, cwd=self.root,
        capture_output=True, text=True, timeout=60, check=False,
    )

  def test_init_adds_gcs_backend_and_configures_it(self):
    result = self._run("tf_init.sh")
    self.assertEqual(result.returncode, 0, result.stderr)
    override = (self.tf_dir / "gcs_backend_override.tf").read_text()
    self.assertIn('backend "gcs" {}', override)
    self.assertEqual(self._calls(), [
        f"-chdir={self.tf_dir} init -input=false -reconfigure "
        "-backend-config=bucket=state-bucket -backend-config=prefix=cm/gcp"
    ])

  def test_init_requires_the_bucket(self):
    env = dict(self.env)
    del env["TF_STATE_BUCKET"]
    result = subprocess.run(
        [_SH, str(_CI_DIR / "tf_init.sh")], env=env, cwd=self.root,
        capture_output=True, text=True, timeout=60, check=False,
    )
    self.assertNotEqual(result.returncode, 0)
    self.assertIn("TF_STATE_BUCKET", result.stderr)
    self.assertEqual(self._calls(), [])

  def test_init_rejects_a_missing_directory(self):
    result = self._run("tf_init.sh", TF_DIR=str(self.root / "nope"))
    self.assertEqual(result.returncode, 1)
    self.assertEqual(self._calls(), [])

  def test_plan_is_read_only(self):
    result = self._run("tf_plan.sh")
    self.assertEqual(result.returncode, 0, result.stderr)
    calls = self._calls()
    self.assertEqual(len(calls), 2)
    self.assertIn(" init ", calls[0])
    self.assertIn(" plan ", calls[1])
    self.assertIn("-lock=false", calls[1])
    self.assertNotIn("-out", calls[1])
    self.assertFalse(any(" apply" in c for c in calls))

  def test_failing_plan_fails_the_check(self):
    (self.stub_dir / "fail_plan").write_text("")
    result = self._run("tf_plan.sh")
    self.assertNotEqual(result.returncode, 0)

  def test_apply_all_applies_the_saved_plan(self):
    result = self._run("tf_apply.sh", "all")
    self.assertEqual(result.returncode, 0, result.stderr)
    calls = self._calls()
    self.assertIn("-out=tfplan", calls[1])
    self.assertIn("-lock-timeout=15m", calls[1])
    self.assertIn("show -json tfplan", calls[2])
    self.assertTrue(calls[3].endswith("apply -input=false -no-color -lock-timeout=15m tfplan"))
    self.assertTrue((self.tf_dir / "tfplan.json").exists())

  def test_apply_all_stops_on_protected_deletion(self):
    self._plan([{
        "address": "google_storage_bucket.reports", "mode": "managed",
        "type": "google_storage_bucket", "change": {"actions": ["delete", "create"]},
    }])
    result = self._run("tf_apply.sh", "all")
    self.assertEqual(result.returncode, 1)
    self.assertIn("google_storage_bucket.reports", result.stdout)
    self.assertFalse(any(" apply " in c for c in self._calls()))

  def test_apply_all_with_allow_destroy(self):
    self._plan([{
        "address": "google_storage_bucket.reports", "mode": "managed",
        "type": "google_storage_bucket", "change": {"actions": ["delete"]},
    }])
    result = self._run("tf_apply.sh", "all", ALLOW_DESTROY="true")
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(self._calls()[-1].endswith("tfplan"))

  def test_steps_can_run_separately(self):
    for step in ("plan", "guard", "apply"):
      with self.subTest(step=step):
        result = self._run("tf_apply.sh", step)
        self.assertEqual(result.returncode, 0, result.stderr)
    self.assertTrue(self._calls()[-1].endswith("tfplan"))

  def test_apply_needs_a_saved_plan(self):
    result = self._run("tf_apply.sh", "apply")
    self.assertEqual(result.returncode, 1)
    self.assertIn("No saved plan", result.stderr)
    self.assertEqual(self._calls(), [])

  def test_plan_step_removes_a_stale_plan(self):
    (self.tf_dir / "tfplan").write_text("stale")
    (self.stub_dir / "fail_plan").write_text("")
    result = self._run("tf_apply.sh", "plan")
    self.assertNotEqual(result.returncode, 0)
    self.assertFalse((self.tf_dir / "tfplan").exists())

  def test_unknown_mode(self):
    for args in ((), ("destroy",)):
      with self.subTest(args=args):
        result = self._run("tf_apply.sh", *args)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
  unittest.main()
