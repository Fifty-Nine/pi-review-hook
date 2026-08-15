# Implementation Plan: Amend-after-Go Support

**Status**: Approved (Grill Me design interview, 2026-08-15)
**Scope**: pi-review-precommit — support `git commit --amend` after a go-with-feedback
**Design source**: Grill Me checkpoint (shared understanding, all branches resolved)

---

## 1. Overview

When the reviewer approves a change (`go`) but gives feedback (suggestions),
users often `git commit --amend` to address it in the same commit before
pushing. Today this is broken in two ways:

1. **Notes are lost**: the original review's note sits on the commit the amend
   orphans; that commit is unreachable and `git gc` reclaims it.
2. **Delta-only review**: the pre-commit hook reviews only `git diff --cached`
   (index vs HEAD = the new changes), not the full amended change set, so the
   reviewer can't re-evaluate the whole change.

This plan adds amend detection that resumes the existing session and shows the
reviewer the **full change set** the amended commit will contain, plus a
post-commit note carry-forward so the review history survives the amend.

## 2. Agreed design summary

| Branch | Decision |
|---|---|
| Root | On a detected amend, resume the existing session and show the full change set (diff from the parent of the approved commit to the staged tree); the reviewer verifies the previous feedback was addressed and re-evaluates the whole change |
| Detection | Process-primary + fresh fallback: walk `/proc` ancestors (Linux, stdlib) for the nearest `git` ancestor with `--amend`; `AMEND`→follow-up, `NOT_AMEND`→non-amend path, `UNKNOWN`→non-amend path. No lumping; non-Linux amends fall through to the current behavior (documented) |
| Detached-HEAD guard | `git symbolic-ref -q HEAD` fails (rebase/cherry-pick) → skip amend behavior, fall back to non-amend path. Scopes amend to real branch commits |
| State lifecycle | Lazy clear + archive snapshot at go: keep `session_id`+`approved_tree`+`base_tree` after go; archive a session snapshot at go (note links it immediately); keep the live session dir for resume; clear on the next `NOT_AMEND`/`UNKNOWN` commit (round 0). Multi-amend: each go archives a new snapshot, session accumulates. No-go on amend: keep state, resume next amend |
| Diff semantics | Resume the existing pi session (`--session-id`); prompt = full change set diff (`base_tree`→staged) + "you previously approved this; verify your suggestions were addressed and re-evaluate." Framing by round (after go: verify suggestions; after no-go-on-amend: verify issues + leniency) |
| Notes behavior | Carry-forward via post-commit reflog detection: `HEAD@{1}` not an ancestor of `HEAD` → amend → carry `HEAD@{1}`'s note forward for ALL amends (reviewed, SKIP'd, empty-diff). New note = `Previous review (amended from <old-sha>): <old note>` + `Amended review: <new re-review>` (or `...amended without re-review` audit line). Decoupled from the pre-commit review |
| Validation | Unit tests (mock the `/proc` walk for the AMEND path) + dogfooding + scratch-repo E2E |
| Rollout | Three atomic commits, each pushed + rev bumped + dogfooded, then scratch-repo E2E |

### Detection matrix (pre-commit)

| `detect_amend()` | round | Behavior |
|---|---|---|
| `AMEND` | 0 | Amend follow-up: resume session, full change set (base→staged), verify suggestions + re-evaluate |
| `AMEND` | >0 | Amend follow-up: resume session, full change set, verify issues + leniency + re-evaluate |
| `NOT_AMEND` | 0 | Fresh review: clear kept-after-go state, new session, delta diff |
| `NOT_AMEND` | >0 | Existing no-go follow-up: resume session, delta + leniency *(preserved)* |
| `UNKNOWN` | 0 | Fresh review (current broken-amend behavior) |
| `UNKNOWN` | >0 | Existing no-go follow-up *(preserved)* |
| detached HEAD | any | `NOT_AMEND` (skip amend behavior) |

### Rebase hook behavior (verified)

`git rebase` suppresses the pre-commit hook for `pick`/`squash`/`fixup`
(`-n`/`--no-verify`) and `reword` (`--no-pre-commit`; only `commit-msg` runs);
conflict resolution via `rebase --continue` also doesn't run it. The only
rebase false positive is `rebase -i edit`, where the user explicitly runs
`git commit --amend` — handled by the detached-HEAD guard.

## 3. Commit (a): the pre-commit amend feature

**Goal**: detect amends, keep state after go, and run an amend follow-up review
(resume session, full change set) — one coherent commit because state +
detection + prompt need each other.

### New module `src/pi_review_precommit/amend_detect.py`

