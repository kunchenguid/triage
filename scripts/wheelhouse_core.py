#!/usr/bin/env python3
"""
Wheelhouse - deterministic brain (ported from the local OSS-triage machinery).

Runs inside GitHub Actions. One GraphQL query per repo fetches every open
PR/issue with compliance + test status, classifies each deterministically, and
emits a worklist of items that need the maintainer's decision. Also carries the
security-gated CI approval (the fork-CI / pwn-request HOLD).

This is the GHA port of `data/triage/triage.py`. What the Actions model
replaces has been dropped: the local single-flight lock (-> Actions
`concurrency`), the lavish board and nudge-ledger (-> issues/labels/comments as
state), per-repo `owner` (-> derived from github.repository_owner).

Usage:
  wheelhouse_core.py scan                 scan all configured repos -> JSON worklist on stdout
  wheelhouse_core.py scan <repo>          scan a single configured repo
  wheelhouse_core.py approve-ci <repo> <pr>   security-gated fork-CI approval (exit 4 = HOLD)
  wheelhouse_core.py checks <repo>        list distinct check names on a repo's PRs (onboarding)
  wheelhouse_core.py authorized           print true/false: is $SENDER allowed to drive decisions?
  wheelhouse_core.py deep-review-enabled  print true/false: is deep_review on in config?
  wheelhouse_core.py nl-decisions-enabled print true/false: is nl_decisions on in config?
  wheelhouse_core.py state <field>        print one field of the state block in $ISSUE_BODY
  wheelhouse_core.py repos                list configured repos

Owner is derived from $GITHUB_REPOSITORY_OWNER (or --owner). Cross-repo reads
use the ambient GH_TOKEN (set to FLEET_TOKEN by the calling workflow step).
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

try:
    import yaml
except ImportError:  # pragma: no cover - workflows `pip install pyyaml` first
    sys.exit("PyYAML is required (pip install pyyaml)")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Config search order: repo root, then .github/.
CONFIG_CANDIDATES = [
    os.path.join(ROOT, "wheelhouse.config.yml"),
    os.path.join(ROOT, "wheelhouse.config.yaml"),
    os.path.join(ROOT, ".github", "wheelhouse.config.yml"),
    os.path.join(ROOT, ".github", "wheelhouse.config.yaml"),
]

GQL = """
query($owner:String!, $name:String!) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:100, orderBy:{field:UPDATED_AT, direction:DESC}) {
      totalCount
      nodes {
        number title isDraft updatedAt
        author { login }
        headRefName headRefOid
        labels(first:20){ nodes{ name } }
        closingIssuesReferences(first:10){ nodes{ number } }
        commits(last:1){ nodes{ commit{ statusCheckRollup{
          state
          contexts(first:100){ nodes{
            __typename
            ... on CheckRun { name conclusion status }
            ... on StatusContext { context state }
          }}
        }}}}
      }
    }
    issues(states:OPEN, first:100, orderBy:{field:UPDATED_AT, direction:DESC}) {
      totalCount
      nodes { number title updatedAt author{login} labels(first:20){nodes{name}} }
    }
  }
}
"""

# Buckets that need the maintainer's call vs. ones waiting on the contributor.
NEEDS_MAINTAINER = {"merge-ready", "needs-ci-approval", "review-needed"}
# (waiting-on-contributor: needs-reraise, fix-tests, draft, ci-running)

# Decision-card "kind" per PR bucket.
PR_KIND = {
    "merge-ready": "pr-review",
    "review-needed": "pr-review",
    "needs-ci-approval": "ci-approval",
}

PRIORITY = {
    "merge-ready": "med",
    "needs-ci-approval": "med",
    "review-needed": "low",
    "issue-triage": "low",
}


# --------------------------------------------------------------------------- #
# config + owner
# --------------------------------------------------------------------------- #
def config_path():
    for p in CONFIG_CANDIDATES:
        if os.path.exists(p):
            return p
    sys.exit("no wheelhouse.config.yml found (looked in repo root and .github/)")


def load_config():
    with open(config_path()) as f:
        cfg = yaml.safe_load(f) or {}
    repos = cfg.get("repos") or []
    by_name = {}
    for r in repos:
        if isinstance(r, dict) and r.get("name"):
            by_name[r["name"]] = r
    return {
        "repos": by_name,
        "maintainer": (cfg.get("maintainer") or "").strip(),
        "deep_review": bool(cfg.get("deep_review", False)),
        "nl_decisions": bool(cfg.get("nl_decisions", False)),
        "card_issues": bool(cfg.get("card_issues", False)),
    }


def get_owner():
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    if not owner:
        sys.exit("owner not set (GITHUB_REPOSITORY_OWNER missing)")
    return owner


# --------------------------------------------------------------------------- #
# gh wrappers (ambient GH_TOKEN, set per-step by the workflow)
# --------------------------------------------------------------------------- #
def gh_graphql(owner, name):
    r = subprocess.run(
        ["gh", "api", "graphql", "-f", "query=" + GQL, "-f", "owner=" + owner, "-f", "name=" + name],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "gh graphql failed")
    data = json.loads(r.stdout)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"]))
    return data["data"]["repository"]


def gh_rest(path, method=None, fields=None, jq=None, paginate=False):
    cmd = ["gh", "api"]
    if method:
        cmd += ["--method", method]
    if paginate:
        cmd += ["--paginate"]
    cmd += [path]
    for k, v in (fields or {}).items():
        cmd += ["-f", "%s=%s" % (k, v)]
    if jq:
        cmd += ["--jq", jq]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("gh api %s failed: %s" % (path, r.stderr.strip()))
    out = r.stdout.strip()
    if not out:
        return None
    if jq:
        return out
    return json.loads(out) if out[:1] in ("{", "[") else out


# --------------------------------------------------------------------------- #
# classification (ported)
# --------------------------------------------------------------------------- #
def check_status(pr, cfg):
    """Return (compliance, tests, ci_present, names).

    compliance in pass/fail/pending/missing/n/a/none; tests in green/fail/pending/none.
    """
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    if not rollup or not rollup["contexts"]["nodes"]:
        return ("none", "none", False, [])
    comp_name = cfg.get("compliance_check")
    patterns = cfg.get("test_check_patterns", []) or []
    compliance = "missing" if comp_name else "n/a"
    tests = []
    names = []
    for c in rollup["contexts"]["nodes"]:
        if c["__typename"] == "CheckRun":
            name = c.get("name") or ""
            names.append(name)
            concl = (c.get("conclusion") or "").upper()
            status = (c.get("status") or "").upper()
            done = status == "COMPLETED" or status == ""
            if comp_name and name == comp_name:
                compliance = ("pass" if concl == "SUCCESS"
                              else "fail" if concl in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE")
                              else "pending")
            elif any(p in name for p in patterns):
                tests.append("pass" if (done and concl == "SUCCESS")
                             else "fail" if (done and concl in ("FAILURE", "TIMED_OUT", "CANCELLED"))
                             else "pending")
        else:  # StatusContext
            ctx = c.get("context") or ""
            names.append(ctx)
            st = (c.get("state") or "").upper()
            if comp_name and ctx == comp_name:
                compliance = "pass" if st == "SUCCESS" else "fail" if st in ("FAILURE", "ERROR") else "pending"
            elif any(p in ctx for p in patterns):
                tests.append("pass" if st == "SUCCESS" else "pending" if st == "PENDING" else "fail")
    if not tests:
        tstate = "none"
    elif "fail" in tests:
        tstate = "fail"
    elif "pending" in tests:
        tstate = "pending"
    else:
        tstate = "green"
    return (compliance, tstate, True, names)


def classify(draft, comp, tests, ci):
    if draft:
        return "draft"
    if not ci:
        return "needs-ci-approval"
    if comp == "fail":
        return "needs-reraise"
    if comp == "pending":
        return "ci-running"
    if comp in ("pass", "n/a"):
        if tests == "green":
            return "merge-ready"
        if tests == "fail":
            return "fix-tests"
        if tests == "pending":
            return "ci-running"
        if tests == "none":
            return "review-needed"  # compliant but no test signal - look before trusting
    return "review-needed"  # comp missing-but-ci-present, or anything unmodeled


def config_warning(repo, comp, names):
    """Catch the most dangerous misconfig: a gate-like check exists but
    compliance_check is unset/wrong, which would silently show non-compliant
    PRs as merge-ready."""
    if comp and comp not in names:
        return ("compliance_check %r not seen in any PR check on %s - misconfigured? "
                "(run: checks %s)" % (comp, repo, repo))
    if not comp:
        # Generic, owner-agnostic gate-like check name heuristics.
        gate_terms = ("must be raised", "policy", "dco", "cla", "sign-off",
                      "signoff", "contribut", "compliance", "required")
        gateish = [n for n in names if any(t in n.lower() for t in gate_terms)]
        if gateish:
            return ("no compliance_check set on %s but a gate-like check exists (%r) - "
                    "non-compliant PRs may show as merge-ready" % (repo, gateish[0]))
    return None


# --------------------------------------------------------------------------- #
# worklist item rendering helpers
# --------------------------------------------------------------------------- #
def _overlap_note(number, closes, dup_clusters, addressed):
    notes = []
    for issue in closes:
        sibs = dup_clusters.get(issue)
        if sibs and len(sibs) > 1:
            others = [n for n in sibs if n != number]
            if others:
                notes.append("overlaps PR(s) %s (all close issue #%d)"
                             % (", ".join("#%d" % n for n in sorted(others)), issue))
    return "; ".join(notes)


def _recommendation(bucket):
    return {
        "merge-ready": "Merge - compliance and tests are green.",
        "review-needed": "Review before merge - compliant but the test signal is missing/unclear.",
        "needs-ci-approval": "Approve CI to get a test signal (security-gated; held automatically if the PR touches CI/action files).",
        "issue-triage": "Triage - open issue with no linked PR yet.",
    }.get(bucket, "Needs your call.")


def build_repo(owner, repo_cfg, card_issues):
    """Scan one repo. Returns (repo_result, items)."""
    name = repo_cfg["name"]
    slug = "%s/%s" % (owner, name)
    try:
        data = gh_graphql(owner, name)
    except Exception as e:  # resilient: a missing/unreadable repo does not abort the scan
        return ({"name": name, "ok": False, "warning": "scan failed: %s" % str(e)[:200],
                 "open_pr_numbers": [], "open_issue_numbers": []}, [])

    prs = data["pullRequests"]["nodes"]
    issues = data["issues"]["nodes"]
    all_names = set()
    enriched = []
    closing = {}  # issue -> [pr numbers]
    for pr in prs:
        comp, tests, ci, names = check_status(pr, repo_cfg)
        all_names.update(names)
        bucket = classify(pr["isDraft"], comp, tests, ci)
        closes = [i["number"] for i in pr["closingIssuesReferences"]["nodes"]]
        for i in closes:
            closing.setdefault(i, []).append(pr["number"])
        enriched.append({
            "number": pr["number"], "title": pr["title"],
            "author": (pr.get("author") or {}).get("login", "?"),
            "comp": comp, "tests": tests, "ci": ci, "bucket": bucket,
            "closes": closes, "head_sha": pr["headRefOid"],
        })

    open_issue_numbers = [it["number"] for it in issues]
    addressed = {n for n in closing if n in set(open_issue_numbers)}

    items = []
    for pr in enriched:
        if pr["bucket"] not in NEEDS_MAINTAINER:
            continue
        kind = PR_KIND[pr["bucket"]]
        overlap = _overlap_note(pr["number"], pr["closes"], closing, addressed)
        priority = "high" if overlap else PRIORITY.get(pr["bucket"], "low")
        summary = "compliance=%s tests=%s" % (pr["comp"], pr["tests"])
        if overlap:
            summary += "; " + overlap
        items.append({
            "repo": name, "number": pr["number"], "kind": kind,
            "head_sha": pr["head_sha"], "title": pr["title"], "author": pr["author"],
            "bucket": pr["bucket"], "comp": pr["comp"], "tests": pr["tests"],
            "url": "https://github.com/%s/pull/%d" % (slug, pr["number"]),
            "summary": summary, "recommendation": _recommendation(pr["bucket"]),
            "priority": priority,
        })

    if card_issues:
        for it in issues:
            if it["number"] in addressed:
                continue  # an open PR is already on it
            items.append({
                "repo": name, "number": it["number"], "kind": "issue-triage",
                "head_sha": "", "title": it["title"],
                "author": (it.get("author") or {}).get("login", "?"),
                "bucket": "issue-triage", "comp": "n/a", "tests": "n/a",
                "url": "https://github.com/%s/issues/%d" % (slug, it["number"]),
                "summary": "open issue, no linked PR",
                "recommendation": _recommendation("issue-triage"),
                "priority": PRIORITY["issue-triage"],
            })

    warning = config_warning(name, repo_cfg.get("compliance_check"), sorted(all_names))
    result = {
        "name": name, "ok": True,
        "open_pr_numbers": [p["number"] for p in enriched],
        "open_issue_numbers": open_issue_numbers,
        "truncated": data["pullRequests"]["totalCount"] > len(prs)
        or data["issues"]["totalCount"] > len(issues),
        "warning": warning,
    }
    return (result, items)


# --------------------------------------------------------------------------- #
# state block parsing (shared util)
# --------------------------------------------------------------------------- #
# Cards now WRITE `wheelhouse-state` (see render_card.py), but the legacy
# `triage-state` marker MUST keep parsing: existing open cards in a live machine
# were rendered with it, and they have to stay drivable after the rename. So the
# reader accepts BOTH; only the writer moved to the new name. (When a legacy card
# is next upserted it is re-rendered with the new marker, so the queue migrates
# itself over time.)
_STATE_RE = re.compile(r"<!--\s*(?:wheelhouse|triage)-state:\s*(\{.*?\})\s*-->", re.S)


def parse_state_block(body):
    """Extract the hidden machine-readable state from a decision-card body.

    Accepts the current `wheelhouse-state` marker and the legacy `triage-state`
    marker (back-compat for cards rendered before the rename)."""
    if not body:
        return None
    m = _STATE_RE.search(body)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# security-gated CI approval (ported exit-4 HOLD)
# --------------------------------------------------------------------------- #
def _ci_risky_files(slug, pr):
    """Files whose change makes approving fork CI dangerous: approving runs the
    PR's OWN workflow/action code (the 'pwn request' supply-chain vector).
    Fails CLOSED - if files can't be listed, treat as risky."""
    out = subprocess.run(
        ["gh", "api", "--paginate", "/repos/%s/pulls/%s/files" % (slug, pr), "--jq", ".[].filename"],
        capture_output=True, text=True)
    if out.returncode != 0:
        return ["<could-not-list-files - failing closed>"]
    risky = []
    for f in out.stdout.splitlines():
        f = f.strip()
        if (f.startswith(".github/workflows/") or f.startswith(".github/actions/")
                or f.endswith("/action.yml") or f.endswith("/action.yaml")
                or f in ("action.yml", "action.yaml")):
            risky.append(f)
    return risky


