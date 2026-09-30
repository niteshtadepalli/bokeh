# Customer snapshot export

`export_snapshot.py` builds the repository snapshot that is handed to the
customer. It is for maintainers of this repository; the directory is not part
of the snapshot.

## What it does

1.  Reads the files at a **committed ref** (`--ref`, default `HEAD`). The
    working tree and uncommitted changes are never used.
2.  Keeps only the files that [`allowlist.txt`](allowlist.txt) selects.
    Anything not listed is left out, so files that upstream syncs add stay out
    of the snapshot until someone adds them. Terraform variable files, state
    and provider caches are never exported, whatever the list says.
3.  Applies [`renames.txt`](renames.txt). The customer README
    (`docs/customer/README.md`) becomes the snapshot's `README.md`, and the
    other customer docs move to `docs/`. Relative Markdown links in every
    exported file are rewritten to follow the moves. A link to a file that is
    not exported fails the export.
4.  Runs two checks on the result:
    *   every relative Markdown link and `#anchor` resolves inside the
        snapshot (`scripts/ci/check_md_links.py`, the same check CI runs);
    *   no file content, file path, commit message or author matches a
        pattern in [`banned_patterns.b64`](banned_patterns.b64).
5.  With `--target`, commits the snapshot to that repository as a single
    commit. If the branch already exists, the new commit's parent is its tip
    (the previous snapshot), so the customer can merge each new snapshot into
    their copy. Otherwise the commit has no parent. Commits from this
    repository are never copied. If nothing changed since the previous
    snapshot, nothing is committed.

## Usage

```bash
# Build and check only, and list the files:
scripts/handoff/export_snapshot.py --ref origin/main --dry-run --out /tmp/snapshot

# Commit a snapshot to a local clone of the handoff repository, then review
# and push it yourself:
scripts/handoff/export_snapshot.py --ref origin/main --target ~/handoff \
    --author-name "Your Team" --author-email your-team@example.com \
    --message "Snapshot 2026-10-01"
git -C ~/handoff log --stat -1
```

`--target` accepts a non-bare clone, a bare repository, or a missing or empty
directory (initialised as a new repository). In a non-bare clone with the
branch checked out, the work tree must be clean; it is updated to the new
snapshot. The script never pushes.

Exit status: 0 on success (including "no changes"), 1 if a check fails or the
export cannot be built, 2 for usage errors. Check failures name the file, line
and the number of the banned pattern, not the matched text.

## Banned patterns

`banned_patterns.b64` holds one case-insensitive Python regex per line,
base64-encoded, so the banned strings themselves never appear in this
repository in plain text. To add one:

```bash
scripts/handoff/export_snapshot.py --encode 'some-name' >> scripts/handoff/banned_patterns.b64
```

To see the list:

```bash
grep -v '^#' scripts/handoff/banned_patterns.b64 | while read -r l; do echo "$l" | base64 -d; echo; done
```

`--extra-banned FILE` adds plain-text patterns from a local file for a single
run (for example, names you do not want committed even in encoded form).

## Tests

```bash
python3 -m pytest -q scripts/handoff
```
