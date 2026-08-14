#!/usr/bin/env python3
"""GitHub stats for the CodingAssistant GitHub pages (PAGE_GITHUB / PAGE_GITHUB_DETAIL).

Reads the org's pulse through the GitHub API using the token the `gh` CLI
already holds in the Windows keyring (`gh auth token`) - the daemon owns no
GitHub credential of its own and never refreshes one. One fetch collects:

  - the viewer's contribution calendar (the green-squares heatmap)
  - open PR counts, split human vs Dependabot, plus review requests
    (one GraphQL call - the 30/min search REST budget is never touched)
  - org-wide Dependabot, secret-scanning and code-scanning alert counts
  - which repos failed the org's code-security configuration attachment

Security posture, non-negotiable (see plan.md B1): the secret-scanning API
returns committed secret VALUES in plaintext. This module extracts only
counts, repo names, types and severities - `parse_secret_alerts` whitelists
fields and nothing here ever logs a response body or the token. Tests in
tests/test_github_stats.py assert both.

Integration (claude_usage_daemon_windows.py):
    reg.add(SourceCache("github", fetch_github_stats, interval=300.0,
                        timeout=25.0, log=log))
and in build_page_payload:
    elif page == PAGE_GITHUB:
        stats = SOURCES.value("github", max_age=MAX_SOURCE_AGE) if SOURCES else None
        return to_overview_wire(stats), False
    elif page == PAGE_GITHUB_DETAIL:
        stats = SOURCES.value("github", max_age=MAX_SOURCE_AGE) if SOURCES else None
        return to_detail_wire(stats), False
"""
from __future__ import annotations

import json
import subprocess
import sys

# Mirror of the PAGE_* enum in firmware/src/data.h.
PAGE_GITHUB = 6
PAGE_GITHUB_DETAIL = 7
PAGE_GH_PRS = 10

API = "https://api.github.com"


def get_github_org() -> str | None:
    """The org the GitHub pages report on. Env GITHUB_ORG, else `org=` in the
    daemon config file. No default: this is per-install configuration, and a
    missing org answers err:no_org so the device pages prune themselves."""
    import os
    from pathlib import Path
    if org := os.environ.get("GITHUB_ORG"):
        return org
    local_appdata = Path(os.environ.get("LOCALAPPDATA",
                                        Path.home() / "AppData" / "Local"))
    config_file = local_appdata / "CodingAssistant" / "config"
    if config_file.exists():
        try:
            for line in config_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("org="):
                    return line.split("=", 1)[1].strip() or None
        except OSError:
            pass
    return None

# The profile calendar shows 53 columns; we send 52, dropping only the
# oldest (a year ago, and usually a partial week), which keeps the absurd
# worst case (every counter five digits) under the 480-byte wire guard --
# test-asserted. The current week is always the last column, as on github.com.
HEATMAP_WEEKS = 52

# Detail rows are pre-rendered here so the firmware stays a dumb renderer:
# fixed row count and width, matching char rows[GH_DETAIL_ROWS][GH_DETAIL_COLS]
# in firmware/src/data.h.
DETAIL_MAX_ROWS = 4
DETAIL_MAX_CHARS = 52

_LEVELS = {
    "NONE": "0",
    "FIRST_QUARTILE": "1",
    "SECOND_QUARTILE": "2",
    "THIRD_QUARTILE": "3",
    "FOURTH_QUARTILE": "4",
}

_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "moderate": 2, "low": 3}

_GRAPHQL_QUERY = """
query {
  viewer {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { contributionLevel } }
      }
    }
  }
  prsAll:  search(query: "org:%(org)s is:pr is:open", type: ISSUE) { issueCount }
  prsBots: search(query: "org:%(org)s is:pr is:open author:app/dependabot", type: ISSUE) { issueCount }
  reviews: search(query: "is:pr is:open review-requested:@me", type: ISSUE) { issueCount }
  prsOldest: search(query: "org:%(org)s is:pr is:open -author:app/dependabot sort:created-asc",
                    type: ISSUE, first: 8) {
    nodes { ... on PullRequest { number title createdAt repository { name } } }
  }
}
"""