def approve_ci(owner, repo, pr):
    """Approve fork-PR workflow runs awaiting maintainer approval.

    Returns (status, message). status in:
      approved - one or more runs approved
      noop     - nothing awaiting approval
      hold     - SECURITY HOLD (PR changes CI-execution files) - NOT approved
      error    - could not act
    """
    slug = "%s/%s" % (owner, repo)
    pj = subprocess.run(["gh", "api", "/repos/%s/pulls/%s" % (slug, pr)], capture_output=True, text=True)
    if pj.returncode != 0:
        return ("error", "pr fetch failed: %s" % pj.stderr.strip()[:160])
    head_ref = json.loads(pj.stdout)["head"]["ref"]

    risky = _ci_risky_files(slug, pr)
    if risky:
        return ("hold",
                "SECURITY HOLD: #%s changes CI-execution files - NOT auto-approving. Approving fork "
                "CI would run the PR's OWN workflow/action code with repo perms. Needs manual review: %s"
                % (pr, ", ".join(risky)))

    lst = subprocess.run(
        ["gh", "run", "list", "--branch", head_ref, "--status", "action_required",
         "--limit", "30", "-R", slug, "--json", "databaseId,workflowName"],
        capture_output=True, text=True)
    runs = json.loads(lst.stdout) if lst.returncode == 0 and lst.stdout.strip() else []
    if not runs:
        return ("noop", "#%s (%s): no workflow runs awaiting approval" % (pr, head_ref))

    done = []
    for run in runs:
        rid = run["databaseId"]
        ar = subprocess.run(
            ["gh", "api", "--method", "POST", "/repos/%s/actions/runs/%s/approve" % (slug, rid)],
            capture_output=True, text=True)
        done.append("%s:%s" % (run.get("workflowName", "?"), "OK" if ar.returncode == 0 else "FAIL"))
    return ("approved", "#%s (%s): approved %d run(s) [%s]" % (pr, head_ref, len(runs), ", ".join(done)))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_scan(only_repo=None):
    owner = get_owner()
    cfg = load_config()
    repos = cfg["repos"]
    if only_repo:
        if only_repo not in repos:
            sys.exit("unknown repo '%s' (configured: %s)" % (only_repo, ", ".join(repos)))
        names = [only_repo]
    else:
        names = list(repos)

    out_repos = {}
    items = []
    for name in names:
        result, repo_items = build_repo(owner, repos[name], cfg["card_issues"])
        out_repos[name] = result
        items.extend(repo_items)
        if result.get("warning"):
            print("::warning::%s" % result["warning"], file=sys.stderr)

    payload = {
        "owner": owner,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "card_issues": cfg["card_issues"],
        "repos": out_repos,
        "items": items,
    }
    print(json.dumps(payload, indent=2))


