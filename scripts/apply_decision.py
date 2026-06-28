#!/usr/bin/env python3
"""
Triage Hub - decision executor.

Two phases, run as separate workflow steps so each uses the right token:

  parse    Determine the decision from the triggering event (checkbox tick,
           slash-command, or decision:<key> label). No side effects, no token
           needed. Writes decision/target to $GITHUB_OUTPUT.

  execute  Act on the TARGET repo (merge / approve-ci / close / decline /
           comment) using the ambient GH_TOKEN, which the workflow sets to
           FLEET_TOKEN for this step. Writes result_message/terminal_state to
           $GITHUB_OUTPUT.

Security: the caller owner-gates the whole job; only owner-authored text ever
reaches this script. Merge re-checks the PR head SHA against the card's state
block and refuses if the PR moved. approve-ci routes through the security HOLD.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import triage_core as core  # noqa: E402

# Slash actions allowed per kind (checkbox options are a subset; comment/decline
# are text-bearing and slash-only).
ALLOWED = {
    "pr-review": {"merge", "close", "decline", "hold", "comment"},
    "ci-approval": {"approve-ci", "close", "decline", "hold", "comment"},
    "issue-triage": {"close", "decline", "hold", "comment"},
}

SLASH = {
    "/merge": "merge",
    "/approve-ci": "approve-ci",
    "/approve_ci": "approve-ci",
    "/close": "close",
    "/decline": "decline",
    "/hold": "hold",
    "/comment": "comment",
}


# --------------------------------------------------------------------------- #
# $GITHUB_OUTPUT
# --------------------------------------------------------------------------- #
def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    text = "" if value is None else str(value)
    if not path:
        print("%s=%s" % (name, text))
        return
    with open(path, "a") as f:
        if "\n" in text:
            f.write("%s<<__TRIAGE_EOF__\n%s\n__TRIAGE_EOF__\n" % (name, text))
        else:
            f.write("%s=%s\n" % (name, text))


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #
def parse_slash(comment, allowed):
    if not comment:
        return (None, "")
    first = comment.strip().splitlines()[0].strip() if comment.strip() else ""
    if not first.startswith("/"):
        return (None, "")
    parts = first.split(None, 1)
    action = SLASH.get(parts[0].lower())
    rest = parts[1].strip() if len(parts) > 1 else ""
    if action not in allowed:
        return (None, "")
    if action in ("comment", "decline") and not rest:
        if action == "comment":
            return (None, "")  # nothing to post
        rest = "Declining for now."
    return (action, rest)


_CHK_RE = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*.*?<!--\s*opt:([a-z\-]+)\s*-->", re.M)


def _checked_map(body):
    out = {}
    for m in _CHK_RE.finditer(body or ""):
        out[m.group(2)] = m.group(1).lower() == "x"
    return out


def diff_checkbox(old_body, new_body, options):
    old = _checked_map(old_body)
    new = _checked_map(new_body)
    newly = [k for k, v in new.items() if v and not old.get(k) and k in options]
    return newly[0] if len(newly) == 1 else None  # exactly one tick, else no-op


def parse_label(label_name, allowed):
    if label_name and label_name.startswith("decision:"):
        key = label_name.split(":", 1)[1].strip()
        if key in allowed:
            return key
    return None


def cmd_parse():
    body = os.environ.get("ISSUE_BODY", "")
    state = core.parse_state_block(body)
    if not state:
        set_output("decision", "")  # not a triage card
        return
    kind = state.get("kind", "pr-review")
    allowed = ALLOWED.get(kind, set())
    options = state.get("options", [])

    event = os.environ.get("EVENT_NAME", "")
    action = os.environ.get("EVENT_ACTION", "")
    decision, free_text = None, ""

    if event == "issue_comment":
        decision, free_text = parse_slash(os.environ.get("COMMENT_BODY", ""), allowed)
    elif event == "issues" and action == "edited":
        decision = diff_checkbox(os.environ.get("OLD_BODY", ""), body, options)
    elif event == "issues" and action == "labeled":
        decision = parse_label(os.environ.get("LABEL_NAME", ""), allowed)

    if not decision:
        set_output("decision", "")
        return

    set_output("decision", decision)
    set_output("free_text", free_text)
    set_output("target_repo", state.get("repo", ""))
    set_output("target_number", state.get("number", ""))
    set_output("kind", kind)
    set_output("head_sha", state.get("head_sha", ""))


# --------------------------------------------------------------------------- #
# execute (ambient GH_TOKEN = FLEET_TOKEN)
# --------------------------------------------------------------------------- #
def _merge_method(repo):
    try:
        rc = core.load_config()["repos"].get(repo, {})
        return rc.get("merge_method") or "squash"
    except SystemExit:
        return "squash"


def _comment_target(slug, number, text):
    core.gh_rest("/repos/%s/issues/%s/comments" % (slug, number), method="POST",
                 fields={"body": text})


def _close_target(slug, number):
    core.gh_rest("/repos/%s/issues/%s" % (slug, number), method="PATCH",
                 fields={"state": "closed"})


def do_merge(owner, repo, number, head_sha):
    slug = "%s/%s" % (owner, repo)
    pr = core.gh_rest("/repos/%s/pulls/%s" % (slug, number))
    if pr.get("merged"):
        return ("Target %s#%s is already merged - nothing to do." % (repo, number), "resolved")
    if pr.get("state") != "open":
        return ("Target %s#%s is not open (%s) - consuming card." % (repo, number, pr.get("state")), "resolved")
    current = (pr.get("head") or {}).get("sha", "")
    if head_sha and current and current != head_sha:
        return ("HOLD: %s#%s head moved since this card (was %s, now %s). Re-scan before merging."
                % (repo, number, head_sha[:8], current[:8]), "blocked")
    method = _merge_method(repo)
    try:
        core.gh_rest("/repos/%s/pulls/%s/merge" % (slug, number), method="PUT",
                     fields={"merge_method": method})
    except RuntimeError as e:
        return ("Merge of %s#%s failed: %s" % (repo, number, str(e)[:200]), "error")
    return ("Merged %s#%s (%s)." % (repo, number, method), "resolved")


def do_approve_ci(owner, repo, number):
    status, message = core.approve_ci(owner, repo, number)
    if status == "hold":
        return (message, "blocked")
    if status == "error":
        return (message, "error")
    return (message, "resolved")


def do_close(owner, repo, number, reason=None):
    slug = "%s/%s" % (owner, repo)
    if reason:
        _comment_target(slug, number, reason)
    try:
        _close_target(slug, number)
    except RuntimeError as e:
        return ("Close of %s#%s failed: %s" % (repo, number, str(e)[:200]), "error")
    suffix = " with a note" if reason else ""
    return ("Closed %s#%s%s." % (repo, number, suffix), "resolved")


def do_comment(owner, repo, number, text):
    slug = "%s/%s" % (owner, repo)
    try:
        _comment_target(slug, number, text)
    except RuntimeError as e:
        return ("Comment on %s#%s failed: %s" % (repo, number, str(e)[:200]), "error")
    return ("Posted your comment on %s#%s." % (repo, number), "none")


def cmd_execute():
    owner = core.get_owner()
    decision = os.environ.get("DECISION", "")
    free_text = os.environ.get("FREE_TEXT", "")
    repo = os.environ.get("TARGET_REPO", "")
    number = os.environ.get("TARGET_NUMBER", "")
    head_sha = os.environ.get("HEAD_SHA", "")

    if not decision or not repo or not number:
        set_output("result_message", "No actionable decision.")
        set_output("terminal_state", "none")
        set_output("success", "false")
        return

    if decision == "merge":
        message, terminal = do_merge(owner, repo, number, head_sha)
    elif decision == "approve-ci":
        message, terminal = do_approve_ci(owner, repo, number)
    elif decision == "close":
        message, terminal = do_close(owner, repo, number)
    elif decision == "decline":
        message, terminal = do_close(owner, repo, number, reason=free_text or "Declining for now.")
    elif decision == "comment":
        message, terminal = do_comment(owner, repo, number, free_text)
    elif decision == "hold":
        message, terminal = ("Held %s#%s - parked for manual handling." % (repo, number), "blocked")
    else:
        message, terminal = ("Unknown decision %r - no action taken." % decision, "error")

    set_output("result_message", message)
    set_output("terminal_state", terminal)
    set_output("success", "true" if terminal not in ("error",) else "false")


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: apply_decision.py parse|execute")
    if sys.argv[1] == "parse":
        cmd_parse()
    elif sys.argv[1] == "execute":
        cmd_execute()
    else:
        sys.exit("usage: apply_decision.py parse|execute")


if __name__ == "__main__":
    main()
