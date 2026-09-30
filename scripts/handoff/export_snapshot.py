#!/usr/bin/env python3
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
"""Builds the customer snapshot of this repository.

The snapshot contains only the files that allowlist.txt selects, read from a
committed ref (never the working tree), with the renames in renames.txt
applied and the relative Markdown links rewritten to match. Two checks must
pass before anything is written to the target: every relative Markdown link
resolves inside the snapshot, and no file, path or commit message matches a
banned pattern (banned_patterns.b64).

The snapshot is committed to --target as one commit on --branch. If the
branch already exists, the new commit's parent is its tip (the previous
snapshot), so the recipient can merge successive snapshots. Otherwise the
commit has no parent. The source repository's history is never copied.

Examples:
  # Build and check only; list the files.
  export_snapshot.py --ref origin/main --dry-run --out /tmp/snapshot

  # Commit a snapshot to a local handoff repository.
  export_snapshot.py --ref origin/main --target ~/handoff \
      --author-name "Example Team" --author-email team@example.com
"""

import argparse
import base64
import dataclasses
import datetime
import importlib.util
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Never exported, whatever the allow-list says.
_ALWAYS_DENY = [
    re.compile(r"(^|/)[^/]*\.tfvars$"),
    re.compile(r"(^|/)[^/]*\.tfstate($|\.)"),
    re.compile(r"(^|/)\.terraform/"),
    re.compile(r"(^|/)gcs_backend_override\.tf$"),
]


def _load_link_checker():
  path = os.path.join(HERE, "..", "ci", "check_md_links.py")
  spec = importlib.util.spec_from_file_location("check_md_links", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


check_md_links = _load_link_checker()


class ExportError(Exception):
  """The snapshot could not be built or failed a check."""


# --------------------------------------------------------------------------
# Configuration files
# --------------------------------------------------------------------------


def _config_lines(path: str) -> List[str]:
  with open(path, encoding="utf-8") as f:
    return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]


def glob_to_regex(pattern: str) -> re.Pattern:
  """Compiles a path glob: '*' and '?' stay within one path segment, '**'
  matches any number of segments. Patterns match the whole path from the
  repository root."""
  out = []
  i = 0
  while i < len(pattern):
    if pattern.startswith("**/", i):
      out.append("(?:.*/)?")
      i += 3
    elif pattern.startswith("**", i):
      out.append(".*")
      i += 2
    elif pattern[i] == "*":
      out.append("[^/]*")
      i += 1
    elif pattern[i] == "?":
      out.append("[^/]")
      i += 1
    else:
      out.append(re.escape(pattern[i]))
      i += 1
  return re.compile("^" + "".join(out) + "$")


@dataclasses.dataclass
class AllowList:
  """gitignore-style include list: the last matching line wins, '!' excludes."""

  rules: List[Tuple[bool, re.Pattern]]

  @classmethod
  def parse(cls, lines: Iterable[str]) -> "AllowList":
    rules = []
    for line in lines:
      include = not line.startswith("!")
      rules.append((include, glob_to_regex(line if include else line[1:])))
    return cls(rules)

  def matches(self, path: str) -> bool:
    result = False
    for include, regex in self.rules:
      if regex.match(path):
        result = include
    return result


def parse_renames(lines: Iterable[str]) -> Dict[str, str]:
  renames: Dict[str, str] = {}
  for line in lines:
    src, sep, dest = line.partition("->")
    src, dest = src.strip(), dest.strip()
    if not sep or not src or not dest:
      raise ExportError(f"renames: expected 'source -> destination', got {line!r}")
    if src in renames:
      raise ExportError(f"renames: {src} listed twice")
    renames[src] = dest
  return renames


def encode_pattern(pattern: str) -> str:
  re.compile(pattern)
  return base64.b64encode(pattern.encode("utf-8")).decode("ascii")


def load_banned_patterns(paths: Sequence[str], plain_paths: Sequence[str] = ()) -> List[re.Pattern]:
  """Loads case-insensitive regexes: base64-encoded (one per line) from
  paths, and plain text from plain_paths.

  The committed list is encoded so that the banned strings themselves never
  appear in the repository in plain text.
  """
  patterns = []
  for path in paths:
    for line in _config_lines(path):
      try:
        patterns.append(re.compile(base64.b64decode(line, validate=True).decode("utf-8"), re.IGNORECASE))
      except (ValueError, re.error) as e:
        raise ExportError(f"{path}: bad banned pattern line {line!r}: {e}") from e
  for path in plain_paths:
    patterns.extend(re.compile(line, re.IGNORECASE) for line in _config_lines(path))
  if not patterns:
    raise ExportError("no banned patterns loaded; refusing to export without the check")
  return patterns


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------


