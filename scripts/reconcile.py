#!/usr/bin/env python3
"""
Wheelhouse - backstop reconciler.

The safety net behind the event-driven `ingest` path. Given a fresh scan of the
fleet (scan.json) and the current open cards in THIS repo (cards.json), it:

  * opens a decision card for any worklist item that has no open card, and
  * closes any open card whose underlying PR/issue is no longer open
    (merged/closed/resolved) - so the queue self-heals even if a dispatch was
    lost.

Both card operations run against THIS repo via the ambient GH_TOKEN, which the
workflow sets to the default GITHUB_TOKEN (card activity must not re-trigger the
handler).

Usage:
  reconcile.py scan.json cards.json

cards.json is `gh issue list --state open --json number,body,labels,title`.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wheelhouse_core as core  # noqa: E402
import render_card  # noqa: E402

PR_KINDS = {"pr-review", "ci-approval"}


def load(path):
    with open(path) as f:
        return json.load(f)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: reconcile.py scan.json cards.json")
    scan = load(sys.argv[1])
    cards = load(sys.argv[2])

    repos = scan.get("repos", {})
    items = scan.get("items", [])

    # Index existing open cards by their target (repo, number) from the state block.
    existing = {}            # (repo, number) -> card number
    cards_with_state = []    # (card_number, state)
    for card in cards:
        state = core.parse_state_block(card.get("body", ""))
        if not state:
            continue  # a manually-created issue with no card state; leave it alone
        key = (state.get("repo"), int(state.get("number", 0)))
        existing[key] = card["number"]
        cards_with_state.append((card["number"], state))

    # 1) Create cards for new items that have no open card.
    created = 0
    for item in items:
        key = (item["repo"], int(item["number"]))
        if key in existing:
            continue
        try:
            render_card.upsert_card(item)
            created += 1
        except Exception as e:  # one bad item must not abort the whole pass
            print("::warning::failed to create card for %s#%s: %s"
                  % (item["repo"], item["number"], str(e)[:160]))

    # 2) Close cards whose target is no longer open. Skip repos that failed to
    #    scan (ok:false) - we don't know their state, so we must not close.
    closed = 0
    for card_number, state in cards_with_state:
        repo = state.get("repo")
        r = repos.get(repo)
        if not r or not r.get("ok"):
            continue
        number = int(state.get("number", 0))
        kind = state.get("kind", "pr-review")
        open_set = set(r.get("open_pr_numbers", []) if kind in PR_KINDS
                       else r.get("open_issue_numbers", []))
        if number in open_set:
            continue  # still open and (for PRs) still in the live set - leave card
        msg = ("Self-healed by the scheduled backstop: %s#%s is no longer open "
               "(merged/closed) - consuming this card." % (repo, number))
        try:
            render_card.close_card(card_number, msg)
            closed += 1
        except Exception as e:
            print("::warning::failed to close card #%s: %s" % (card_number, str(e)[:160]))

    print("reconcile: %d card(s) created, %d card(s) closed" % (created, closed))


if __name__ == "__main__":
    main()
