# Project agent memory

Triage Hub - a portable, forkable IssueOps machine. Issues in this repo are a
human-in-the-loop decision queue for cross-repo OSS maintenance, driven entirely
by GitHub Actions. This file holds durable, project-intrinsic notes.

## Non-negotiable invariants

- **Portability / fork-and-own.** Never hardcode an owner or repo name in
  workflows or scripts. Owner is always `github.repository_owner` (env
  `GITHUB_REPOSITORY_OWNER`); the fleet + policy come from the single root file
  `triage.config.yml`. A fork on any account must work after editing only that
  file and adding the secrets.
- **Security.** Owner-gate every acting path (`sender == repository_owner`, plus
  optional `maintainer` override via `triage_core.py authorized`). Cross-repo
  actions use `FLEET_TOKEN`; everything that touches THIS repo's cards uses the
  default `GITHUB_TOKEN` (this is also what prevents the decision-handler from
  re-triggering itself - GitHub does not raise workflow events for
  GITHUB_TOKEN-authored activity). The fork-CI / pwn-request HOLD (exit 4 in
  `approve_ci`) must never be removed: approving fork CI that changes
  `.github/workflows`, `.github/actions`, or `action.yml(.yaml)` is held for
  manual review and fails closed.

## Architecture

- **State lives in GitHub, not on disk.** Open issue = pending decision; closed =
  consumed. Labels are state (`needs-decision`, `processing`, `resolved`,
  `blocked`, `repo:*`, `kind:*`, `priority:*`). A hidden
  `<!-- triage-state: {...} -->` block in each card body carries
  `{repo, number, kind, head_sha, options}`. The local lock/board/ledger from
  the original `triage.py` are intentionally dropped (replaced by Actions
  `concurrency` + issues/labels/comments).
- **Workflows:** `ingest` (dispatch/manual -> upsert a card), `decision-handler`
  (tick/slash/label -> act on target -> consume card), `scan-backstop`
  (scheduled scan -> reconcile), `deep-review` (phase 2, inert).
- **Scripts:** `triage_core.py` (scan/classify/dedup/security gate + shared utils
  `parse_state_block`, `authorized`, `state`), `render_card.py` (render + card
  CRUD), `apply_decision.py` (`parse` then `execute`, split so each phase uses
  the right token), `build_item.py` (normalize ingest payload), `reconcile.py`
  (backstop create/close). `render_card`/`apply_decision`/`reconcile`/`build_item`
  import `triage_core` via `sys.path.insert(0, dirname(__file__))`.

## Sharp edges

- Decision cards are machine-created. The card body's hidden state block and the
  per-checkbox `<!-- opt:KEY -->` markers are load-bearing - the handler diffs
  old vs new body to find the newly-ticked option, and parses slash-commands
  against the kind's allowed set. Don't reformat them away.
- Token discipline per step: scan/execute/deep-review-target-fetch use
  `FLEET_TOKEN`; all card writes use `github.token`. Mixing them either breaks
  cross-repo acting or creates a re-trigger loop.
- `triage_core.py scan` is resilient: a repo that fails to read is reported as a
  warning (`ok:false`) and skipped, and `reconcile.py` must never close cards for
  an `ok:false` repo (state unknown).

## Phase 2

`deep-review.yml` is scaffolded but **inert** unless `deep_review: true` AND the
`CLAUDE_CODE_OAUTH_TOKEN` secret is present. Auth is a Claude **subscription**
token (`claude setup-token`) via `anthropics/claude-code-action` - NOT an
Anthropic API key. Only owner-authored card text reaches the LLM as
instructions; target content is passed as delimited untrusted data and the LLM
only gets this repo's token.

## Validation

No build step. Validate with `python -m py_compile scripts/*.py` and a YAML
parse of `.github/workflows/*.yml` + `triage.config.yml` (run `actionlint` if
available). Secrets the maintainer must add: `FLEET_TOKEN` (always) and
`CLAUDE_CODE_OAUTH_TOKEN` (phase 2 only).