def git(repo: str, *args: str, input_bytes: Optional[bytes] = None, env: Optional[Dict[str, str]] = None) -> bytes:
  cmd = ["git", "-C", repo, *args]
  result = subprocess.run(cmd, input=input_bytes, capture_output=True, env=env)
  if result.returncode != 0:
    raise ExportError(f"{' '.join(cmd)} failed: {result.stderr.decode(errors='replace').strip()}")
  return result.stdout


@dataclasses.dataclass(frozen=True)
class TreeEntry:
  mode: str
  kind: str
  sha: str
  path: str


def list_tree(repo: str, ref: str) -> List[TreeEntry]:
  out = git(repo, "ls-tree", "-r", "-z", "--full-tree", ref)
  entries = []
  for record in out.split(b"\0"):
    if not record:
      continue
    meta, path = record.split(b"\t", 1)
    mode, kind, sha = meta.decode().split(" ")
    entries.append(TreeEntry(mode, kind, sha, path.decode("utf-8")))
  return entries


# --------------------------------------------------------------------------
# Building the snapshot
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Snapshot:
  staging: str
  files: Dict[str, str]  # destination path -> mode
  source_of: Dict[str, str]  # destination path -> source path


def select_files(
    entries: Sequence[TreeEntry], allow: AllowList, renames: Dict[str, str]
) -> Dict[str, TreeEntry]:
  """Returns destination path -> source entry."""
  by_path = {e.path: e for e in entries}
  missing = sorted(src for src in renames if src not in by_path)
  if missing:
    raise ExportError(f"renames: source file(s) not in the ref: {', '.join(missing)}")
  selected: Dict[str, TreeEntry] = {}
  for entry in entries:
    if entry.path not in renames and not allow.matches(entry.path):
      continue
    dest = renames.get(entry.path, entry.path)
    for path in {entry.path, dest}:
      if any(r.search(path) for r in _ALWAYS_DENY):
        if entry.path in renames:
          raise ExportError(f"{path} is never exported (secrets or Terraform state)")
        break
    else:
      if entry.kind != "blob" or entry.mode not in ("100644", "100755"):
        raise ExportError(f"{entry.path}: only regular files can be exported (mode {entry.mode}, {entry.kind})")
      if dest in selected:
        raise ExportError(f"{dest}: exported from both {selected[dest].path} and {entry.path}")
      selected[dest] = entry
  if not selected:
    raise ExportError("the allow-list selects no files")
  return selected


def rewrite_links(
    text: str, src_path: str, dest_path: str, dest_of: Dict[str, str], dest_dirs: Set[str]
) -> Tuple[str, List[str]]:
  """Rewrites relative links in one Markdown file for its new location.

  Each link is resolved against the file's source path, mapped to where the
  target lands in the snapshot, and made relative to dest_path. Returns the
  new text and a list of problems (links to files that are not exported).
  """
  problems: List[str] = []
  for link in sorted(check_md_links.iter_links(text), key=lambda l: l.start, reverse=True):
    target = link.target
    if not target or check_md_links.is_external(target) or target.startswith("#"):
      continue
    path_part, _, fragment = target.partition("#")
    if path_part.startswith("/") or not path_part:
      continue  # absolute paths are reported by the link check
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(src_path), path_part))
    if resolved in dest_of:
      new_target = dest_of[resolved]
    elif resolved in dest_dirs:
      new_target = resolved
    else:
      problems.append(f"{src_path}:{link.line}: {target}: links to {resolved}, which is not exported")
      continue
    rel = posixpath.relpath(new_target, posixpath.dirname(dest_path) or ".")
    if path_part.endswith("/") and not rel.endswith("/"):
      rel += "/"
    new = rel + (f"#{fragment}" if fragment else "")
    if new != target:
      text = text[: link.start] + new + text[link.end :]
  return text, problems


