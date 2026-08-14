#!/usr/bin/env python3
"""Tests for daemon/source_cache.py — the isolation between slow sources and BLE.

Run: python -m pytest daemon/tests/test_source_cache.py -x -q
"""
import asyncio
import time

import pytest

from daemon.source_cache import SourceCache, SourceRegistry


def _run(coro):
    return asyncio.run(coro)


# ---- the core promise: nothing here can block or raise into the caller ----

def test_get_before_any_fetch_is_none_not_an_error():
    c = SourceCache("x", lambda: 1, interval=60, timeout=1)
    assert c.get() is None
    assert c.age() == float("inf")


def test_successful_fetch_is_cached():
    c = SourceCache("x", lambda: {"v": 42}, interval=60, timeout=1)
    assert _run(c.refresh_once()) is True
    assert c.get() == {"v": 42}
    assert c.age() < 5
    assert c.consecutive_failures == 0


def test_failure_keeps_the_previous_good_value():
    """A blip must not blank a page for a whole poll interval."""
    state = {"fail": False}

    def fetch():
        if state["fail"]:
            raise RuntimeError("boom")
        return "good"

    c = SourceCache("x", fetch, interval=60, timeout=1)
    _run(c.refresh_once())
    state["fail"] = True
    assert _run(c.refresh_once()) is False
    assert c.get() == "good"                      # still served
    assert c.consecutive_failures == 1
    assert "RuntimeError" in c.last_error


def test_timeout_is_bounded_and_does_not_propagate():
    """The whole point: a hung source must not stall the caller."""
    async def hangs():
        await asyncio.sleep(30)

    c = SourceCache("slow", hangs, interval=60, timeout=0.05)
    started = time.time()
    assert _run(c.refresh_once()) is False
    assert time.time() - started < 5              # bounded by timeout, not by 30s
    assert "timeout" in c.last_error


def test_blocking_sync_fetch_does_not_block_the_loop():
    """Sync fetches run in a worker thread, so the loop keeps turning."""
    async def scenario():
        c = SourceCache("blocking", lambda: time.sleep(0.3) or "done",
                        interval=60, timeout=5)
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.02)
                ticks += 1

        await asyncio.gather(c.refresh_once(), ticker())
        return c.get(), ticks

    value, ticks = _run(scenario())
    assert value == "done"
    assert ticks >= 5, "event loop was blocked by a sync fetch"


def test_the_caller_owns_the_staleness_policy():
    """get() serves whatever it has; max_age is how a caller opts into a limit.

    Deliberate: how old is too old differs per page. A 7-day Codex quota is
    still true hours later, whereas a CPU load figure is worthless in a minute.
    """
    c = SourceCache("x", lambda: "v", interval=60, timeout=1)
    _run(c.refresh_once())
    c.fetched_at = time.time() - 3600             # pretend it is an hour old
    assert c.get() == "v"                         # no policy asked for, none applied
    assert c.get(max_age=600) is None             # an hour is too old for this caller
    assert c.get(max_age=7200) == "v"             # but not for this one


def test_none_is_a_legitimate_value_and_counts_as_fetched():
    """A source that legitimately has nothing to report is not a failure."""
    c = SourceCache("x", lambda: None, interval=60, timeout=1)
    assert _run(c.refresh_once()) is True
    assert c.consecutive_failures == 0
    assert c.get() is None


# ---- registry ------------------------------------------------------------

def test_registry_lookup_of_unknown_source_is_none():
    reg = SourceRegistry()
    assert reg.value("nope") is None
    assert reg.get("nope") is None


def test_registry_serves_cached_values():
    reg = SourceRegistry()
    c = reg.add(SourceCache("x", lambda: 7, interval=60, timeout=1))
    _run(c.refresh_once())
    assert reg.value("x") == 7


def test_registry_start_and_stop_are_clean():
    async def scenario():
        reg = SourceRegistry()
        reg.add(SourceCache("x", lambda: 1, interval=0.05, timeout=1))
        stop = asyncio.Event()
        reg.start_all(stop)
        await asyncio.sleep(0.15)
        assert reg.value("x") == 1
        await reg.stop_all()
        return [s["name"] for s in reg.statuses()]

    assert _run(scenario()) == ["x"]


def test_failing_source_backs_off_rather_than_hammering():
    """A dead VPS should not be retried at full cadence forever."""
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise OSError("down")

    async def scenario():
        c = SourceCache("x", always_fails, interval=0.02, timeout=1)
        stop = asyncio.Event()
        c.start(stop)
        await asyncio.sleep(0.25)
        stop.set()
        await asyncio.sleep(0.05)

    _run(scenario())
    # Without back-off a 0.02s cadence over 0.25s would be ~12 calls.
    assert calls["n"] < 10, f"no back-off observed ({calls['n']} calls)"
