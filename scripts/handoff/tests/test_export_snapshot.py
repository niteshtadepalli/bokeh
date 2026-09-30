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
"""Tests for scripts/handoff/export_snapshot.py."""

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "export_snapshot.py")
_spec = importlib.util.spec_from_file_location("export_snapshot", _SCRIPT)
export_snapshot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_snapshot)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Source Dev",
    "GIT_AUTHOR_EMAIL": "source-dev@example.com",
    "GIT_COMMITTER_NAME": "Source Dev",
    "GIT_COMMITTER_EMAIL": "source-dev@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def run_git(repo, *args):
  env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
  env.update(_GIT_ENV)
  return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


class GlobAndConfigTest(unittest.TestCase):

  def test_glob_segments(self):
    self.assertTrue(export_snapshot.glob_to_regex("docs/*.md").match("docs/a.md"))
    self.assertFalse(export_snapshot.glob_to_regex("docs/*.md").match("docs/sub/a.md"))
    self.assertTrue(export_snapshot.glob_to_regex("docs/**").match("docs/sub/a.md"))
    self.assertTrue(export_snapshot.glob_to_regex("**/*.tfvars").match("x.tfvars"))
    self.assertTrue(export_snapshot.glob_to_regex("**/*.tfvars").match("a/b/x.tfvars"))
    self.assertFalse(export_snapshot.glob_to_regex("**/*.tfvars").match("a/x.tfvars.example"))

  def test_allowlist_last_match_wins(self):
    allow = export_snapshot.AllowList.parse(["terraform/**", "!**/*.tfvars", "terraform/keep.tfvars"])
    self.assertTrue(allow.matches("terraform/main.tf"))
    self.assertFalse(allow.matches("terraform/prod.tfvars"))
    self.assertTrue(allow.matches("terraform/keep.tfvars"))
    self.assertFalse(allow.matches("README.md"))

  def test_parse_renames(self):
    self.assertEqual(export_snapshot.parse_renames(["a.md -> b.md"]), {"a.md": "b.md"})
    with self.assertRaises(export_snapshot.ExportError):
      export_snapshot.parse_renames(["a.md b.md"])
    with self.assertRaises(export_snapshot.ExportError):
      export_snapshot.parse_renames(["a.md -> b.md", "a.md -> c.md"])

  def test_banned_patterns_round_trip(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = os.path.join(tmp, "banned.b64")
      with open(path, "w") as f:
        f.write("# comment\n" + export_snapshot.encode_pattern("acme-[0-9]+") + "\n")
      patterns = export_snapshot.load_banned_patterns([path])
      self.assertTrue(patterns[0].search("see ACME-42 here"))
      self.assertEqual(open(path).read().count("acme"), 0)
      empty = os.path.join(tmp, "empty.b64")
      open(empty, "w").close()
      with self.assertRaises(export_snapshot.ExportError):
        export_snapshot.load_banned_patterns([empty])

  def test_committed_banned_list_decodes(self):
    patterns = export_snapshot.load_banned_patterns([os.path.join(export_snapshot.HERE, "banned_patterns.b64")])
    self.assertGreater(len(patterns), 0)


class RewriteLinksTest(unittest.TestCase):

  def test_follows_renames_and_new_location(self):
    dest_of = {
        "docs/customer/README.md": "README.md",
        "docs/customer/ops.md": "docs/ops.md",
        "docs/guides/guide.md": "docs/guides/guide.md",
        "terraform/gcp/repos.example.yaml": "terraform/gcp/repos.example.yaml",
    }
    dirs = {".", "docs", "docs/guides", "terraform", "terraform/gcp"}
    text = (
        "[ops](ops.md#keys) [guide](../guides/guide.md) [yaml](../../terraform/gcp/repos.example.yaml)\n"
        "[dir](../../terraform/gcp/) [self](#top) [web](https://example.com/x.md) [wrapped\ntext](ops.md)\n"
    )
    new, problems = export_snapshot.rewrite_links(text, "docs/customer/README.md", "README.md", dest_of, dirs)
    self.assertEqual(problems, [])
    self.assertEqual(
        new,
        "[ops](docs/ops.md#keys) [guide](docs/guides/guide.md) [yaml](terraform/gcp/repos.example.yaml)\n"
        "[dir](terraform/gcp/) [self](#top) [web](https://example.com/x.md) [wrapped\ntext](docs/ops.md)\n",
    )

  def test_link_into_moved_file_from_unmoved_file(self):
    dest_of = {"docs/customer/ops.md": "docs/ops.md", "docs/guides/guide.md": "docs/guides/guide.md"}
    new, problems = export_snapshot.rewrite_links(
        "[x](../customer/ops.md#a)", "docs/guides/guide.md", "docs/guides/guide.md", dest_of, {".", "docs", "docs/guides"}
    )
    self.assertEqual((new, problems), ("[x](../ops.md#a)", []))

  def test_link_to_unexported_file_is_a_problem_even_if_the_name_is_reused(self):
    # README.md is not exported from the source; another file lands there.
    dest_of = {"docs/customer/README.md": "README.md", "docs/ops.md": "docs/ops.md"}
    _, problems = export_snapshot.rewrite_links("[up](../README.md)", "docs/ops.md", "docs/ops.md", dest_of, {".", "docs"})
    self.assertEqual(len(problems), 1)
    self.assertIn("links to README.md, which is not exported", problems[0])


class EndToEndTest(unittest.TestCase):

  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self._tmp.cleanup)
    self.tmp = self._tmp.name
    self.src = os.path.join(self.tmp, "src")
    self.cfg = os.path.join(self.tmp, "cfg")
    self.target = os.path.join(self.tmp, "handoff")
    os.makedirs(self.src)
    os.makedirs(self.cfg)
    run_git(self.src, "init", "-q", "-b", "main")
    self.write_src("README.md", "# Upstream readme\n")
    self.write_src("docs/customer/README.md", "# Product\nSee [ops](ops.md) and [code](../../app/main.py).\n")
    self.write_src("docs/customer/ops.md", "# Ops\n[home](README.md#product)\n")
    self.write_src("docs/internal.md", "internal notes\n")
    self.write_src("app/main.py", "print('hi')\n")
    self.write_src("app/run.sh", "#!/bin/sh\necho run\n", mode=0o755)
    self.write_src("app/prod.tfvars", "secret = 1\n")
    self.commit_src("first")
    self.write_cfg("allowlist.txt", "app/**\n!**/*.tfvars\n")
    self.write_cfg("renames.txt", "docs/customer/README.md -> README.md\ndocs/customer/ops.md -> docs/ops.md\n")
    self.write_cfg("banned_patterns.b64", export_snapshot.encode_pattern("acme-corp") + "\n")

  def write_src(self, rel, text, mode=0o644):
    path = os.path.join(self.src, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
      f.write(text)
    os.chmod(path, mode)

  def write_cfg(self, name, text):
    with open(os.path.join(self.cfg, name), "w") as f:
      f.write(text)

  def commit_src(self, message):
    run_git(self.src, "add", "-A")
    run_git(self.src, "commit", "-q", "-m", message)

  def export(self, *extra):
    args = ["--source-repo", self.src, "--config-dir", self.cfg, *extra]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
      code = export_snapshot.main(args)
    return code, out.getvalue(), err.getvalue()

  def commit_args(self, message="Snapshot one"):
    return ["--target", self.target, "--author-name", "Handoff Bot", "--author-email", "bot@example.com", "--message", message]

  def test_dry_run_builds_only_the_allowlist(self):
    out_dir = os.path.join(self.tmp, "out")
    code, out, err = self.export("--dry-run", "--out", out_dir)
    self.assertEqual(code, 0, err)
    files = sorted(
        os.path.relpath(os.path.join(d, f), out_dir) for d, _, fs in os.walk(out_dir) for f in fs
    )
    self.assertEqual(files, ["README.md", "app/main.py", "app/run.sh", "docs/ops.md"])
    with open(os.path.join(out_dir, "README.md")) as f:
      self.assertEqual(f.read(), "# Product\nSee [ops](docs/ops.md) and [code](app/main.py).\n")
    with open(os.path.join(out_dir, "docs/ops.md")) as f:
      self.assertEqual(f.read(), "# Ops\n[home](../README.md#product)\n")
    self.assertTrue(os.access(os.path.join(out_dir, "app/run.sh"), os.X_OK))
    self.assertIn("Checks passed", out)
    self.assertFalse(os.path.exists(self.target))

  def test_banned_string_fails_and_commits_nothing(self):
    self.write_src("app/main.py", "print('ACME-Corp inside')\n")
    self.commit_src("leak")
    code, _, err = self.export(*self.commit_args())
    self.assertEqual(code, 1)
    self.assertIn("app/main.py:1: matches banned pattern #1", err)
    self.assertNotIn("ACME", err)
    self.assertFalse(os.path.exists(self.target))

  def test_banned_string_in_commit_message_or_path_fails(self):
    code, _, err = self.export(*self.commit_args(message="Snapshot for acme-corp"))
    self.assertEqual(code, 1)
    self.assertIn("(commit message):1", err)
    self.write_src("app/acme-corp.txt", "x\n")
    self.commit_src("path")
    code, _, err = self.export("--dry-run")
    self.assertEqual(code, 1)
    self.assertIn("(path) app/acme-corp.txt", err)

  def test_broken_link_fails(self):
    self.write_src("docs/customer/ops.md", "# Ops\n[internal](../internal.md)\n")
    self.commit_src("bad link")
    code, _, err = self.export("--dry-run")
    self.assertEqual(code, 1)
    self.assertIn("links to docs/internal.md, which is not exported", err)

  def test_uncommitted_changes_are_not_exported(self):
    self.write_src("app/main.py", "print('acme-corp, uncommitted')\n")
    code, _, err = self.export("--dry-run")
    self.assertEqual(code, 0, err)

  def test_snapshots_chain_without_source_history(self):
    code, out, err = self.export(*self.commit_args("Snapshot one"))
    self.assertEqual(code, 0, err)
    first = run_git(self.target, "rev-parse", "main")
    self.assertEqual(run_git(self.target, "rev-list", "--count", "main"), "1")
    self.assertEqual(run_git(self.target, "log", "-1", "--format=%P", "main"), "")
    self.assertEqual(run_git(self.target, "log", "-1", "--format=%an <%ae>|%cn <%ce>", "main"),
                     "Handoff Bot <bot@example.com>|Handoff Bot <bot@example.com>")
    self.assertEqual(
        run_git(self.target, "ls-tree", "-r", "--name-only", "main").split("\n"),
        ["README.md", "app/main.py", "app/run.sh", "docs/ops.md"],
    )
    self.assertIn("100755 blob", run_git(self.target, "ls-tree", "main", "app/run.sh"))
    # The work tree of a fresh target follows the snapshot.
    with open(os.path.join(self.target, "README.md")) as f:
      self.assertIn("# Product", f.read())

    # Unchanged source: nothing is committed.
    code, out, _ = self.export(*self.commit_args("Snapshot again"))
    self.assertEqual(code, 0)
    self.assertIn("No changes since the previous snapshot", out)
    self.assertEqual(run_git(self.target, "rev-parse", "main"), first)

    # A new source commit gives a second snapshot whose parent is the first.
    self.write_src("app/main.py", "print('v2')\n")
    self.commit_src("second")
    code, _, err = self.export(*self.commit_args("Snapshot two"))
    self.assertEqual(code, 0, err)
    self.assertEqual(run_git(self.target, "rev-list", "--count", "main"), "2")
    self.assertEqual(run_git(self.target, "log", "-1", "--format=%P", "main"), first)
    self.assertEqual(run_git(self.target, "log", "--format=%s", "main").split("\n"), ["Snapshot two", "Snapshot one"])
    with open(os.path.join(self.target, "app/main.py")) as f:
      self.assertEqual(f.read(), "print('v2')\n")
    # None of the source commits reached the target.
    for sha in run_git(self.src, "rev-list", "--all").split("\n"):
      self.assertNotEqual(
          subprocess.run(["git", "-C", self.target, "cat-file", "-e", sha], capture_output=True).returncode, 0
      )

  def test_dirty_target_work_tree_is_refused(self):
    self.assertEqual(self.export(*self.commit_args())[0], 0)
    with open(os.path.join(self.target, "local.txt"), "w") as f:
      f.write("local change\n")
    self.write_src("app/main.py", "print('v2')\n")
    self.commit_src("second")
    before = run_git(self.target, "rev-parse", "main")
    code, _, err = self.export(*self.commit_args("Snapshot two"))
    self.assertEqual(code, 1)
    self.assertIn("has local changes", err)
    self.assertEqual(run_git(self.target, "rev-parse", "main"), before)

  def test_bare_target(self):
    run_git(self.tmp, "init", "-q", "--bare", "-b", "main", self.target)
    code, _, err = self.export(*self.commit_args())
    self.assertEqual(code, 0, err)
    self.assertEqual(run_git(self.target, "rev-list", "--count", "main"), "1")

  def test_target_inside_another_repository_gets_its_own_repository(self):
    nested = os.path.join(self.src, "nested-handoff")
    code, _, err = self.export("--target", nested, "--author-name", "Handoff Bot", "--author-email", "bot@example.com")
    self.assertEqual(code, 0, err)
    self.assertTrue(os.path.isdir(os.path.join(nested, ".git")))
    self.assertEqual(run_git(self.src, "log", "--format=%s", "main"), "first")

  def test_non_empty_non_repository_target_is_refused(self):
    os.makedirs(self.target)
    with open(os.path.join(self.target, "stray.txt"), "w") as f:
      f.write("x\n")
    code, _, err = self.export(*self.commit_args())
    self.assertEqual(code, 1)
    self.assertIn("neither a git repository nor empty", err)

  def test_target_needs_an_identity(self):
    code, _, err = self.export("--target", self.target)
    self.assertEqual(code, 2)
    self.assertIn("--author-name", err)

  def test_never_exports_tfvars_even_if_renamed(self):
    self.write_cfg("renames.txt", "app/prod.tfvars -> app/prod.txt\n")
    code, _, err = self.export("--dry-run")
    self.assertEqual(code, 1)
    self.assertIn("never exported", err)

  def test_never_exports_json_tfvars_or_plans_even_if_allowed(self):
    self.write_src("app/prod.auto.tfvars.json", "{\"secret\": 1}\n")
    self.write_src("app/saved.tfplan", "plan\n")
    self.commit_src("more secrets")
    out_dir = os.path.join(self.tmp, "out")
    code, out, err = self.export("--dry-run", "--out", out_dir)
    self.assertEqual(code, 0, err)
    self.assertNotIn("tfvars.json", out)
    self.assertNotIn("tfplan", out)
    self.assertFalse(os.path.exists(os.path.join(out_dir, "app", "saved.tfplan")))

  def test_target_with_history_on_another_branch_is_refused(self):
    os.makedirs(self.target)
    run_git(self.target, "init", "-q", "-b", "trunk")
    with open(os.path.join(self.target, "old.txt"), "w") as f:
      f.write("previous snapshot\n")
    run_git(self.target, "add", "-A")
    run_git(self.target, "commit", "-q", "-m", "old snapshot")
    code, _, err = self.export(*self.commit_args())
    self.assertEqual(code, 1)
    self.assertIn("has history but no branch 'main'", err)
    self.assertNotEqual(
        subprocess.run(["git", "-C", self.target, "rev-parse", "-q", "--verify", "refs/heads/main"],
                       capture_output=True).returncode, 0)
    # With the right branch, the new snapshot chains onto the old one.
    old = run_git(self.target, "rev-parse", "trunk")
    code, _, err = self.export(*self.commit_args(), "--branch", "trunk")
    self.assertEqual(code, 0, err)
    self.assertEqual(run_git(self.target, "log", "-1", "--format=%P", "trunk"), old)


class RealConfigTest(unittest.TestCase):

  def test_live_deployment_config_is_never_exported(self):
    allow = export_snapshot.AllowList.parse(
        export_snapshot._config_lines(os.path.join(export_snapshot.HERE, "allowlist.txt")))
    self.assertTrue(allow.matches("terraform/gcp/repos.example.yaml"))
    self.assertTrue(allow.matches("terraform/gcp/deployment.example.yaml"))
    self.assertFalse(allow.matches("terraform/gcp/repos.yaml"))
    self.assertFalse(allow.matches("terraform/gcp/deployment.yaml"))
    self.assertFalse(allow.matches("terraform/gcp/terraform.tfvars.json"))


if __name__ == "__main__":
  unittest.main()
