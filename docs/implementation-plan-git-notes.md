# Implementation Plan: Git Notes for Review Artifacts + jsonl.gz Archive

**Status**: Approved (Grill Me design interview, 2026-08-15)
**Scope**: pi-review-precommit — enhance discoverability of review artifacts
**Design source**: Grill Me checkpoint (shared understanding, all branches resolved)

---

## 1. Overview

Three coordinated changes:

1. **Git notes for finished reviews** — a new post-commit stage hook
   (`pi-review-notes`) attaches a human-readable printout of each finished
   (go) review to the commit it approved, under a dedicated
   `refs/notes/pi-review` ref. Every commit gets a note: the review printout
   when a review log matches the commit's tree, otherwise a brief "no review"
   audit note.
2. **Archive format change** — the opt-in session archive becomes a simple
   gzipped jsonl file (`session-<id>.jsonl.gz` in `.git/pi-reviewer/archive/`)
   instead of a tar.gz wrapper. A review session is always just that one file.
3. **Archive link in the note** — the git note includes a
   `Session archive: ./.git/pi-reviewer/archive/session-<id>.jsonl.gz` line
   when archiving is enabled.

## 2. Agreed design summary

| Branch | Decision |
|---|---|
| Timing/mechanics | Post-commit stage hook finds the just-created commit whose tree matches the reviewed tree hash and attaches the note to the real commit |
| Post-commit responsibilities | (a) pack review context into a git note; (b) clean up the consumed review log JSON (archive stays); (c) record "no associated review" for auditing |
| Notes ref | Dedicated `refs/notes/pi-review` — no collision with other note tooling, independently pushable/ignorable. Visibility: `git log --notes=pi-review` or `git config notes.displayRef refs/notes/pi-review` |
| Audit scope | Every commit gets a note; `SKIP=pi-review` skips only the pre-commit review, the audit note still records the skip |
| Note content | `Decision` / `Summary` / `Suggestions` / `Issues` (when present) + conditional `Session archive:` line (only when archiving enabled). No link to the review log JSON (it is cleaned up) |
| Archive format | Session entries (message, tool call, etc.) as JSON lines in order, gzipped; `session-<id>.jsonl.gz` in `.git/pi-reviewer/archive/` (singular). Old `.tar.gz` files in `archives/` left as-is (historical) |
| Cleanup | Post-commit deletes only the review log it consumed, and only after a successful note attach. Orphaned logs stay (small, harmless; a future commit of that tree still gets its note) |
| Config surface | Second hook id `pi-review-notes` (stages [post-commit], always_run, pass_filenames false, require_serial); consumers opt in by listing it. No new flags: ref fixed, audit notes always on, archive link conditional on the recorded path. `SKIP=pi-review-notes` skips it |
| Failure modes | Post-commit exits non-zero when the note attach fails (pre-commit shows the error; the commit already happened, so nothing blocks); review log kept for retry. Cleanup failures log to stderr. Go-time archive failure stays fail-closed (commit blocked, state intact) |
| Validation | Unit tests + dogfooding + disposable scratch-repo E2E (no-match audit notes, amend-after-go, note-attach failure paths) |
| Rollout | Atomic feature commits (a/b/c), push + rev bump after each, then enable `pi-review-notes` in the dogfooding config and run the scratch-repo E2E |

## 3. Commit (a): archive format change

**Goal**: replace the tar.gz session archive with a gzipped jsonl file, and
record the archive path in the review log so the post-commit hook can link it.

### `src/pi_review_precommit/state.py`

- **`archive_sessions(session_dir, session_id) -> Path`** — rewrite:
  - Read the pi session store for `<session_id>` (the session entries:
    messages, tool calls, etc., in order).
  - Write one JSON object per line, gzipped, to
    `Path(session_dir).parent / "archive" / f"session-{session_id}.jsonl.gz"`
    (note: singular `archive/`, not `archives/`).
  - Return the archive path.
  - Drop the `tarfile` dependency for this path (stdlib `gzip` + `json`).
