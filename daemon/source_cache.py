#!/usr/bin/env python3
"""Last-known-good caches for data sources, refreshed off the BLE poll path.

Why this exists:

The poll loop awaits everything on one task, and `ZOMBIE_BREAK_LIMIT = 1` means
the very first failed write tears the connection down. So anything slow on that
path is not merely slow -- it stops `client.is_connected` being re-checked, stops
writes going out, and blows the 120 s reconnect SLA. A single hung SSH handshake
or a black-holed HTTPS call would drop a working link.

Claude's own poll is on that path and always has been; that is tolerable because
it is one call with a 20 s timeout. It stops being tolerable the moment there are
five sources: even well-behaved ones serialise, and three 20 s timeouts on the
same tick exceed the entire poll interval.

So every source other than Claude runs here instead: its own task, its own
cadence, its own timeout, writing into a cache. The payload builders then only
ever do a dict lookup -- instantaneous, and incapable of raising or blocking.
That preserves the contract `add_codex_fields` established, where a broken
provider degrades to "xok": false rather than taking Claude down with it.

The cache deliberately keeps the last good value when a refresh fails, and
reports how old it is. Callers decide whether age makes a reading unusable --
`codex_usage.py` already draws that line for its own data. Serving a known-stale
number knowingly beats serving a fabricated fresh one.

Run: python -m pytest daemon/tests/test_source_cache.py -x -q
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


class SourceCache:
    """One data source's latest good value, plus whether it is worth trusting.

    `fetch` may be sync or async. Sync callables are run in a worker thread so a
    blocking file read or subprocess cannot stall the event loop -- the whole
    point of this class.
    """

    def __init__(
        self,
        name: str,
        fetch: Callable[[], Any] | Callable[[], Awaitable[Any]],
        interval: float,
        timeout: float,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.name = name
        self._fetch = fetch
        self.interval = interval
        self.timeout = timeout
        self._log = log or (lambda _msg: None)

        self.value: Any = None          # last good value, or None if never fetched
        self.fetched_at: float = 0.0    # when that value arrived
        self.last_error: str | None = None
        self.consecutive_failures = 0
        self._task: asyncio.Task | None = None

    # ---- what the payload builders call -------------------------------

    def get(self, max_age: float | None = None) -> Any:
        """Latest value, or None if there is none or it is older than max_age.

        Never blocks and never raises: this is called from the poll path.
        """
        if self.value is None:
            return None
        if max_age is not None and self.age() > max_age:
            return None
        return self.value

    def age(self) -> float:
        return float("inf") if self.fetched_at == 0.0 else time.time() - self.fetched_at

    def status(self) -> dict:
        """Diagnostics for the tray/log, not for the wire."""
        return {
            "name": self.name,
            "have_value": self.value is not None,
            "age_seconds": self.age(),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }

    # ---- the background half -------------------------------------------

    async def refresh_once(self) -> bool:
        """Fetch once under the timeout. Returns whether the value was updated.

        A failure keeps the previous value rather than blanking it: a source that
        blips should not make a page go dark for a minute.
        """
        try:
            if asyncio.iscoroutinefunction(self._fetch):
                value = await asyncio.wait_for(self._fetch(), timeout=self.timeout)
            else:
                # to_thread matters: a sync fetch that blocks would otherwise
                # block the loop, which is the exact failure this class prevents.
                value = await asyncio.wait_for(
                    asyncio.to_thread(self._fetch), timeout=self.timeout
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.consecutive_failures += 1
            self.last_error = f"timeout after {self.timeout}s"
            self._log(f"{self.name}: refresh timed out after {self.timeout}s")
            return False
        except Exception as e:                     # noqa: BLE001
            # Deliberately broad, for the same reason add_codex_fields is: an
            # unexpected exception type from a source must not kill the task that
            # keeps every other source alive.
            self.consecutive_failures += 1
            self.last_error = f"{type(e).__name__}: {e}"
            self._log(f"{self.name}: refresh failed: {self.last_error}")
            return False

        self.value = value
        self.fetched_at = time.time()
        self.consecutive_failures = 0
        self.last_error = None
        return True

    async def _run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.refresh_once()
            # Back off when a source is failing, so a dead VPS is not hammered
            # every cadence -- capped so recovery is still noticed promptly.
            delay = self.interval
            if self.consecutive_failures:
                delay = min(self.interval * (2 ** min(self.consecutive_failures, 4)),
                            self.interval * 8)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def start(self, stop_event: asyncio.Event) -> asyncio.Task:
        """Begin refreshing. Tasks outlive BLE reconnects deliberately -- a
        dropped link should not reset every source's cadence."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(stop_event), name=f"src:{self.name}")
        return self._task


class SourceRegistry:
    """The set of background sources, started once for the daemon's lifetime."""

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self._sources: dict[str, SourceCache] = {}
        self._log = log or (lambda _msg: None)

    def add(self, source: SourceCache) -> SourceCache:
        self._sources[source.name] = source
        return source

    def get(self, name: str) -> SourceCache | None:
        return self._sources.get(name)

    def value(self, name: str, max_age: float | None = None) -> Any:
        src = self._sources.get(name)
        return src.get(max_age) if src else None

    def start_all(self, stop_event: asyncio.Event) -> None:
        for src in self._sources.values():
            src.start(stop_event)
            self._log(f"source '{src.name}' started (every {src.interval}s,"
                      f" timeout {src.timeout}s)")

    async def stop_all(self) -> None:
        tasks = [s._task for s in self._sources.values() if s._task and not s._task.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass

    def statuses(self) -> list[dict]:
        return [s.status() for s in self._sources.values()]