def _graphql_query(org: str) -> str:
    return _GRAPHQL_QUERY % {"org": org}

_cached_token: str | None = None


def _gh_auth_token() -> str | None:
    """Read the gh CLI's stored token. Never logged, never persisted."""
    global _cached_token
    if _cached_token:
        return _cached_token
    creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=10,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = out.stdout.strip()
    if out.returncode != 0 or not token:
        return None
    _cached_token = token
    return token


def _drop_cached_token() -> None:
    global _cached_token
    _cached_token = None


# ---- pure parsers (unit-tested against canned API JSON) --------------------

def parse_calendar(gql_data: dict) -> tuple[str, int]:
    """GraphQL response -> (heatmap digit string, total contributions).

    One digit 0-4 per day, oldest first, trimmed to the last HEATMAP_WEEKS
    whole-week columns plus the current partial week.
    """
    cal = gql_data["viewer"]["contributionsCollection"]["contributionCalendar"]
    weeks = cal["weeks"][-HEATMAP_WEEKS:]
    digits = []
    for w in weeks:
        for d in w["contributionDays"]:
            digits.append(_LEVELS.get(d["contributionLevel"], "0"))
    return "".join(digits), int(cal["totalContributions"])


def parse_pr_counts(gql_data: dict) -> tuple[int, int, int]:
    """GraphQL response -> (open human PRs, open bot PRs, review requests)."""
    total = int(gql_data["prsAll"]["issueCount"])
    bots = int(gql_data["prsBots"]["issueCount"])
    reviews = int(gql_data["reviews"]["issueCount"])
    return max(0, total - bots), bots, reviews


def parse_pr_rows(gql_data: dict) -> list[str]:
    """GraphQL prsOldest nodes -> pre-rendered device rows, oldest first.

    "repo#123 47d Title.." - the age is what makes an oldest-first list
    actionable. ASCII only; clipped to the detail-row width.
    """
    import datetime as _dt
    rows = []
    now = _dt.datetime.now(_dt.timezone.utc)
    for n in (gql_data.get("prsOldest") or {}).get("nodes") or []:
        if not n:
            continue
        repo = ((n.get("repository") or {}).get("name") or "?")[:16]
        title = str(n.get("title") or "").encode("ascii", "replace").decode()
        age = ""
        try:
            created = _dt.datetime.fromisoformat(
                str(n.get("createdAt", "")).replace("Z", "+00:00"))
            days = max(0, (now - created).days)
            age = f"{days}d" if days < 365 else f"{days // 365}y"
        except ValueError:
            pass
        row = f"{repo}#{n.get('number', 0)} {age} {title}"
        rows.append(row if len(row) <= 50 else row[:48] + "..")
    return rows


def parse_dependabot_alerts(alerts: list) -> list[dict]:
    """Whitelist: repo, severity, package. Sorted worst first."""
    rows = []
    for a in alerts:
        adv = a.get("security_advisory") or {}
        dep = (a.get("dependency") or {}).get("package") or {}
        rows.append({
            "repo": ((a.get("repository") or {}).get("name")) or "?",
            "severity": (adv.get("severity") or "low").lower(),
            "package": dep.get("name") or "?",
        })
    rows.sort(key=lambda r: _SEV_RANK.get(r["severity"], 9))
    return rows


def parse_secret_alerts(alerts: list) -> list[dict]:
    """Whitelist: repo and type display name ONLY.

    The raw alert carries the committed secret value in plaintext under
    `.secret`; it must never leave this function. Only the two whitelisted
    fields are copied out - the raw list is not stored or logged.
    """
    rows = []
    for a in alerts:
        rows.append({
            "repo": ((a.get("repository") or {}).get("name")) or "?",
            "type": a.get("secret_type_display_name") or a.get("secret_type") or "secret",
        })
    return rows