- **`detect_amend() -> str`** — returns `"AMEND"` / `"NOT_AMEND"` /
  `"UNKNOWN"`.
  1. **Detached-HEAD guard**: run `git symbolic-ref -q HEAD`; if it exits
     non-zero → return `"NOT_AMEND"` (rebase/cherry-pick → skip amend
     behavior).
  2. **Process heuristic** (Linux `/proc`, stdlib-only): from `os.getpid()`,
     walk ancestors via `/proc/<pid>/stat` (PPid) and `/proc/<pid>/cmdline`
     (null-separated argv). Find the **nearest** ancestor whose argv[0]
     basename is `git` (or ends with `/git`):
     - `--amend` in its argv → `"AMEND"`.
     - a `git` ancestor without `--amend` → `"NOT_AMEND"`.
     - no `git` ancestor found, or `/proc` unavailable → `"UNKNOWN"`.
  3. Return on the **first** `git` ancestor (closest = the `git commit`
     invocation, not other `git` subcommands the hook itself spawned, which
     are children not ancestors).
- Keep it pure and unit-testable: factor the ancestor walk into a helper that
  can be replaced/mocked (e.g. `_iter_ancestors()` yielding `(pid, argv)`).

### `src/pi_review_precommit/state.py`

- State schema gains `approved_tree` and `base_tree` (both `str | None`):
  ```
  {"session_id": ..., "rejected_trees": [...], "round": N,
   "approved_tree": "...", "base_tree": "..."}
  ```
- **`record_go_state(session_id, approved_tree, base_tree)`** — write state
  with `rejected_trees=[]`, `round=0`, and the given `approved_tree`/
  `base_tree`. Keeps the session for resume.
