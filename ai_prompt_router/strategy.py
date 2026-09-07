"""Upstream selection strategies.

v1 ships pure round-robin. The strategy is an object rather than a function so the
later `least_busy` / `weighted` variants (plan section 10) can be dropped in without
touching router.py: everything the router needs is "given the eligible upstreams in
config order, hand me the order in which to try them".

Returning an *order* rather than a single pick is what makes failover free -- the
retry list is just the tail of the same rotation, so a request that has to skip a
dead bridge never re-selects the one it just failed on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence, TypeVar

T = TypeVar("T")


class SelectionStrategy(ABC):
    """Orders the eligible upstreams for one request."""

    name: str = "abstract"

    @abstractmethod
    def order(self, candidates: Sequence[T]) -> list[T]:
        """Return `candidates` reordered; index 0 is the upstream to try first."""


class RoundRobin(SelectionStrategy):
    """One counter, advanced once per request, indexed into the eligible list.

    The counter is deliberately global rather than per-eligible-list: keeping a
    cursor per distinct candidate set (which changes whenever an upstream goes
    unhealthy or a provider pin narrows the list) would make the rotation depend on
    history no operator can see. A single counter means "every call moves the dial
    one notch", which is what `X-AI-Prompt-Upstream` alternating in curl output
    actually demonstrates.

    Not lock-protected: every caller lives on the one asyncio event loop, and the
    read-modify-write below contains no await point, so it cannot interleave.
    """

    name = "round_robin"

    def __init__(self) -> None:
        self._counter = 0

    def order(self, candidates: Sequence[T]) -> list[T]:
        if not candidates:
            return []
        start = self._counter % len(candidates)
        self._counter += 1
        return list(candidates[start:]) + list(candidates[:start])

    @property
    def counter(self) -> int:
        """Exposed for tests/diagnostics; not part of the routing contract."""
        return self._counter


STRATEGIES: dict[str, type[SelectionStrategy]] = {
    RoundRobin.name: RoundRobin,
}

STRATEGY_NAMES = tuple(STRATEGIES)


def make_strategy(name: str) -> SelectionStrategy:
    try:
        return STRATEGIES[name]()
    except KeyError:
        raise ValueError(
            f"unknown strategy {name!r}; expected one of {', '.join(STRATEGY_NAMES)}"
        ) from None