def build_snapshot(
    source_repo: str, ref: str, allow: AllowList, renames: Dict[str, str], staging: str
) -> Snapshot:
  entries = list_tree(source_repo, ref)
  selected = select_files(entries, allow, renames)
  dest_of = {e.path: dest for dest, e in selected.items()}
  dest_dirs = {"."}
  for dest in selected:
    parts = dest.split("/")[:-1]
    for i in range(1, len(parts) + 1):
      dest_dirs.add("/".join(parts[:i]))
  problems: List[str] = []
  files: Dict[str, str] = {}
  for dest, entry in sorted(selected.items()):
    data = git(source_repo, "cat-file", "blob", entry.sha)
    if dest.endswith(".md"):
      text, found = rewrite_links(data.decode("utf-8"), entry.path, dest, dest_of, dest_dirs)
      problems.extend(found)
      data = text.encode("utf-8")
    out = os.path.join(staging, *dest.split("/"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
      f.write(data)
    os.chmod(out, 0o755 if entry.mode == "100755" else 0o644)
    files[dest] = entry.mode
  if problems:
    raise ExportError("links to files outside the snapshot:\n  " + "\n  ".join(problems))
  return Snapshot(staging=staging, files=files, source_of={d: e.path for d, e in selected.items()})


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def find_banned(patterns: Sequence[re.Pattern], name: str, text: str) -> List[str]:
  hits = []
  for number, line in enumerate(text.splitlines(), start=1):
    for pattern in patterns:
      if pattern.search(line):
        hits.append(f"{name}:{number}: matches banned pattern #{patterns.index(pattern) + 1}")
  return hits


def check_snapshot(snapshot: Snapshot, patterns: Sequence[re.Pattern], extra_texts: Dict[str, str]) -> List[str]:
  """Runs the link and banned-string checks; returns the failures."""
  failures = [str(e) for e in check_md_links.check_tree(snapshot.staging, sorted(p for p in snapshot.files if p.endswith(".md")))]
  for dest in sorted(snapshot.files):
    failures.extend(find_banned(patterns, f"(path) {dest}", dest))
    with open(os.path.join(snapshot.staging, *dest.split("/")), "rb") as f:
      failures.extend(find_banned(patterns, dest, f.read().decode("utf-8", errors="replace")))
  for name, text in extra_texts.items():
    failures.extend(find_banned(patterns, name, text))
  return failures


# --------------------------------------------------------------------------
# Committing
# --------------------------------------------------------------------------


def _clean_git_env(name: str, email: str, date: Optional[str]) -> Dict[str, str]:
  env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
  env.update(
      GIT_AUTHOR_NAME=name,
      GIT_AUTHOR_EMAIL=email,
      GIT_COMMITTER_NAME=name,
      GIT_COMMITTER_EMAIL=email,
  )
  if date:
    env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
  return env


def _open_target(target: str, branch: str) -> Tuple[List[str], bool]:
  """Returns (git arguments that address the target repository, is_bare).

  The target is addressed explicitly (--git-dir), never found by searching
  parent directories, so a directory inside some other repository is not
  mistaken for that repository. A missing or empty directory is initialised
  as a new non-bare repository.
  """
  target = os.path.abspath(target)
  dot_git = os.path.join(target, ".git")
  if os.path.exists(dot_git):
    return ["--git-dir", dot_git, "--work-tree", target], False
  if all(os.path.exists(os.path.join(target, p)) for p in ("HEAD", "objects", "refs")):
    return ["--git-dir", target], True
  if os.path.exists(target) and os.listdir(target):
    raise ExportError(f"{target} is neither a git repository nor empty")
  os.makedirs(target, exist_ok=True)
  subprocess.run(["git", "init", "-q", "--initial-branch", branch, target], check=True, capture_output=True)
  return ["--git-dir", dot_git, "--work-tree", target], False


def _tgit(base: List[str], *args: str, **kwargs) -> bytes:
  return git(".", *base, *args, **kwargs)


def _tgit_ok(base: List[str], *args: str) -> Tuple[bool, str]:
  result = subprocess.run(["git", *base, *args], capture_output=True, text=True)
  return result.returncode == 0, result.stdout.strip()


def commit_snapshot(
    snapshot: Snapshot,
    target: str,
    branch: str,
    message: str,
    author_name: str,
    author_email: str,
    date: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
  """Commits the snapshot to target; returns (new_commit, parent).

  new_commit is None when the snapshot is identical to the previous one.
  """
  ref = f"refs/heads/{branch}"
  if subprocess.run(["git", "check-ref-format", ref], capture_output=True).returncode != 0:
    raise ExportError(f"invalid branch name {branch!r}")
  base, bare = _open_target(target, branch)
  found, parent_sha = _tgit_ok(base, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
  parent = parent_sha if found else None

  _, head = _tgit_ok(base, "symbolic-ref", "-q", "HEAD")
  update_worktree = not bare and head == ref
  if update_worktree:
    status = _tgit(base, "status", "--porcelain", "--untracked-files=all").decode()
    if status.strip():
      raise ExportError(f"{target} has local changes on {branch}; commit, stash or remove them first")

  env = _clean_git_env(author_name, author_email, date)
  with tempfile.TemporaryDirectory() as tmp:
    index_env = dict(env, GIT_INDEX_FILE=os.path.join(tmp, "index"))
    records = []
    for dest in sorted(snapshot.files):
      path = os.path.join(snapshot.staging, *dest.split("/"))
      sha = _tgit(base, "hash-object", "-w", "--no-filters", "--", path, env=env).decode().strip()
      records.append(f"{snapshot.files[dest]} {sha}\t{dest}")
    _tgit(base, "update-index", "--add", "--index-info", input_bytes=("\n".join(records) + "\n").encode("utf-8"), env=index_env)
    tree = _tgit(base, "write-tree", env=index_env).decode().strip()

  if parent and _tgit(base, "rev-parse", f"{parent}^{{tree}}").decode().strip() == tree:
    return None, parent
  args = ["commit-tree", tree]
  if parent:
    args += ["-p", parent]
  commit = _tgit(base, *args, input_bytes=message.encode("utf-8"), env=env).decode().strip()
  if update_worktree:
    _tgit(base, "read-tree", "-u", "--reset", commit)
  _tgit(base, "update-ref", "-m", "snapshot", ref, commit, parent or "0" * 40)
  return commit, parent


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
  p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
  p.add_argument("--source-repo", default=os.path.join(HERE, "..", ".."), help="source repository (default: this one)")
  p.add_argument("--ref", default="HEAD", help="committed ref to export (default: HEAD)")
  p.add_argument("--config-dir", default=HERE, help="directory with allowlist.txt, renames.txt and banned_patterns.b64")
  p.add_argument("--extra-banned", action="append", default=[], help="plain-text file of extra banned regexes (repeatable)")
  mode = p.add_mutually_exclusive_group(required=True)
  mode.add_argument("--dry-run", action="store_true", help="build and check only")
  mode.add_argument("--target", help="repository to commit the snapshot to (created if missing)")
  mode.add_argument("--encode", metavar="REGEX", help="print the banned_patterns.b64 line for REGEX and exit")
  p.add_argument("--out", help="directory to build the snapshot in (must be empty or missing; kept afterwards)")
  p.add_argument("--branch", default="main", help="branch in --target (default: main)")
  p.add_argument("--message", help="commit message (default: 'Snapshot <date>')")
  p.add_argument("--author-name", help="author and committer name for the snapshot commit")
  p.add_argument("--author-email", help="author and committer email for the snapshot commit")
  p.add_argument("--date", help="commit date (git date format; default: now)")
  return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
  args = parse_args(argv)
  if args.encode:
    print(encode_pattern(args.encode))
    return 0
  if args.target and not (args.author_name and args.author_email):
    print("error: --target needs --author-name and --author-email", file=sys.stderr)
    return 2

  message = args.message or f"Snapshot {datetime.date.today().isoformat()}\n"
  if not message.endswith("\n"):
    message += "\n"
  cleanup = None
  try:
    allow = AllowList.parse(_config_lines(os.path.join(args.config_dir, "allowlist.txt")))
    renames = parse_renames(_config_lines(os.path.join(args.config_dir, "renames.txt")))
    patterns = load_banned_patterns([os.path.join(args.config_dir, "banned_patterns.b64")], args.extra_banned)

    if args.out:
      if os.path.exists(args.out) and os.listdir(args.out):
        raise ExportError(f"--out {args.out} is not empty")
      os.makedirs(args.out, exist_ok=True)
      staging = args.out
    else:
      staging = cleanup = tempfile.mkdtemp(prefix="snapshot-")

    source = os.path.abspath(args.source_repo)
    ref_sha = git(source, "rev-parse", "--verify", f"{args.ref}^{{commit}}").decode().strip()
    snapshot = build_snapshot(source, ref_sha, allow, renames, staging)

    extra = {"(commit message)": message}
    if args.target:
      extra["(author)"] = f"{args.author_name} <{args.author_email}>"
    failures = check_snapshot(snapshot, patterns, extra)

    print(f"Snapshot of {args.ref} ({ref_sha[:12]}): {len(snapshot.files)} file(s) in {staging}")
    for dest in sorted(snapshot.files):
      src = snapshot.source_of[dest]
      print(f"  {dest}" + (f"  (from {src})" if src != dest else ""))
    if failures:
      print(f"\nFAILED: {len(failures)} problem(s):", file=sys.stderr)
      for failure in failures:
        print(f"  {failure}", file=sys.stderr)
      return 1
    print("Checks passed: relative links, banned strings.")

    if args.target:
      commit, parent = commit_snapshot(snapshot, args.target, args.branch, message, args.author_name, args.author_email, args.date)
      if commit is None:
        print(f"No changes since the previous snapshot ({parent[:12]}); nothing committed.")
      else:
        print(f"Committed {commit[:12]} on {args.branch} in {args.target} (parent: {parent[:12] if parent else 'none'}).")
    return 0
  except ExportError as e:
    print(f"error: {e}", file=sys.stderr)
    return 1
  finally:
    if cleanup:
      shutil.rmtree(cleanup, ignore_errors=True)


if __name__ == "__main__":
  sys.exit(main())
