#!/usr/bin/env python3
"""Unit tests for daemon/codex_usage.py — the Codex quota reader.

Run: python -m pytest daemon/tests/test_codex_usage.py -x -q
"""
import json
import time

import pytest

from daemon.codex_usage import (
    MAX_SESSIONS_SCANNED,
    find_codex_home,
    read_codex_rate_limits,
    to_wire_fields,
)


def _rollout_line(used=42.0, window_minutes=10080, resets_at=2_000_000_000,
                  plan="plus", limit_id="codex"):
    """One rollout JSONL record carrying a populated rate_limits block."""
    return json.dumps({
        "type": "event_msg",
        "payload": {
            "rate_limits": {
                "limit_id": limit_id,
                "plan_type": plan,
                "primary": {
                    "used_percent": used,
                    "window_minutes": window_minutes,
                    "resets_at": resets_at,
                },
                "secondary": None,
                "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            }
        },
    })


def _write_session(home, name, lines):
    d = home / "sessions" / "2026" / "08"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-{name}.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_reads_populated_reading(tmp_path):
    """Nominal: a single session with one populated record."""
    _write_session(tmp_path, "a", [_rollout_line(used=37.5)])
    r = read_codex_rate_limits(tmp_path, now=1_000_000_000)
    assert r is not None
    assert r["used_percent"] == 37.5
    assert r["window_minutes"] == 10080
    assert r["plan_type"] == "plus"
    assert r["stale_window"] is False


def test_last_reading_in_file_wins(tmp_path):
    """A session accumulates readings; the newest one is the current quota."""
    _write_session(tmp_path, "a", [
        _rollout_line(used=10.0),
        _rollout_line(used=20.0),
        _rollout_line(used=90.0),      # newest
    ])
    r = read_codex_rate_limits(tmp_path, now=1_000_000_000)
    assert r["used_percent"] == 90.0


def test_null_windows_are_skipped(tmp_path):
    """The limit_id 'premium' records carry null windows and must not win."""
    null_rec = json.dumps({"payload": {"rate_limits": {
        "limit_id": "premium", "plan_type": "plus",
        "primary": None, "secondary": None,
        "used_percent_placeholder": "used_percent",   # forces the cheap prefilter to pass
    }}})
    _write_session(tmp_path, "a", [_rollout_line(used=55.0), null_rec])
    r = read_codex_rate_limits(tmp_path, now=1_000_000_000)
    assert r is not None and r["used_percent"] == 55.0


def test_stale_window_flagged_once_reset_has_passed(tmp_path):
    """A reading whose window already rolled describes a quota that no longer exists."""
    _write_session(tmp_path, "a", [_rollout_line(used=100.0, resets_at=1_500)])
    r = read_codex_rate_limits(tmp_path, now=2_000)
    assert r["stale_window"] is True
    assert r["reset_minutes"] == 0          # clamped, never negative
    assert to_wire_fields(r) == {"xok": False}   # not reported as fact


def test_reset_minutes_counts_down(tmp_path):
    _write_session(tmp_path, "a", [_rollout_line(resets_at=10_000)])
    r = read_codex_rate_limits(tmp_path, now=10_000 - 3600)
    assert r["reset_minutes"] == 60


def test_percent_is_clamped(tmp_path):
    """Defensive: a nonsense percentage must not reach the display."""
    _write_session(tmp_path, "a", [_rollout_line(used=999.0)])
    assert read_codex_rate_limits(tmp_path, now=1_000)["used_percent"] == 100.0
    _write_session(tmp_path, "b", [_rollout_line(used=-5.0)])
    assert read_codex_rate_limits(tmp_path, now=1_000)["used_percent"] == 0.0


def test_missing_home_returns_none(tmp_path):
    """No Codex install at all is a normal state, not an error."""
    assert read_codex_rate_limits(tmp_path / "nope", now=1_000) is None


def test_no_sessions_dir_returns_none(tmp_path):
    (tmp_path / "auth.json").write_text("{}", encoding="utf-8")
    assert read_codex_rate_limits(tmp_path, now=1_000) is None


def test_garbage_lines_do_not_raise(tmp_path):
    """Rollout logs are appended live and can hold a torn final line."""
    _write_session(tmp_path, "a", [
        "not json at all",
        '{"payload": {"rate_limits": "used_percent not a dict"}}',
        '{"truncated": "used_percent" ',
        _rollout_line(used=12.0),
    ])
    r = read_codex_rate_limits(tmp_path, now=1_000)
    assert r["used_percent"] == 12.0


def test_only_recent_sessions_are_scanned(tmp_path, monkeypatch):
    """A busy install has hundreds of sessions; the reader must bound its work."""
    now = time.time()
    # Oldest file holds the only reading, but sits outside the scan window.
    old = _write_session(tmp_path, "old", [_rollout_line(used=11.0)])
    import os
    os.utime(old, (now - 99999, now - 99999))
    for i in range(MAX_SESSIONS_SCANNED + 2):
        p = _write_session(tmp_path, f"new{i}", ["{}"])
        os.utime(p, (now - i, now - i))
    assert read_codex_rate_limits(tmp_path, now=now) is None


def test_find_codex_home_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom"))
    assert find_codex_home() == tmp_path / "custom"


def test_wire_fields_omit_session_pair(tmp_path):
    """Codex exposes one window; the session keys must not be invented.

    xag (reading age, minutes) is honest metadata about the log-derived
    figure; xp appears only when the log carries a plan name.
    """
    _write_session(tmp_path, "a", [_rollout_line(used=64.0, resets_at=10_000)])
    w = to_wire_fields(read_codex_rate_limits(tmp_path, now=10_000 - 120 * 60))
    assert w["xw"] == 64 and w["xwr"] == 120 and w["xok"] is True
    assert isinstance(w["xag"], int) and w["xag"] >= 0
    assert "xs" not in w and "xsr" not in w


def test_wire_fields_handle_absence():
    assert to_wire_fields(None) == {"xok": False}