def cmd_approve_ci(repo, pr):
    owner = get_owner()
    cfg = load_config()
    if repo not in cfg["repos"]:
        sys.exit("unknown repo '%s'" % repo)
    status, message = approve_ci(owner, repo, pr)
    print(message)
    if status == "hold":
        sys.exit(4)  # distinct exit: blocked for security review
    if status == "error":
        sys.exit(1)


def cmd_checks(repo):
    owner = get_owner()
    cfg = load_config()
    if repo not in cfg["repos"]:
        sys.exit("unknown repo '%s'" % repo)
    rc = cfg["repos"][repo]
    data = gh_graphql(owner, rc["name"])
    comp = rc.get("compliance_check")
    pats = rc.get("test_check_patterns", []) or []
    names = set()
    for pr in data["pullRequests"]["nodes"]:
        _, _, _, n = check_status(pr, rc)
        names.update(n)
    print("check names on %s (compliance_check=%r):" % (repo, comp))
    for n in sorted(names):
        tag = "  <- COMPLIANCE" if (comp and n == comp) else ("  <- test" if any(p in n for p in pats) else "")
        print("  %s%s" % (n, tag))
    w = config_warning(repo, comp, sorted(names))
    if w:
        print("!! " + w)


def maintainers():
    """The set of logins allowed to drive decisions: the repo owner (from
    $OWNER / $GITHUB_REPOSITORY_OWNER) plus the optional configured `maintainer`.

    This is the SINGLE source of truth for "who is the maintainer" - the gate
    (`authorized`) and the natural-language conversation-history filter both use
    it, so trusted-author rules never drift apart."""
    owner = (os.environ.get("OWNER", "") or os.environ.get("GITHUB_REPOSITORY_OWNER", "")).strip()
    maintainer = ""
    try:
        maintainer = load_config()["maintainer"]
    except SystemExit:
        pass
    return {x for x in (owner, maintainer) if x}