- **`clear_state(session_dir, archive=False, session_id=None)`** — the archive
  step now produces the jsonl.gz in `archive/`; otherwise unchanged (state
  file unlinked last, so a failed archive leaves state intact — existing
  behavior preserved).
- **`record_approval(session_id, tree_hash, round_number, decision_args, archive_path=None)`** —
  add an `archive_path` field to the persisted JSON (None when archiving is
  off). The post-commit hook reads it to emit the `Session archive:` line.

### `src/pi_review_precommit/hook.py` (go path)

- Reorder so the archive path is known before `record_approval`:
  1. If `config.archive_sessions`: `archive_path = archive_sessions(...)`
     (fail-closed on error — existing behavior).
  2. `record_approval(..., archive_path=archive_path)`.
  3. `clear_state(...)` (removes state file + session dir; the archive already
     exists, so no re-archive).

### Tests

- `test_state.py`: archive writer produces a valid gzip jsonl containing the
  session entries in order; naming `session-<id>.jsonl.gz`; location
  `archive/`; `record_approval` persists `archive_path` (present when
  archiving on, null when off); archive-failure ordering test still passes.
- `test_hook.py`: go path with `--archive-sessions` records the archive path
  in the review log; go path without the flag records `archive_path: null`.

## 4. Commit (b): note builder + post-commit hook

**Goal**: the `pi-review-notes` post-commit hook — find the commit by tree
hash, attach the review note (or a "no review" audit note), clean up the
consumed review log.

### New module `src/pi_review_precommit/notes.py`

- **`build_note_text(review_log: dict) -> str`** — the human-readable
  printout, exactly per the agreed content:
  ```
  Decision: go
  Summary: <summary>
  Suggestions:
  - <suggestion>
  Issues:
  - [<severity>] <description> (<file>:<line>)
  Session archive: ./.git/pi-reviewer/archive/session-<id>.jsonl.gz   # only when archive_path present
  ```
  Omit empty sections; the `Session archive:` line only when
  `review_log["archive_path"]` is set.
- **`build_audit_note_text() -> str`** — brief "no review" note, e.g.
  `No pi-review associated with this commit (skipped, bypassed, or tree mismatch).`
- **`find_review_log_for_tree(tree_hash) -> Path | None`** — scan
  `.git/pi-reviewer/reviews/*.json` for a filename containing the tree hash.
- **`attach_note(commit_sha, note_text) -> None`** — run
  `git notes --ref=refs/notes/pi-review add -m <note_text> <commit_sha>`;
  raise on non-zero exit.
- **`main(argv=None) -> int`** — the `pi-review-notes` entry point:
  1. `commit = git rev-parse HEAD`; `tree = git rev-parse HEAD^{tree}`.
  2. `log = find_review_log_for_tree(tree)`.
  3. If found: `attach_note(commit, build_note_text(log))`; on success
     unlink the log (cleanup failure → stderr warning, exit 0); on note
     failure → stderr error, **exit 1**, log kept.
  4. If not found: `attach_note(commit, build_audit_note_text())`; on failure
     → stderr error, **exit 1**.
  5. Any git plumbing failure (not a repo, rev-parse fails) → stderr error,
     exit 1 (visible, non-blocking).

### `pyproject.toml`

- Add console script: `pi-review-notes = "pi_review_precommit.notes:main"`.

### Tests

- `test_notes.py` (new): note text builder (all fields, empty-section
  omission, conditional archive link); audit note text; review-log lookup by
  tree hash; `attach_note` command construction (mock subprocess); `main`
  flow: match → note + log deleted; no match → audit note; note failure →
  exit 1 + log kept; cleanup failure → exit 0 + stderr warning.

## 5. Commit (c): hook id + docs

### `.pre-commit-hooks.yaml`

```yaml
- id: pi-review-notes
  name: pi-review-notes
  description: Attach pi-review notes (review printout or no-review audit) to commits.
  entry: pi-review-notes
  language: python
  pass_filenames: false
  always_run: true
  require_serial: true
  stages: [post-commit]
```

### `README.md`

