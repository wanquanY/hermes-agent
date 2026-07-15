"""In-flight ownership barrier for subscription database polling.

Subscription records borrow a database facade from their transport/session
owner. Removing a subscription prevents future polls, but a poller may already
hold a snapshot. This barrier lets removal wait before that owner closes the
borrowed database.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable


class SubscriptionPollLifecycle:
    """Track accepted polls until their borrowed resources are quiescent."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._inflight_by_subscription: dict[str, int] = {}

    def start(self, subscription_ids: Iterable[str]) -> None:
        """Accept a snapshot batch before the registry lock is released."""

        with self._condition:
            for subscription_id in subscription_ids:
                stable = str(subscription_id or "").strip()
                if stable:
                    self._inflight_by_subscription[stable] = (
                        self._inflight_by_subscription.get(stable, 0) + 1
                    )

    def finish(self, subscription_id: str) -> None:
        """Release one accepted poll and wake resource owners when quiescent."""

        stable = str(subscription_id or "").strip()
        if not stable:
            return
        with self._condition:
            remaining = self._inflight_by_subscription.get(stable, 0) - 1
            if remaining > 0:
                self._inflight_by_subscription[stable] = remaining
            else:
                self._inflight_by_subscription.pop(stable, None)
                self._condition.notify_all()

    def wait(self, subscription_ids: Iterable[str]) -> None:
        """Wait until accepted polls for ``subscription_ids`` have finished."""

        stable_ids = {
            str(subscription_id or "").strip()
            for subscription_id in subscription_ids
            if str(subscription_id or "").strip()
        }
        if not stable_ids:
            return
        with self._condition:
            self._condition.wait_for(
                lambda: not any(
                    self._inflight_by_subscription.get(subscription_id, 0) > 0
                    for subscription_id in stable_ids
                )
            )


__all__ = ["SubscriptionPollLifecycle"]
