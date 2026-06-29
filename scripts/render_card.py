#!/usr/bin/env python3
"""
Wheelhouse - decision-card renderer + card operations.

`render(item)` turns one classified item into a decision card: a human-readable
body with quick-decision checkboxes and a hidden machine-readable state block.
`upsert_card`/`close_card` create/update/consume cards in THIS repo (via the
ambient GH_TOKEN, which the workflow sets to the default GITHUB_TOKEN so that
card-side activity never re-triggers the handler).

CLI:
  render_card.py upsert --item-file item.json    create-or-update a card (dedup by marker)
  render_card.py render --item-file item.json --out-dir DIR    debug: write title/body/labels
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

# Quick-decision (checkbox) option keys per kind. Comment / decline carry text,
# so they are slash-command-only (see apply_decision.py), not checkboxes.
CHECKBOX_OPTIONS = {
    "pr-review": ["merge", "close", "hold"],
    "ci-approval": ["approve-ci", "close", "hold"],
    "issue-triage": ["close", "hold"],
}

OPTION_LABELS = {
    "merge": "Merge it",
    "approve-ci": "Approve the CI run (security-gated)",
    "close": "Close / decline",
    "hold": "Hold - I'll handle this manually",
}

SLASH_HINT = {
    "pr-review": "`/merge`, `/close`, `/decline <reason>`, `/hold`, `/comment <text>`",
    "ci-approval": "`/approve-ci`, `/close`, `/decline <reason>`, `/hold`, `/comment <text>`",
    "issue-triage": "`/close`, `/decline <reason>`, `/hold`, `/comment <text>`",
}

KIND_LABEL = {
    "pr-review": "PR review",
    "ci-approval": "CI approval",
    "issue-triage": "Issue triage",
}


def marker_label(item):
    return "target:%s-%s" % (item["repo"], item["number"])


def card_labels(item):
    return [
        "needs-decision",
        "repo:%s" % item["repo"],
        "kind:%s" % item["kind"],
        "priority:%s" % item.get("priority", "low"),
        marker_label(item),
    ]


def render(item):
    """item -> {title, body, labels, marker}. Tolerates missing optional fields."""
    kind = item.get("kind", "pr-review")
    repo = item["repo"]
    number = int(item["number"])
    title = (item.get("title") or "").strip() or "(no title)"
    options = item.get("options") or CHECKBOX_OPTIONS.get(kind, ["close", "hold"])

    state = {
        "repo": repo,
        "number": number,
        "kind": kind,
        "head_sha": item.get("head_sha", "") or "",
        "options": options,
    }

    short = title if len(title) <= 70 else title[:67] + "..."
    issue_title = "[%s#%d] %s" % (repo, number, short)

    lines = []
    lines.append("## Decision needed - [%s#%d](%s)" % (repo, number, item.get("url", "")))
    lines.append("")
    meta = "**%s** by @%s" % (KIND_LABEL.get(kind, kind), item.get("author", "?"))
    if item.get("bucket"):
        meta += " &middot; `%s`" % item["bucket"]
    lines.append(meta)
    lines.append("")
    lines.append("> %s" % title)
    lines.append("")
    lines.append("### Situation")
    lines.append("- Compliance: `%s`" % item.get("comp", "n/a"))
    lines.append("- Tests: `%s`" % item.get("tests", "n/a"))
    if item.get("summary"):
        lines.append("- Notes: %s" % item["summary"])
    lines.append("")
    lines.append("### Recommended action")
    lines.append(item.get("recommendation", "Needs your call."))
    lines.append("")
    lines.append("### Your decision")
    lines.append("Tick **one** box for a quick call, or reply with a slash-command "
                 "(%s):" % SLASH_HINT.get(kind, "`/close`, `/hold`"))
    lines.append("")
    for key in options:
        label = OPTION_LABELS.get(key, key)
        lines.append("- [ ] %s <!-- opt:%s -->" % (label, key))
    lines.append("")
    lines.append("<sub>Only the repository owner can drive this decision - everyone "
                 "else's edits and comments are ignored.</sub>")
    lines.append("")
    lines.append("<!-- wheelhouse-state: %s -->" % json.dumps(state, separators=(",", ":")))
    body = "\n".join(lines)

    return {"title": issue_title, "body": body, "labels": card_labels(item),
            "marker": marker_label(item)}


# --------------------------------------------------------------------------- #
# gh card operations (ambient GH_TOKEN = default GITHUB_TOKEN)
# --------------------------------------------------------------------------- #
def _gh(args, check=True):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("gh %s failed: %s" % (" ".join(args), r.stderr.strip()))
    return r


def ensure_labels(labels):
    """Idempotently create the labels (gh issue create/edit needs them to exist)."""
    for label in labels:
        color = "ededed"
        if label == "needs-decision":
            color = "1d76db"
        elif label.startswith("priority:high"):
            color = "d93f0b"
        elif label.startswith("priority:"):
            color = "fbca04"
        elif label.startswith("kind:"):
            color = "5319e7"
        elif label.startswith("repo:"):
            color = "0e8a16"
        _gh(["label", "create", label, "--force", "--color", color], check=False)


def find_card(marker):
    r = _gh(["issue", "list", "--state", "open", "--label", marker,
             "--json", "number", "--limit", "5"])
    arr = json.loads(r.stdout or "[]")
    return arr[0]["number"] if arr else None


def upsert_card(item):
    """Create a new card, or update the existing one for this target in place.
    Returns the issue number."""
    card = render(item)
    ensure_labels(card["labels"])
    existing = find_card(card["marker"])
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(card["body"])
        body_path = f.name
    try:
        if existing:
            args = ["issue", "edit", str(existing), "--body-file", body_path]
            for label in card["labels"]:
                args += ["--add-label", label]
            _gh(args)
            print("updated card #%s for %s" % (existing, card["marker"]))
            return existing
        args = ["issue", "create", "--title", card["title"], "--body-file", body_path]
        for label in card["labels"]:
            args += ["--label", label]
        r = _gh(args)
        url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        print("created card %s for %s" % (url or "?", card["marker"]))
        return url
    finally:
        os.unlink(body_path)


def close_card(number, message, label="resolved"):
    ensure_labels([label])
    _gh(["issue", "comment", str(number), "--body", message], check=False)
    _gh(["issue", "edit", str(number), "--add-label", label,
         "--remove-label", "needs-decision"], check=False)
    _gh(["issue", "close", str(number)], check=False)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def load_item(path):
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upsert")
    up.add_argument("--item-file", required=True)

    rd = sub.add_parser("render")
    rd.add_argument("--item-file", required=True)
    rd.add_argument("--out-dir", required=True)

    args = ap.parse_args()
    item = load_item(args.item_file)

    if args.cmd == "upsert":
        upsert_card(item)
    elif args.cmd == "render":
        card = render(item)
        os.makedirs(args.out_dir, exist_ok=True)
        with open(os.path.join(args.out_dir, "title"), "w") as f:
            f.write(card["title"])
        with open(os.path.join(args.out_dir, "body.md"), "w") as f:
            f.write(card["body"])
        with open(os.path.join(args.out_dir, "labels"), "w") as f:
            f.write("\n".join(card["labels"]))
        with open(os.path.join(args.out_dir, "marker"), "w") as f:
            f.write(card["marker"])
        print(card["title"])


if __name__ == "__main__":
    main()
