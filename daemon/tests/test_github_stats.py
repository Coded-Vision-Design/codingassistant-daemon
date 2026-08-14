#!/usr/bin/env python3
"""Tests for daemon/github_stats.py - wire mapping and the secret-hygiene rule.

Run: python -m pytest daemon/tests/test_github_stats.py -x -q
"""
import json

from daemon.github_stats import (
    DETAIL_MAX_CHARS,
    DETAIL_MAX_ROWS,
    HEATMAP_WEEKS,
    PAGE_GITHUB,
    PAGE_GITHUB_DETAIL,
    build_detail_rows,
    parse_calendar,
    parse_dependabot_alerts,
    parse_pr_counts,
    parse_secret_alerts,
    to_detail_wire,
    to_overview_wire,
)

# The wire guard in claude_usage_daemon_windows.py; a payload this size or
# larger is refused before it reaches BLE, so pages must stay under it.
WIRE_MAX_BYTES = 480

SECRET_SENTINEL = "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RSER"
TOKEN_SENTINEL = "gho_THIS-TOKEN-MUST-NEVER-APPEAR"


def _gql(levels_weeks=2, total=293, prs=263, bots=151, reviews=140):
    week = {"contributionDays": [{"contributionLevel": "FIRST_QUARTILE"}] * 7}
    return {
        "viewer": {"contributionsCollection": {"contributionCalendar": {
            "totalContributions": total,
            "weeks": [week] * levels_weeks,
        }}},
        "prsAll": {"issueCount": prs},
        "prsBots": {"issueCount": bots},
        "reviews": {"issueCount": reviews},
    }


def _stats(**over):
    base = {
        "heat": "0123441230" * 36 + "0123",  # 364 digits = 52 weeks
        "total_contrib": 293,
        "pr_open": 112, "pr_bots": 151, "reviews": 140,
        "dep": [
            {"repo": "OptimalHybridAcademy", "severity": "high", "package": "extract-zip"},
            {"repo": "GaleruHairStudioPlatform", "severity": "medium", "package": "lodash"},
        ],
        "sec": [{"repo": "Crafted", "type": "Google OAuth Client Secret"}] * 3
               + [{"repo": "WolvesBJJ", "type": "Google API Key"}],
        "scan": [],
        "scan_failed_repos": ["kmcfinal", "FaxiaPreta"],
        "scan_ok": True, "sec_ok": True, "dep_ok": True,
    }
    base.update(over)
    return base


# ---- parsers ---------------------------------------------------------------

def test_calendar_becomes_digits_and_total():
    heat, total = parse_calendar(_gql(levels_weeks=3))
    assert heat == "1" * 21
    assert total == 293


def test_calendar_trims_to_heatmap_weeks():
    heat, _ = parse_calendar(_gql(levels_weeks=53))
    assert len(heat) == HEATMAP_WEEKS * 7


def test_unknown_level_renders_as_zero_not_a_crash():
    g = _gql(levels_weeks=1)
    g["viewer"]["contributionsCollection"]["contributionCalendar"]["weeks"][0][
        "contributionDays"][0]["contributionLevel"] = "SOMETHING_NEW"
    heat, _ = parse_calendar(g)
    assert heat[0] == "0"


def test_pr_counts_split_humans_from_bots():
    assert parse_pr_counts(_gql()) == (112, 151, 140)


def test_dependabot_sorted_worst_first():
    rows = parse_dependabot_alerts([
        {"repository": {"name": "b"}, "security_advisory": {"severity": "low"},
         "dependency": {"package": {"name": "x"}}},
        {"repository": {"name": "a"}, "security_advisory": {"severity": "critical"},
         "dependency": {"package": {"name": "y"}}},
    ])
    assert [r["severity"] for r in rows] == ["critical", "low"]


# ---- the secret-hygiene rule -----------------------------------------------

def test_secret_values_never_survive_parsing():
    """The raw alert carries the committed secret in plaintext; the parser
    must whitelist repo and type and drop everything else."""
    raw = [{
        "repository": {"name": "WolvesBJJ"},
        "secret_type_display_name": "Google OAuth Client Secret",
        "secret": SECRET_SENTINEL,
        "first_location_detected": {"path": "env.local", "start_line": 21},
    }]
    rows = parse_secret_alerts(raw)
    blob = json.dumps(rows)
    assert SECRET_SENTINEL not in blob
    assert "env.local" not in blob
    assert rows == [{"repo": "WolvesBJJ", "type": "Google OAuth Client Secret"}]


