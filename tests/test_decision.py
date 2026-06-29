#!/usr/bin/env python3
"""
Unit-exercise the decision parse/route logic with NO network and a MOCKED LLM.

Run: python tests/test_decision.py   (stdlib only; exits non-zero on failure)

Covers:
  * the checkbox path now consumes issue-ops/parser `{selected, unselected}`
    JSON (for the new + old card body) and keeps "exactly one newly-ticked";
  * the natural-language structured-intent contract: an `action` result drives
    the deterministic executor, while `answer`/`clarify` only reply and leave
    the card open - i.e. `execute` runs ONLY for `action` mode;
  * the trust boundary: an action outside the per-kind allowlist, or a
    malformed/empty LLM result, falls back to a clarify reply (no action).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import apply_decision as ad  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


# --------------------------------------------------------------------------- #
# checkbox path: issue-ops/parser JSON -> deterministic key diff
# --------------------------------------------------------------------------- #
def parser_json(*checked):
    """Mimic issue-ops/parser `json` output for our card. The parser strips only
    the `- [x] ` prefix, so each selected entry keeps its `<!-- opt:KEY -->`."""
    labels = {
        "merge": "Merge it <!-- opt:merge -->",
        "close": "Close / decline <!-- opt:close -->",
        "hold": "Hold - I'll handle this manually <!-- opt:hold -->",
    }
    selected = [labels[k] for k in checked]
    unselected = [labels[k] for k in labels if k not in checked]
    # plus the noise lines the parser also sweeps into `unselected`
    unselected += ["Tick **one** box ...", "<!-- triage-state: {\"options\":[]} -->"]
    return json.dumps({"decision": {"selected": selected, "unselected": unselected}})


OPTS = ["merge", "close", "hold"]


def test_checkbox_diff():
    none, merge, merge_hold = parser_json(), parser_json("merge"), parser_json("merge", "hold")
    check("checkbox: one newly-ticked -> that key",
          ad.diff_checkbox(none, merge, OPTS) == "merge")
    check("checkbox: no change -> no-op",
          ad.diff_checkbox(merge, merge, OPTS) is None)
    check("checkbox: two newly-ticked -> ambiguous no-op",
          ad.diff_checkbox(none, merge_hold, OPTS) is None)
    check("checkbox: untick -> no-op",
          ad.diff_checkbox(merge, none, OPTS) is None)
    check("checkbox: empty/missing parser json -> no-op",
          ad.diff_checkbox("", "", OPTS) is None)
    check("checkbox: a key not in this card's options is ignored",
          ad.diff_checkbox(parser_json(), parser_json("merge"), ["close", "hold"]) is None)


# --------------------------------------------------------------------------- #
# natural-language path: mocked LLM result -> validated, deterministic outputs
# --------------------------------------------------------------------------- #
STATE = {"repo": "lavish-axi", "number": 42, "kind": "pr-review",
         "head_sha": "deadbeefcafe"}


def route(result, kind="pr-review"):
    return ad.route_decision(result, kind, STATE)


def test_action_mode_drives_execute():
    r = route({"mode": "action", "action": "merge"})
    check("action: mode preserved", r["mode"] == "action")
    check("action: decision set (this is what runs execute)", r["decision"] == "merge")
    check("action: target carried from state block",
          r["target_repo"] == "lavish-axi" and str(r["target_number"]) == "42"
          and r["head_sha"] == "deadbeefcafe")

    r = route({"mode": "action", "action": "decline", "free_text": "wrong approach"})
    check("action: decline keeps free_text", r["decision"] == "decline" and r["free_text"] == "wrong approach")

    r = route({"mode": "action", "action": "decline"})
    check("action: decline defaults a reason", r["decision"] == "decline" and r["free_text"])


def test_answer_and_clarify_do_not_execute():
    r = route({"mode": "answer", "answer": "It rebases cleanly because X."})
    check("answer: mode preserved", r["mode"] == "answer")
    check("answer: NO decision -> execute never runs", r["decision"] == "")
    check("answer: reply carried", "rebases" in r["answer"])

    r = route({"mode": "clarify", "answer": "Do you mean merge or close?"})
    check("clarify: mode preserved", r["mode"] == "clarify")
    check("clarify: NO decision -> execute never runs", r["decision"] == "")
    check("clarify: question carried", "merge or close" in r["answer"])


def test_trust_boundary():
    # An action the kind does not allow must NOT execute - downgraded to clarify.
    r = route({"mode": "action", "action": "merge"}, kind="issue-triage")
    check("guard: disallowed action -> no decision", r["decision"] == "")
    check("guard: disallowed action -> clarify reply", r["mode"] == "clarify" and r["answer"])

    # A made-up verb the LLM might hallucinate is rejected too.
    r = route({"mode": "action", "action": "rm -rf"})
    check("guard: unknown verb -> no decision", r["decision"] == "" and r["mode"] == "clarify")

    # comment with no text -> clarify (nothing to post).
    r = route({"mode": "action", "action": "comment"})
    check("guard: comment without text -> no decision", r["decision"] == "" and r["mode"] == "clarify")

    # Malformed / empty results never silently no-op: they ask the owner.
    for bad in (None, {}, {"mode": "banana"}, "not a dict"):
        r = route(bad)
        check("guard: malformed %r -> clarify, no decision" % (bad,),
              r["decision"] == "" and r["mode"] == "clarify" and bool(r["answer"]))


def test_load_llm_result_tolerant():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "decision.json")
        with open(p, "w") as f:
            f.write('```json\n{"mode":"action","action":"merge"}\n```\n')
        obj = ad._load_llm_result(p)
        check("load: extracts JSON from code fences", obj == {"mode": "action", "action": "merge"})
        check("load: missing file -> None", ad._load_llm_result(os.path.join(d, "nope.json")) is None)
        with open(p, "w") as f:
            f.write("")
        check("load: empty file -> None", ad._load_llm_result(p) is None)


def main():
    test_checkbox_diff()
    test_action_mode_drives_execute()
    test_answer_and_clarify_do_not_execute()
    test_trust_boundary()
    test_load_llm_result_tolerant()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all decision tests passed")


if __name__ == "__main__":
    main()