def parse_scan_alerts(alerts: list) -> list[dict]:
    """Whitelist: repo and rule id."""
    rows = []
    for a in alerts:
        rows.append({
            "repo": ((a.get("repository") or {}).get("name")) or "?",
            "rule": ((a.get("rule") or {}).get("id")) or "?",
        })
    return rows


# ---- fetch -----------------------------------------------------------------

async def fetch_github_stats() -> dict | None:
    """One refresh: 1 GraphQL + 5 small REST calls, ~every 5 minutes.

    Runs under SourceCache: returning None (or raising) keeps the previous
    good value; nothing here blocks the BLE loop. On a 401 the cached token
    is dropped so the next cycle re-reads `gh auth token` once.
    """
    import httpx

    org = get_github_org()
    if not org:
        # Per-install configuration, absent: the wire fns turn this into
        # err:no_org so the device pages prune themselves.
        return {"err": "no_org"}

    token = _gh_auth_token()
    if not token:
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        r = await client.post(f"{API}/graphql", json={"query": _graphql_query(org)})
        if r.status_code == 401:
            _drop_cached_token()
            return None
        r.raise_for_status()
        gql = r.json().get("data") or {}

        async def _rest(path: str, params: dict | None = None) -> tuple[int, list]:
            resp = await client.get(f"{API}{path}", params=params or {})
            if resp.status_code == 401:
                _drop_cached_token()
            if resp.status_code != 200:
                return resp.status_code, []
            body = resp.json()
            return 200, body if isinstance(body, list) else []

        dep_code, dep_raw = await _rest(
            f"/orgs/{org}/dependabot/alerts",
            {"state": "open", "per_page": 100})
        sec_code, sec_raw = await _rest(
            f"/orgs/{org}/secret-scanning/alerts",
            {"state": "open", "per_page": 100})
        scan_code, scan_raw = await _rest(
            f"/orgs/{org}/code-scanning/alerts",
            {"state": "open", "per_page": 100})

        # Repos that failed the org's code-security configuration attachment:
        # scanning silently isn't running there, which is worth a detail row.
        failed_repos: list[str] = []
        cfg_code, cfgs = await _rest(f"/orgs/{org}/code-security/configurations")
        if cfg_code == 200 and cfgs:
            cfg_id = cfgs[0].get("id")
            if cfg_id is not None:
                fr_code, frs = await _rest(
                    f"/orgs/{org}/code-security/configurations/{cfg_id}/repositories",
                    {"status": "failed", "per_page": 10})
                if fr_code == 200:
                    failed_repos = [
                        ((f.get("repository") or {}).get("name")) or "?" for f in frs]

    heat, total = parse_calendar(gql)
    pr_open, pr_bots, reviews = parse_pr_counts(gql)
    return {
        "heat": heat,
        "total_contrib": total,
        "pr_open": pr_open,
        "pr_bots": pr_bots,
        "reviews": reviews,
        "pr_rows": parse_pr_rows(gql),
        "dep": parse_dependabot_alerts(dep_raw),
        "sec": parse_secret_alerts(sec_raw),
        "scan": parse_scan_alerts(scan_raw),
        "scan_failed_repos": failed_repos,
        # "clean" is only meaningful if the endpoint answered; a 403/404 org
        # without code scanning renders as off, not as a fake green zero.
        "scan_ok": scan_code == 200,
        "sec_ok": sec_code == 200,
        "dep_ok": dep_code == 200,
    }


# ---- wire mapping ----------------------------------------------------------

def to_overview_wire(stats: dict | None) -> dict:
    """PAGE_GITHUB payload. Compact keys, one digit per heatmap day."""
    if not stats:
        return {"p": PAGE_GITHUB, "ok": False}
    if stats.get("err"):
        return {"p": PAGE_GITHUB, "ok": False, "err": stats["err"]}
    dep = stats.get("dep") or []

    # Four digits max per counter: with the full 364-digit heat string, five-
    # digit counters push the payload past the 480-byte wire guard and the
    # send would be silently refused. 9999+ of anything reads the same on a
    # desk display anyway.
    def _c(n: int) -> int:
        return min(int(n), 9999)

    return {
        "p": PAGE_GITHUB,
        "hm": stats.get("heat", ""),
        "tc": _c(stats.get("total_contrib", 0)),
        "pr": _c(stats.get("pr_open", 0)),
        "prb": _c(stats.get("pr_bots", 0)),
        "rv": _c(stats.get("reviews", 0)),
        "da": _c(len(dep)),
        "dh": _c(sum(1 for a in dep if a.get("severity") in ("critical", "high"))),
        "ss": _c(len(stats.get("sec") or [])),
        "cs": _c(len(stats.get("scan") or [])),
        "csok": bool(stats.get("scan_ok")),
        "ok": True,
    }