- New "Git notes" section: what the notes contain, the `refs/notes/pi-review`
  ref, visibility (`git log --notes=pi-review`,
  `git config notes.displayRef refs/notes/pi-review`), pushing the ref
  (`git push origin refs/notes/pi-review`), and the `pi-review-notes` hook id
  (opt-in, `SKIP=pi-review-notes`).
- Update the archiving section: jsonl.gz format, `archive/` dir, migration
  note (old `.tar.gz` files in `archives/` left as-is), `zgrep` inspection.
- Update the config table (no new flags; note the second hook id).

### `AGENTS.md`

- Flow diagram: add the post-commit step (note attach + cleanup + audit).
- Module layout: add `notes.py`.
- Config table: add the `pi-review-notes` hook id row.
- Implementation status: check off the three commits.

## 6. Push + rev-bump cycle

After each commit, in order:

1. `git push` (origin/master).
2. Bump the dogfooding `rev` in `.pre-commit-config.yaml` to the new HEAD.
3. Commit the bump through the dogfood hook, push.

**Chicken-and-egg note** (observed in prior work): the dogfooding config can
only reference published revs, and a new hook id can only be enabled in the
config after its commit is published and the rev bumped. So the enablement
(§7) strictly follows commit (c) + rev bump.

## 7. Dogfooding enablement

After commit (c) is pushed and the rev bumped:

- Add `- id: pi-review-notes` to this repo's `.pre-commit-config.yaml`
  (no args needed — the post-commit hook does not invoke pi).
- Commit the config change through the dogfood hook, push.
- From the next commit onward, every commit in this repo gets a
  `refs/notes/pi-review` note; verify with `git log --notes=pi-review`.

## 8. Scratch-repo E2E scenarios

Disposable repo (e.g. `/tmp/pi-review-notes-e2e`) with the hook repo pinned
at the new rev, both hook ids enabled, `--archive-sessions` on.

1. **Normal review flow**: stage a change → `git commit` → pre-commit review
   (go) → post-commit attaches the review note. Verify: note text contains
   `Decision: go`, `Summary`, and `Session archive: ./.git/pi-reviewer/archive/session-<id>.jsonl.gz`;
   the review log JSON is gone from `reviews/`; the archive is a gzipped jsonl
   (`zgrep` finds the session entries); `git log --notes=pi-review` shows the
   note.
2. **No-match audit note**: `SKIP=pi-review git commit` (or `--no-verify`) →
   post-commit attaches the "no review" audit note; verify via
   `git notes --ref=refs/notes/pi-review show HEAD`.
3. **Amend-after-go**: after scenario 1, `git commit --amend` (same tree) →
   the review log was already consumed, so the amended commit gets a "no
   review" audit note (accepted edge consequence of the cleanup decision).
4. **Note-attach failure**: force a `git notes` failure (e.g. corrupt the
   notes ref or mock the command) → post-commit exits non-zero, the error is
   visible in pre-commit output, the commit stands, and the review log is
   kept for a retry.
5. **Archiving off**: repeat scenario 1 without `--archive-sessions` → the
   note has no `Session archive:` line, and no `archive/` dir is created.

## 9. Verification commands

```bash
git log --notes=pi-review                      # review notes inline
git notes --ref=refs/notes/pi-review show HEAD # single commit's note
git config notes.displayRef refs/notes/pi-review  # one-time: plain git log shows notes
git push origin refs/notes/pi-review           # share notes with collaborators
zgrep '"type":"message"' .git/pi-reviewer/archive/session-*.jsonl.gz  # inspect an archive
```

## 10. Open notes / edge cases (accepted)

- Amend-after-go produces a "no review" audit note on the amended commit
  (the review log was consumed by the original commit) — accepted.
- Orphaned review logs (go whose commit never lands) stay on disk — small,
  harmless, and a future commit of that tree still gets its note.
- The notes ref is not pushed by default; sharing notes across clones
  requires the explicit `git push origin refs/notes/pi-review` (documented).
- The archive writer must adapt to the actual pi session store layout
  (entries in order); verify against a real archived session during commit
  (a) dogfooding.