def test_no_wire_payload_can_carry_a_secret_or_token():
    stats = _stats()
    stats["sec"] = parse_secret_alerts([{
        "repository": {"name": "Crafted"},
        "secret_type_display_name": "Google API Key",
        "secret": SECRET_SENTINEL,
    }])
    for wire in (to_overview_wire(stats), to_detail_wire(stats)):
        blob = json.dumps(wire)
        assert SECRET_SENTINEL not in blob
        assert TOKEN_SENTINEL not in blob


# ---- overview wire ---------------------------------------------------------

def test_overview_wire_maps_counts():
    w = to_overview_wire(_stats())
    assert w["p"] == PAGE_GITHUB and w["ok"] is True
    assert w["pr"] == 112 and w["prb"] == 151 and w["rv"] == 140
    assert w["da"] == 2 and w["dh"] == 1          # one high, one medium
    assert w["ss"] == 4 and w["cs"] == 0 and w["csok"] is True
    assert len(w["hm"]) == 364 and set(w["hm"]) <= set("01234")


def test_overview_wire_fits_the_byte_guard_at_worst_case():
    """Full 52-week heat string with every counter at its clamp ceiling.

    Counters are clamped to 9999 in to_overview_wire precisely so this holds:
    unclamped five-digit counters plus 364 heat digits exceed the guard and
    the send would be silently refused.
    """
    w = to_overview_wire(_stats(
        heat="4" * (HEATMAP_WEEKS * 7),
        total_contrib=99999, pr_open=99999, pr_bots=99999, reviews=99999,
        dep=[{"repo": "r", "severity": "high", "package": "p"}] * 10001,
        sec=[{"repo": "r", "type": "t"}] * 200,
        scan=[{"repo": "r", "rule": "x"}] * 200,
    ))
    assert w["tc"] == 9999 and w["pr"] == 9999      # the clamp is what saves the guard
    blob = json.dumps(w, separators=(",", ":"))
    # write_payload refuses strictly-greater-than the guard, so equal is fine.
    assert len(blob.encode()) <= WIRE_MAX_BYTES, len(blob.encode())


def test_missing_stats_is_an_honest_not_ok():
    assert to_overview_wire(None) == {"p": PAGE_GITHUB, "ok": False}
    assert to_detail_wire(None) == {"p": PAGE_GITHUB_DETAIL, "ok": False}


# ---- detail wire -----------------------------------------------------------

def test_detail_rows_lead_with_dependabot_then_secrets():
    rows = build_detail_rows(_stats())
    assert rows[0].startswith("Dep high: extract-zip")
    assert any("3 secrets @ Crafted" in r for r in rows)


def test_detail_rows_capped_with_an_honest_more_marker():
    stats = _stats(dep=[
        {"repo": f"repo{i}", "severity": "high", "package": f"pkg{i}"}
        for i in range(9)
    ])
    rows = build_detail_rows(stats)
    assert len(rows) == DETAIL_MAX_ROWS
    assert rows[-1].startswith("+") and rows[-1].endswith("more")


def test_detail_rows_fit_the_firmware_columns():
    stats = _stats(dep=[{
        "repo": "an-extremely-long-repository-name-that-will-not-fit",
        "severity": "critical",
        "package": "some-very-long-package-name-indeed",
    }])
    for r in build_detail_rows(stats):
        assert len(r) <= DETAIL_MAX_CHARS


def test_detail_wire_fits_the_byte_guard():
    w = to_detail_wire(_stats())
    blob = json.dumps(w, separators=(",", ":"), ensure_ascii=False)
    assert len(blob.encode()) < WIRE_MAX_BYTES


def test_failed_scan_attachment_repos_are_named():
    rows = build_detail_rows(_stats(dep=[], sec=[]))
    assert any("Scan not attached: kmcfinal, FaxiaPreta" in r for r in rows)
