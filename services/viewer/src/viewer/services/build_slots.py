"""The viewer's one way of bounding an expensive build: a fixed number of slots, taken without waiting.

A build that holds memory or a CPU for seconds (a clip's transcode, a derived view's read) takes a slot first. A slot is
taken with a non-blocking acquire, so a burst is REFUSED rather than queued: the caller gets 503 with ``Retry-After``
and its thread returns at once. Queueing would hold a threadpool thread per waiter for as long as the queue ahead took,
exhausting the pool while ``/livez`` stays green.
"""

from __future__ import annotations

import threading

from service_kit.exceptions import ServiceUnavailableError


#: How long a refused caller is told to wait before asking again, in seconds.
RETRY_AFTER = 5


class BuildsBusy(ServiceUnavailableError):
    """Every slot is taken: the caller should retry, not wait."""

    def __init__(self, what: str, capacity: int) -> None:
        super().__init__(f"all {capacity} {what} build slots are busy; try again shortly", headers={"Retry-After": str(RETRY_AFTER)})


class BuildSlot:
    """One acquired slot, released explicitly or by leaving the ``with`` block."""

    def __init__(self, owner: BuildSlots) -> None:
        self._owner = owner
        self._held = True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._owner.release()

    def __enter__(self) -> BuildSlot:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class BuildSlots:
    """``capacity`` slots for builds of ``what``."""

    def __init__(self, what: str, capacity: int) -> None:
        if capacity < 1:
            raise ValueError(f"a build needs at least one slot, not {capacity}")
        self.what = what
        self.capacity = capacity
        self._slots = threading.BoundedSemaphore(capacity)

    def acquire(self) -> BuildSlot:
        """Take a slot or refuse. Never blocks.

        Raises:
            BuildsBusy: All ``capacity`` slots are taken.
        """
        if not self._slots.acquire(blocking=False):
            raise BuildsBusy(self.what, self.capacity)
        return BuildSlot(self)

    def release(self) -> None:
        self._slots.release()


__all__ = ["RETRY_AFTER", "BuildSlot", "BuildSlots", "BuildsBusy"]
