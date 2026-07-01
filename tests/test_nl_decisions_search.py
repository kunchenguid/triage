#!/usr/bin/env python3
"""
Offline wiring checks for the natural-language decision agent's optional
READONLY_TOKEN search capability. NO network, NO live LLM.

Run: python tests/test_nl_decisions_search.py   (needs PyYAML)
"""

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


def load_workflow():
    return yaml.safe_load(read(".github", "workflows", "decision-handler.yml"))


def handle_steps():
    return load_workflow()["jobs"]["handle"]["steps"]


def step_by_id(steps, step_id):
    return next((s for s in steps if s.get("id") == step_id), None)


def step_by_name(steps, name):
    return next((s for s in steps if s.get("name") == name), None)


def claude_steps(steps):
    return [s for s in steps if "claude-code-action" in str(s.get("uses", ""))]


def test_readonly_gate_and_prompt_gating():
    steps = handle_steps()
    gate = step_by_id(steps, "nl-readonly")
    prompt = step_by_id(steps, "nl-prompt")

    check("workflow: nl-readonly gate step exists", gate is not None)
    if gate:
        env = gate.get("env", {})
        run = str(gate.get("run", ""))
        check(
            "workflow: nl-readonly compares the optional READONLY_TOKEN secret",
            env.get("HAS_READONLY_TOKEN") == "${{ secrets.READONLY_TOKEN != '' }}",
        )
        check(
            "workflow: nl-readonly emits an enabled output",
            'echo "enabled=$HAS_READONLY_TOKEN"' in run
            and "$GITHUB_OUTPUT" in run,
        )

    check("workflow: nl-prompt step exists", prompt is not None)
    if prompt:
        env = prompt.get("env", {})
        check(
            "workflow: prompt search language is gated on nl-readonly output",
            env.get("READONLY_SEARCH_ENABLED")
            == "${{ steps.nl-readonly.outputs.enabled }}",
        )


def test_claude_steps_split_legacy_vs_search():
    steps = handle_steps()
    llm_steps = claude_steps(steps)
    legacy = step_by_name(steps, "Claude interprets intent")
    search = step_by_name(steps, "Claude interprets intent (read-only search)")

    check("workflow: two mutually exclusive Claude steps exist", len(llm_steps) == 2)
    check("workflow: legacy no-search Claude step exists", legacy is not None)
    check("workflow: read-only search Claude step exists", search is not None)

    if legacy:
        dumped = yaml.safe_dump(legacy)
        args = str((legacy.get("with") or {}).get("claude_args", "")).strip()
        check(
            "workflow: legacy step is the byte-for-byte no-shell tool mode",
            args == "--allowedTools Write\n--max-turns 6",
        )
        check(
            "workflow: legacy step has no GH_TOKEN env",
            "env" not in legacy or "GH_TOKEN" not in (legacy.get("env") or {}),
        )
        check(
            "workflow: legacy step keeps the default action github_token",
            (legacy.get("with") or {}).get("github_token") == "${{ github.token }}",
        )
        check(
            "workflow: legacy step never receives FLEET_TOKEN or READONLY_TOKEN",
            "FLEET_TOKEN" not in dumped and "READONLY_TOKEN" not in dumped,
        )
        check(
            "workflow: legacy step runs only when readonly search is disabled",
            "steps.nl-readonly.outputs.enabled != 'true'" in str(legacy.get("if", "")),
        )

    if search:
        dumped = yaml.safe_dump(search)
        env = search.get("env", {})
        args = str((search.get("with") or {}).get("claude_args", ""))
        check(
            "workflow: search step exposes READONLY_TOKEN as GH_TOKEN",
            env.get("GH_TOKEN") == "${{ secrets.READONLY_TOKEN }}",
        )
        check(
            "workflow: search step uses READONLY_TOKEN as the action github_token",
            (search.get("with") or {}).get("github_token")
            == "${{ secrets.READONLY_TOKEN }}",
        )
        check(
            "workflow: search step does not receive the default write token",
            "${{ github.token }}" not in dumped,
        )
        check(
            "workflow: search step never receives FLEET_TOKEN",
            "FLEET_TOKEN" not in dumped,
        )
        check(
            "workflow: search step runs only when readonly search is enabled",
            "steps.nl-readonly.outputs.enabled == 'true'" in str(search.get("if", "")),
        )
        for pattern in (
            "Write",
            "Bash(gh pr list *)",
            "Bash(gh pr view *)",
            "Bash(gh pr diff *)",
            "Bash(gh issue list *)",
            "Bash(gh issue view *)",
            "Bash(gh search *)",
            "Bash(gh api repos/*)",
            "Bash(gh api /repos/*)",
            "Bash(gh api search/*)",
            "Bash(gh api /search/*)",
            "Bash(git clone https://github.com/*)",
            "Bash(git log *)",
            "Bash(git show *)",
            "Bash(git grep *)",
            "Bash(git -C * log *)",
            "Bash(git -C * show *)",
            "Bash(git -C * grep *)",
        ):
            check("workflow: search step allows %s" % pattern, pattern in args)
        for forbidden in (
            "FLEET_TOKEN",
            "gh pr merge",
            "gh issue close",
            "gh workflow run",
            "git push",
            "git commit",
        ):
            check("workflow: search step does not allow %s" % forbidden, forbidden not in args)


def test_route_and_execute_stay_deterministic():
    steps = handle_steps()
    route = step_by_id(steps, "route")
    execute = step_by_id(steps, "execute")

    check("workflow: nl-route step still exists", route is not None)
    if route:
        dumped = yaml.safe_dump(route)
        check(
            "workflow: nl-route still runs the deterministic trust boundary",
            str(route.get("run", "")).strip() == "python scripts/apply_decision.py nl-route",
        )
        check(
            "workflow: nl-route does not receive READONLY_TOKEN or FLEET_TOKEN",
            "READONLY_TOKEN" not in dumped and "FLEET_TOKEN" not in dumped,
        )

    check("workflow: execute step still exists", execute is not None)
    if execute:
        dumped = yaml.safe_dump(execute)
        env = execute.get("env", {})
        check(
            "workflow: execute still acts under FLEET_TOKEN",
            env.get("GH_TOKEN") == "${{ secrets.FLEET_TOKEN }}",
        )
        check(
            "workflow: execute never receives READONLY_TOKEN",
            "READONLY_TOKEN" not in dumped,
        )
        check(
            "workflow: execute script is unchanged",
            str(execute.get("run", "")).strip() == "python scripts/apply_decision.py execute",
        )


def main():
    test_readonly_gate_and_prompt_gating()
    test_claude_steps_split_legacy_vs_search()
    test_route_and_execute_stay_deterministic()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all nl-decisions search tests passed")


if __name__ == "__main__":
    main()
