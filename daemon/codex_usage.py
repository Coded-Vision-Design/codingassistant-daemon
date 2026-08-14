#!/usr/bin/env python3
"""Read OpenAI Codex quota from the session logs Codex writes locally.

Why the local files rather than an API call:

Codex records its own rate-limit state into every session rollout log, so the
numbers are already on disk. The alternative is polling
``chatgpt.com/backend-api/wham/usage`` with the token from ``~/.codex/auth.json``
-- which returns the same figures, but is an undocumented endpoint on a consumer
web property and means handling a credential we have no need to touch. Reading a
file Codex already wrote has neither problem, and it cannot break because
OpenAI changed a private route.

The cost is freshness: these files only change when Codex actually runs. That is
less bad than it sounds, because the figure is a percentage of a **7-day**
window -- it does not move unless you use Codex, so a stale reading is usually
still the true reading. The exception is a window that has since rolled over,
which is detectable (``resets_at`` is in the past) and is reported via
``stale_window`` rather than shown as fact.

Shape of the data, confirmed against a live install::

    {"limit_id": "codex", "plan_type": "plus",
     "primary":   {"used_percent": 100.0, "window_minutes": 10080,
                   "resets_at": 1787036639},
     "secondary": null,
     "credits":   {"has_credits": false, "unlimited": false, "balance": "0"}}

Note ``secondary`` is null on a Plus plan: Codex exposes one window, where
Anthropic exposes a 5-hour and a 7-day pair. Callers must cope with a provider
that has only one number.

This module performs no network I/O and never opens ``auth.json``. Rollout logs
contain conversation content, so nothing from them is logged or returned beyond
the rate-limit block itself.

Run: python -m pytest daemon/tests/test_codex_usage.py -x -q
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Only the few most recently touched sessions are examined. A busy install has
# hundreds of rollout files and ~58k rate-limit records; scanning them all on a
# 60-second poll would be wasteful when the answer is always in the newest one.
MAX_SESSIONS_SCANNED = 6

# Rollout logs grow to megabytes. The rate-limit block is emitted throughout the
# session, so reading the tail is enough and bounds the work per poll.
TAIL_BYTES = 512 * 1024


def find_codex_home() -> Path:
    """Locate the Codex data directory, honouring CODEX_HOME."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    return Path.home() / ".codex"


def _iter_recent_rollouts(home: Path) -> list[Path]:
    """Most-recently-modified rollout logs first, newest MAX_SESSIONS_SCANNED."""
    sessions = home / "sessions"
    if not sessions.is_dir():
        return []
    try:
        files = [p for p in sessions.rglob("rollout-*.jsonl") if p.is_file()]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:MAX_SESSIONS_SCANNED]


def _tail_lines(path: Path) -> list[str]:
    """Last TAIL_BYTES of a file as lines, newest last. Partial first line dropped."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # discard the partial line the seek landed inside
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def _extract_rate_limits(line: str) -> dict | None:
    """Return the rate_limits block from one JSONL record, if it has a populated one."""
    # Cheap reject before paying for json.loads on a conversation-sized record.
    if '"rate_limits"' not in line or '"used_percent"' not in line:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    # Records alternate between populated blocks and ones with null windows
    # (limit_id "premium" carries nulls); only populated ones are useful.
    if not isinstance(rl.get("primary"), dict) and not isinstance(rl.get("secondary"), dict):
        return None
    return rl


def read_codex_rate_limits(home: Path | None = None, now: float | None = None) -> dict | None:
    """Newest Codex quota reading available on disk, or None if there is none.

    Returns a provider-neutral dict::

        {"used_percent": float,     # 0..100
         "window_minutes": int,     # 10080 for the 7-day window
         "resets_at": int,          # unix seconds, absolute
         "reset_minutes": int,      # minutes until reset, clamped at 0
         "plan_type": str | None,
         "recorded_at": float,      # mtime of the session that supplied it
         "age_seconds": float,
         "stale_window": bool}      # reading predates a reset -- treat as unknown

    ``stale_window`` is the honest-reporting flag: once ``resets_at`` has passed,
    the recorded percentage describes a window that no longer exists. The caller
    should show "--" rather than a stale number.
    """
    home = home or find_codex_home()
    now = time.time() if now is None else now

    for path in _iter_recent_rollouts(home):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        # Scan newest line first: the last reading in the file is the current one.
        for line in reversed(_tail_lines(path)):
            rl = _extract_rate_limits(line)
            if rl is None:
                continue
            window = rl.get("primary") if isinstance(rl.get("primary"), dict) else rl.get("secondary")
            if not isinstance(window, dict):
                continue
            try:
                used = float(window.get("used_percent", 0.0))
                resets_at = int(window.get("resets_at", 0))
                window_minutes = int(window.get("window_minutes", 0))
            except (TypeError, ValueError):
                continue

            reset_minutes = max(0, int(round((resets_at - now) / 60.0))) if resets_at else 0
            return {
                "used_percent": max(0.0, min(100.0, used)),
                "window_minutes": window_minutes,
                "resets_at": resets_at,
                "reset_minutes": reset_minutes,
                "plan_type": rl.get("plan_type"),
                "recorded_at": mtime,
                "age_seconds": max(0.0, now - mtime),
                "stale_window": bool(resets_at) and now >= resets_at,
            }
    return None


def to_wire_fields(reading: dict | None) -> dict:
    """Map a reading onto the device wire keys for the second provider slot.

    Codex has one window where Claude has two, so only the weekly pair is sent
    and the session pair is deliberately omitted -- the firmware renders a
    provider with no session figure as a single bar rather than inventing one.

    xp carries the plan name and xag the reading's age in minutes: the reading
    comes from session logs, so "how stale is this" is part of the truth (the
    module docstring calls freshness the known weakness) and the device shows
    "as of Nh ago" instead of presenting an old figure as current.
    """
    if reading is None or reading["stale_window"]:
        return {"xok": False}
    fields = {
        "xw": int(round(reading["used_percent"])),
        "xwr": reading["reset_minutes"],
        "xag": int(reading.get("age_seconds", 0) // 60),
        "xok": True,
    }
    plan = reading.get("plan_type")
    if plan:
        fields["xp"] = str(plan)[:12]
    return fields


if __name__ == "__main__":  # manual probe: python daemon/codex_usage.py
    r = read_codex_rate_limits()
    if r is None:
        print("no Codex rate-limit reading found under", find_codex_home())
    else:
        print(json.dumps(r, indent=2))
        print("wire:", json.dumps(to_wire_fields(r)))
