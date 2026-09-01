"""Rate limiting for the GitHub API.

Tracks the three GitHub rate-limit families ("core", "search", "graphql"),
enforces a minimum interval between requests per family, and updates its
budget from response headers when they are present.

Uses time.monotonic() for all local pacing decisions.  Header reset values
are epoch seconds (wall clock), so they are converted to a monotonic
deadline at update time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Mapping


# Per-family defaults: (min seconds between requests, remaining threshold
# below which we start sleeping until reset).
_DEFAULTS: dict[str, tuple[float, int]] = {
    # core: 5000/hr authenticated -> very light pacing.
    "core": (0.1, 20),
    # search: 30/min -> at least 2s between calls to stay safely under.
    "search": (2.1, 3),
    # graphql: 5000 points/hr -> light pacing.
    "graphql": (0.5, 10),
}


@dataclass
class _FamilyState:
    min_interval: float
    low_water: int
    last_request_at: float | None = None
    remaining: int | None = None
    # Monotonic deadline at which the budget resets (None = unknown).
    reset_at_monotonic: float | None = None


class RateLimiter:
    """Conservative client-side pacing for GitHub API request families."""

    def __init__(self) -> None:
        self._families: dict[str, _FamilyState] = {
            name: _FamilyState(min_interval=interval, low_water=low)
            for name, (interval, low) in _DEFAULTS.items()
        }

    def _state(self, family: str) -> _FamilyState:
        state = self._families.get(family)
        if state is None:
            # Unknown family: treat like core (mild pacing), register it.
            state = _FamilyState(min_interval=0.5, low_water=5)
            self._families[family] = state
        return state

    def before(self, family: str) -> None:
        """Call immediately before issuing a request in *family*.

        Sleeps if the minimum inter-request interval has not elapsed, or if
        the known remaining budget is low (in which case it sleeps until the
        reported reset time, capped defensively).
        """
        state = self._state(family)
        now = time.monotonic()

        # Enforce minimum spacing between requests.
        if state.last_request_at is not None:
            elapsed = now - state.last_request_at
            wait = state.min_interval - elapsed
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()

        # If we know the budget is nearly exhausted, wait for the reset.
        if (
            state.remaining is not None
            and state.remaining <= state.low_water
            and state.reset_at_monotonic is not None
        ):
            wait = state.reset_at_monotonic - now
            if wait > 0:
                # Cap the sleep defensively (GitHub windows are <= 1h).
                time.sleep(min(wait + 1.0, 3600.0))
                # Budget should be fresh after reset; forget stale values.
                state.remaining = None
                state.reset_at_monotonic = None

        state.last_request_at = time.monotonic()

    def update_from_headers(self, family: str, headers: Mapping[str, str]) -> None:
        """Record budget info from response headers, if present.

        Safe to call with any mapping; missing or malformed headers are
        ignored.
        """
        if headers is None:  # defensive: some callers may pass None
            return
        state = self._state(family)

        remaining_raw = _header(headers, "x-ratelimit-remaining")
        if remaining_raw is not None:
            try:
                state.remaining = int(remaining_raw)
            except (TypeError, ValueError):
                pass

        reset_raw = _header(headers, "x-ratelimit-reset")
        if reset_raw is not None:
            try:
                reset_epoch = float(reset_raw)
            except (TypeError, ValueError):
                pass
            else:
                delta = reset_epoch - time.time()
                if delta < 0:
                    delta = 0.0
                state.reset_at_monotonic = time.monotonic() + delta


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that tolerates plain dicts."""
    try:
        value = headers.get(name)
        if value is not None:
            return value
        # Plain dicts are case-sensitive; try common variants.
        return headers.get(name.title()) or headers.get(name.upper())
    except AttributeError:
        return None
