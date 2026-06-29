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
  (tick/slash/**plain-English** -> act on target -> consume card), `scan-backstop`
  (scheduled scan -> reconcile), `deep-review` (phase 2, inert).
- **Scripts:** `triage_core.py` (scan/classify/dedup/security gate + shared utils
  `parse_state_block`, `authorized`, `state`, `nl-decisions-enabled`),
  `render_card.py` (render + card CRUD), `apply_decision.py` (deterministic
  `parse` then `execute`, plus the natural-language `nl-eligible`/`nl-prompt`/
  `nl-route` that map an owner's free-text comment to a structured intent),
  `build_item.py` (normalize ingest payload), `reconcile.py` (backstop
  create/close). `render_card`/`apply_decision`/`reconcile`/`build_item` import
  `triage_core` via `sys.path.insert(0, dirname(__file__))`.
- **Reusable actions (pinned to full SHAs).** `decision-handler` delegates two
  mechanical jobs to the `issue-ops` toolkit instead of hand-rolling them:
  `issue-ops/parser` renders the card's checkboxes as `{selected, unselected}`
  (run twice - new body + pre-edit body - so `apply_decision.py` can keep the
  "exactly one newly-ticked" diff), and `issue-ops/labeler` does every
  `processing`/`resolved`/`blocked`/`needs-decision` add/remove (with
  `create: true` so it also creates the label objects). Pin both to a commit SHA
  with a trailing `# vX.Y.Z` comment; never a floating tag.

## Sharp edges

- Decision cards are machine-created. The card body's hidden state block and the
  per-checkbox `<!-- opt:KEY -->` markers are load-bearing - the handler diffs
  the `selected` lists `issue-ops/parser` returns for the new vs pre-edit body to
  find the newly-ticked option (the marker survives because the parser strips
  only the `- [x] ` prefix), and parses slash-commands against the kind's allowed
  set. Don't reformat them away.
- `.github/ISSUE_TEMPLATE/triage-decision.yml` is load-bearing, not cosmetic:
  `issue-ops/parser` only returns `{selected, unselected}` when a template marks
  the section as a `checkboxes` field, and it matches the section by EXACT heading
  text. Its `checkboxes` label MUST stay `"Your decision"` to match the
  `### Your decision` heading `render_card.py` emits. (Cards are still rendered by
  `render_card.py`, not this template; a hand-filed issue from it has no state
  block, so the handler treats it as a no-op.)
- Natural-language decisions are owner-comment-only and structured: the LLM
  returns `{mode: action|answer|clarify, action?, free_text?, answer?}` to
  `decision.json` and nothing else. `apply_decision.py nl-route` is the trust
  boundary - it validates `action` against the per-kind allowlist and only then
  sets the `decision` output that makes the SAME deterministic `execute` run
  (so every guard - allowlist, head-SHA re-check, fork-CI HOLD, token isolation,
  concurrency - applies unchanged). `answer`/`clarify` only post a card comment
  and leave the card open. The LLM is restricted to the `Write` tool and gets
  only this repo's token, never `FLEET_TOKEN` - it maps intent, it never acts.
- Token discipline per step: scan/execute and the read-only target fetch for the
  LLM (`deep-review` prepare, decision-handler `nl-fetch`) use `FLEET_TOKEN`; all
  card writes - including every `issue-ops/labeler` step (its `github_token`
  defaults to `github.token`, passed explicitly here) - use `github.token`.
  Mixing them either breaks cross-repo acting or creates a re-trigger loop. The
  LLM step itself never gets `FLEET_TOKEN`; target content reaches it only as
  pre-fetched, delimited untrusted data inside the prompt.
- `triage_core.py scan` is resilient: a repo that fails to read is reported as a
  warning (`ok:false`) and skipped, and `reconcile.py` must never close cards for
  an `ok:false` repo (state unknown).

## LLM side-jobs (both opt-in, both off by default)

Two independent LLM features share the same auth (a Claude **subscription** token
from `claude setup-token` via `anthropics/claude-code-action` - NOT an Anthropic
API key) and the same injection model (only owner-authored text is an
instruction; target content is delimited untrusted data; the LLM gets only this
repo's token):

- **`deep_review`** + `deep-review.yml`: label `needs-deep-review` -> Claude
  posts a read-only merit/triage verdict. Inert unless `deep_review: true` AND
  `CLAUDE_CODE_OAUTH_TOKEN` present.
- **`nl_decisions`** in `decision-handler.yml`: a plain-English owner comment is
  mapped to a structured intent (see Sharp edges). Inert unless
  `nl_decisions: true` AND `CLAUDE_CODE_OAUTH_TOKEN` present. Claude is restricted
  to the `Write` tool (`claude_args: --allowedTools Write`) - it writes
  `decision.json` and runs no commands.

## Validation

No build step. Validate with `python -m py_compile scripts/*.py tests/*.py`, run
the decision unit test (`python tests/test_decision.py` - mocks the LLM, no
network), and YAML-parse `.github/workflows/*.yml` + `triage.config.yml` +
`.github/ISSUE_TEMPLATE/*.yml` (run `actionlint` if available; fetch the binary
via its `download-actionlint.bash` if not). The live LLM paths (deep-review,
nl_decisions) can only be exercised end-to-end in CI with the flag on and the
token set. Secrets the maintainer must add: `FLEET_TOKEN` (always) and
`CLAUDE_CODE_OAUTH_TOKEN` (deep_review and/or nl_decisions only).