def _clip(s: str) -> str:
    # ASCII ".." rather than an ellipsis: the firmware's Styrene bitmap fonts
    # carry no U+2026 glyph, which would render as a missing-glyph box.
    return s if len(s) <= DETAIL_MAX_CHARS else s[:DETAIL_MAX_CHARS - 2] + ".."


def to_prs_wire(stats: dict | None) -> dict:
    """PAGE_GH_PRS payload: the oldest open human PRs, pre-rendered rows."""
    if not stats:
        return {"p": PAGE_GH_PRS, "ok": False}
    if stats.get("err"):
        return {"p": PAGE_GH_PRS, "ok": False, "err": stats["err"]}
    rows = [_clip(r) for r in (stats.get("pr_rows") or [])][:4]
    more = max(0, stats.get("pr_open", 0) - len(rows))
    page = {"p": PAGE_GH_PRS, "ok": True, "rows": rows, "more": min(more, 9999)}
    while (len(json.dumps(page, separators=(",", ":")).encode()) > 240
           and page["rows"]):
        page["more"] += 1
        page["rows"] = page["rows"][:-1]
    return page


def build_detail_rows(stats: dict) -> list[str]:
    """Worst offenders, one line each, worst first, capped at DETAIL_MAX_ROWS.

    Dependabot alerts lead (severity-sorted), then secrets grouped per repo,
    then repos where scanning attachment failed. Overflow collapses into a
    final "+N more" so a truncated list never reads as complete.
    """
    lines: list[str] = []
    for a in stats.get("dep") or []:
        lines.append(_clip(f"Dep {a['severity']}: {a['package']} @ {a['repo']}"))
    by_repo: dict[str, int] = {}
    for s in stats.get("sec") or []:
        by_repo[s["repo"]] = by_repo.get(s["repo"], 0) + 1
    for repo, n in sorted(by_repo.items(), key=lambda kv: -kv[1]):
        noun = "secret" if n == 1 else "secrets"
        lines.append(_clip(f"{n} {noun} @ {repo}"))
    for a in stats.get("scan") or []:
        lines.append(_clip(f"Scan {a['rule']} @ {a['repo']}"))
    failed = stats.get("scan_failed_repos") or []
    if failed:
        lines.append(_clip("Scan not attached: " + ", ".join(failed)))

    if len(lines) > DETAIL_MAX_ROWS:
        dropped = len(lines) - (DETAIL_MAX_ROWS - 1)
        lines = lines[:DETAIL_MAX_ROWS - 1] + [f"+{dropped} more"]
    return lines


def to_detail_wire(stats: dict | None) -> dict:
    """PAGE_GITHUB_DETAIL payload."""
    if not stats:
        return {"p": PAGE_GITHUB_DETAIL, "ok": False}
    if stats.get("err"):
        return {"p": PAGE_GITHUB_DETAIL, "ok": False, "err": stats["err"]}
    return {
        "p": PAGE_GITHUB_DETAIL,
        "rows": build_detail_rows(stats),
        "ok": True,
    }


if __name__ == "__main__":
    # Manual smoke test from a real terminal: prints wire payloads and sizes,
    # never the raw API responses.
    import asyncio
    stats = asyncio.run(fetch_github_stats())
    for wire in (to_overview_wire(stats), to_detail_wire(stats)):
        blob = json.dumps(wire, separators=(",", ":"))
        print(f"{len(blob):4d} bytes  {blob}")