- **`record_rejection`** — unchanged, but preserve `approved_tree`/`base_tree`
  (a no-go on an amend keeps the last go's base; the next amend resumes).
- **`clear_state`** — unchanged (used by the fresh path to clear kept-after-go
  state: rmtree sessions + unlink state).
- **`archive_sessions`** — unchanged (archive snapshot at go).
- **`get_approved_tree()` / `get_base_tree()`** — accessors returning `None`
  when absent.

### `src/pi_review_precommit/prompts.py`

- **`get_full_change_set_diff(base_tree, staged_tree) -> str`** —
  `git diff <base_tree> <staged_tree>` (the full amended change set). Handle
  the root-commit edge: if `base_tree` is the empty tree
  (`4b825dc642cb6eb9a060e54bf8d69288fbee4904`), `git diff` against it.
- **`build_amend_prompt(diff, files, round_number, review_guidelines=None)`**
  — the amend follow-up prompt:
  - Always: the full change set diff + "you previously approved this change;
    verify your suggestions were addressed and re-evaluate the full amended
    change (you may raise new issues anywhere)."
  - Round 0 (after go): framing = verify suggestions + re-evaluate.
  - Round > 0 (after no-go-on-amend): framing = verify the previous issues
    were resolved + re-evaluate + proportionally lenient on resolved issues.
  - REVIEW_GUIDELINES.md appended if present (as today).

### `src/pi_review_precommit/hook.py`

The flow changes (the existing no-go retry must be preserved):

1. Check pi in PATH (fail-open).
2. Compute staged tree hash + the **delta** diff (`git diff --cached`) — the
   delta is still needed for the empty-diff check and the fresh/no-go paths.
3. Empty diff → exit 0 (nothing to review; covers `--amend --no-edit` with no
   staged changes).
4. Same-tree auto-reject (rejected_trees) — unchanged.
5. Load state: `session_id`, `round_number`, `approved_tree`, `base_tree`.
6. `amend = detect_amend()` (includes the detached-HEAD guard).
7. **Select the path**:
   - `AMEND` **and** `approved_tree` is set:
     - `full_diff = get_full_change_set_diff(base_tree, staged_tree_hash)`.
     - `prompt = build_amend_prompt(full_diff, files, round_number, ...)`.
     - Resume the existing session (`session_id` from state).
   - else (`NOT_AMEND`/`UNKNOWN`, or no `approved_tree`):
     - `round > 0` → existing no-go follow-up: resume session, delta +
       leniency (`build_followup_prompt`) — **preserved**.
     - `round == 0` → fresh: `clear_state(...)` (clear kept-after-go state),
       new `session_id`, `save_state` fresh, `build_first_round_prompt`
       (delta). (If there was kept-after-go state, this clears it.)
8. Invoke pi (`run_review`, passing the chosen `session_id` + prompt).
9. Handle the decision:
   - **go**:
     1. `archive_sessions(...)` if `config.archive_sessions` (snapshot, so
        the note links it) — fail-closed on error.
     2. `record_approval(session_id, tree_hash, round_number, decision_args,
        archive_path=...)`.
     3. Compute `base_tree` for the just-approved commit:
        - `AMEND` → `git rev-parse HEAD~1^{tree}` (amended commit's parent =
          HEAD's parent; amend keeps the parent).
        - fresh → `git rev-parse HEAD^{tree}` (new commit's parent = HEAD).
        - (root commit / no HEAD → empty tree).
     4. `record_go_state(session_id, approved_tree=staged_tree_hash,
        base_tree=...)` — **keep** the session dir + state (do NOT
        `clear_state`). The live session dir stays for resume.
     5. Print summary/suggestions; exit 0.
   - **no-go**: `record_rejection(...)` (preserves `approved_tree`/`base_tree`);
     print issues; exit 1.

### Tests

- `test_amend_detect.py` (new): `detect_amend` parsing with a **mocked**
  `_iter_ancestors` (inject a fake list including a `git --amend` entry →
  `AMEND`; a `git` without `--amend` → `NOT_AMEND`; no `git` → `UNKNOWN`);
  detached-HEAD detection (mock `git symbolic-ref`); the AMEND path can't
  fire under pytest, so all `/proc` access is mocked.
- `test_state.py`: `record_go_state` keeps `session_id`+`approved_tree`+
  `base_tree` and clears `rejected_trees`/resets `round`; `record_rejection`
  preserves `approved_tree`/`base_tree`; `get_approved_tree`/`get_base_tree`.
- `test_prompts.py`: `build_amend_prompt` round-dependent framing (round 0 vs
  >0); `get_full_change_set_diff` command construction.
- `test_hook.py`: AMEND → resume + full change set + record_go_state (keep);
  NOT_AMEND round 0 → fresh (clear kept state); NOT_AMEND round > 0 → no-go
  follow-up preserved; go-on-amend keeps state; no-go-on-amend keeps
  `approved_tree`/`base_tree`; detached HEAD → non-amend path; base_tree
  computation (amend vs fresh).

## 4. Commit (b): post-commit notes carry-forward

**Goal**: the post-commit hook detects amends via the reflog and carries the
old review's note forward, for all amends (reviewed, SKIP'd, empty-diff).

### `src/pi_review_precommit/notes.py`

- **`detect_amend_reflog() -> str | None`** — returns the old commit SHA if
  this was an amend, else `None`.
  - `old = git rev-parse --verify -q HEAD@{1}`; if missing → `None` (root
    commit / no prior HEAD).
  - `git merge-base --is-ancestor <old> HEAD`: exit 0 → `old` is an ancestor
    (new commit on top / merge) → `None`; non-zero → `old` is NOT an ancestor
    (amend) → return `old`.
- **`get_note(commit_sha) -> str | None`** —
  `git notes --ref=refs/notes/pi-review show <sha>`; return the text or `None`
  if no note / error.
- **`build_carry_forward_note(old_sha, old_note, new_review_text) -> str`** —
  ```
  Previous review (amended from <old-sha>):
  <old note>

  Amended review:
  <new re-review>
  ```
- **`build_amended_no_review_note(old_sha, old_note) -> str`** —
  ```
  Previous review (amended from <old-sha>):
  <old note>

  Amended without re-review (skipped, bypassed, or tree mismatch).
  ```
- **`main()`** changes:
  1. Resolve HEAD commit + tree.
  2. `log = find_review_log_for_tree(tree)`.
  3. `old_sha = detect_amend_reflog()`; `old_note = get_note(old_sha)` if
     `old_sha`.
  4. If `log` found: `new_text = build_note_text(log)`; if amend, prepend the
     carry-forward (`build_carry_forward_note`); attach; delete log (cleanup
     failure → stderr warning).
  5. If `log` not found: if amend and `old_note` exists →
     `build_amended_no_review_note`; else `build_audit_note_text`; attach.
  6. Note-attach failure → exit 1, log kept (fail-open but visible). Same as
     today.

### Tests

- `test_notes.py`: `detect_amend_reflog` (mock `git`: ancestor → `None`,
  non-ancestor → old sha; missing `HEAD@{1}` → `None`); carry-forward note
  building; amended-no-review note building; `main` flow: reviewed amend →
  carry-forward + re-review + log deleted; SKIP'd amend (no log, old note) →
  amended-no-review note; empty-diff amend (no log) → amended-no-review (or
  audit if no old note); new-commit-on-top (reflog ancestor) → no
  carry-forward; note-attach failure → exit 1 + log kept.

## 5. Commit (c): docs + non-Linux behavior

### `README.md`

- New "Amending after a go" section: when you `git commit --amend` after a
  go-with-feedback, the hook detects the amend (process hierarchy on Linux),
  resumes the session, and re-reviews the full amended change set; the
  amended commit's note carries the previous review forward.
- Non-Linux caveat: on systems without `/proc`, amend detection can't run, so
  amends fall through to the current behavior (fresh session, delta-only);
  `SKIP=pi-review` to bypass.
- Detached-HEAD note: the amend behavior is skipped during rebase/cherry-pick.

### `AGENTS.md`

- Flow diagram: amend detection branch + record_go_state (keep) + the
  post-commit reflog carry-forward.
- State model: add `approved_tree` / `base_tree` fields; note the lazy-clear
  lifecycle.
- Module layout: add `amend_detect.py`.
- Implementation status: check off the three commits.
- Known issues: non-Linux amend detection degrades to current behavior;
  session/note growth across many amends; rebase `edit` handled by the
  detached-HEAD guard.

## 6. Push + rev-bump + dogfood cycle

After each commit, in order:

1. `git push` (origin/master).
2. Bump the dogfooding `rev` in `.pre-commit-config.yaml` to the new HEAD.
3. Commit the bump through the dogfood hook, push.

**Chicken-and-egg**: the dogfooding config can only reference published revs,
and the new amend behavior only activates after the rev is bumped. So
dogfooding the amend flow happens naturally on the first amend **after**
commit (a) + its rev bump.

**Dogfood the amend flow** (after (a) is live): make a trivial commit (go) →
make a small staged change → `git commit --amend` → verify:
- the review **resumed** the session (round 1, not a fresh session);
- the prompt used the **full change set** (diff vs the parent of the approved
  commit), not just the delta;
- the amended commit's **note carries forward** the previous review (after
  (b) is live).

## 7. Scratch-repo E2E scenarios

Disposable repo (e.g. `/tmp/pi-review-amend-e2e`) with the hook repo pinned at
the new rev, both hook ids enabled, `--archive-sessions` on, post-commit
installed (`pre-commit install --hook-type post-commit`).

1. **Amend-after-go**: stage a change → `git commit` (go) → stage a small
   follow-up change → `git commit --amend`. Verify: the amend review resumed
   the session (round 1), the prompt was the full change set (base→staged),
   the amended commit's note = previous review + amended review, the previous
   review's log was consumed, a new archive snapshot exists.
2. **New-commit-on-top (NOT_AMEND)**: after a go, make a **new** commit on
   top (plain `git commit`, unrelated intent). Verify: fresh session (round
   0), delta diff, **no** carry-forward (reflog: HEAD@{1} is an ancestor);
   the previous commit's note is untouched.
3. **SKIP'd amend carry-forward**: `SKIP=pi-review git commit --amend`. Verify:
   no re-review; the amended commit's note = `Previous review (amended from
   <old>): <old note>` + `Amended without re-review ...` (post-commit reflog
   detection, decoupled from the pre-commit review).
4. **Empty-diff amend (message fix)**: `git commit --amend --no-edit` with no
   staged changes. Verify: pre-commit exits 0 (empty diff); the amended
   commit's note carries the old note forward + the no-review audit line.
5. **No-go-on-amend round 2**: stage a change → commit (go) → amend with a
   flaw → review rejects (no-go) → amend again fixing it → verify round 2,
   the prompt uses the full change set + the previous issues + leniency, and
   `approved_tree`/`base_tree` are unchanged across the no-go.
6. **Detached-HEAD rebase skip**: `git rebase -i` with `edit` on a reviewed
   commit, modify, `git commit --amend`. Verify: detached-HEAD guard fires →
   non-amend path (fresh delta, not the full change set); no wrong-base
   review.

## 8. Verification commands

```bash
# amend review resumed the session?
git log --notes=pi-review                        # amended commit's note
git notes --ref=refs/notes/pi-review show HEAD   # carry-forward note
# session archive snapshot
zgrep '"type":"message"' .git/pi-reviewer/archive/session-*.jsonl.gz
# state kept after go
cat .git/pi-reviewer/state.json   # approved_tree + base_tree present
```

## 9. Open notes / edge cases (accepted)

- **Non-Linux**: amend detection can't run (no `/proc`); amends fall through
  to the current behavior (fresh session, delta-only, orphaned note). Documented.
- **Session growth**: each amend appends a full-change-set turn; the session
  accumulates. Accepted; could exceed context for large changes / many amends.
- **Multiple amends**: the note + session grow (each go archives a new
  snapshot; the note carries forward cumulatively). Accepted.
- **Rebase `edit`**: handled by the detached-HEAD guard (fresh delta review,
  not the amend follow-up). The other rebase actions don't run the pre-commit
  hook at all.
- **Process-heuristic brittleness**: a stdlib `/proc` walk; fails closed to
  `UNKNOWN` (non-amend path) when it can't determine. Unit tests mock the
  walk (the AMEND path can't fire under pytest).
- **Root commit / merge `base_tree`**: computed at go time (amend → `HEAD~1`,
  fresh → `HEAD`); the empty-tree hash covers the root-commit edge. Merges
  use the first parent (`HEAD~1`).