def cmd_authorized():
    """Print true/false: may $SENDER drive decisions on this machine?"""
    sender = os.environ.get("SENDER", "").strip()
    print("true" if sender and sender in maintainers() else "false")


def cmd_repos():
    cfg = load_config()
    for name, rc in cfg["repos"].items():
        print("%-20s gate=%s tests=%s"
              % (name, rc.get("compliance_check"), rc.get("test_check_patterns")))


def cmd_deep_review_enabled():
    print("true" if load_config()["deep_review"] else "false")


def cmd_nl_decisions_enabled():
    print("true" if load_config()["nl_decisions"] else "false")


def cmd_state(field):
    """Print one field of the state block in $ISSUE_BODY (for the deep-review workflow)."""
    st = parse_state_block(os.environ.get("ISSUE_BODY", ""))
    print((st or {}).get(field, ""))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "scan":
        cmd_scan(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "approve-ci" and len(sys.argv) == 4:
        cmd_approve_ci(sys.argv[2], sys.argv[3])
    elif cmd == "checks" and len(sys.argv) == 3:
        cmd_checks(sys.argv[2])
    elif cmd == "authorized":
        cmd_authorized()
    elif cmd == "deep-review-enabled":
        cmd_deep_review_enabled()
    elif cmd == "nl-decisions-enabled":
        cmd_nl_decisions_enabled()
    elif cmd == "state" and len(sys.argv) == 3:
        cmd_state(sys.argv[2])
    elif cmd == "repos":
        cmd_repos()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
