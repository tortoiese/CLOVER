"""Atomic, weighted SimPy admission with capacity changes that drain users."""

from __future__ import annotations

import simpy
from simpy.resources.resource import Request


class AmountRequest(Request):
    """Request all width units together; never hold a partial corridor."""

    def __init__(self, resource: CapacityPool, amount: int = 1) -> None:
        if amount < 1:
            raise ValueError("amount must be positive")
        self.amount = amount
        super().__init__(resource)


class CapacityPool(simpy.Resource):
    """A FIFO resource measured in integer units, including a closed state."""

    def __init__(self, env: simpy.Environment, capacity: int) -> None:
        super().__init__(env, capacity=max(1, capacity))
        self._capacity = max(0, capacity)

    @property
    def count(self) -> int:
        """Return occupied units rather than the number of owners."""
        return sum(request.amount for request in self.users)

    def request(self, amount: int = 1) -> AmountRequest:
        """Acquire an indivisible amount, preserving FIFO order."""
        return AmountRequest(self, amount)

    def _do_put(self, event: AmountRequest) -> None:
        if self.count + event.amount <= self.capacity:
            self.users.append(event)
            event.usage_since = self._env.now
            event.succeed()

    def resize(self, capacity: int) -> None:
        """Keep existing users on shrink and immediately admit on growth."""
        self._capacity = max(0, capacity)
        while self.put_queue:
            before = len(self.put_queue)
            self._trigger_put(None)
            if len(self.put_queue) == before:
                break
