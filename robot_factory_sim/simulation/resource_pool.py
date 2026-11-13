"""Atomic shared-capacity admission for single resources and complete route sections."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType, TracebackType
from typing import Self

import simpy

TRAFFIC_CONTRACT = "traffic-mm-bundle-v1"
WIDTH_UNIT = "mm"
SLOT_UNIT = "slot"


class TrafficBlocked(ValueError):
    """A requested physical capacity is unavailable or no longer exists."""


@dataclass(frozen=True)
class Claim:
    """A resource-specific view of one bundle for execution, observation and planning."""

    request: CapacityRequest
    units: int

    @property
    def amount(self) -> int:
        """Expose the same amount to the detached planning snapshot."""
        return self.units

    @property
    def robot_id(self) -> str:
        """Return the owner identity without inferring it from a GUI position."""
        return self.request.robot_id

    @property
    def created_at(self) -> float:
        """Return the original request timestamp in simulation seconds."""
        return self.request.created_at

    @property
    def usage_since(self) -> float | None:
        """Return the grant timestamp, or None while pending."""
        return self.request.usage_since


class CapacityRequest(simpy.Event):
    """A context-managed all-or-none grant, including cancellation before admission."""

    def __init__(self, arbiter: TrafficArbiter, demands: dict[CapacityPool, int], robot_id: str) -> None:
        super().__init__(arbiter.env)
        self.arbiter = arbiter
        self.request_id = arbiter.next_request_id
        arbiter.next_request_id += 1
        self.demands = MappingProxyType(dict(demands))
        self.robot_id = robot_id
        self.created_at = float(arbiter.env.now)
        self.usage_since: float | None = None
        self.granted = False
        self.closed = False
        self.claims = MappingProxyType({resource: Claim(self, units) for resource, units in demands.items()})

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.release()

    def cancel(self) -> None:
        """Remove pending demand; admitted motion releases at its safe boundary."""
        if not self.granted:
            self.arbiter.release(self)

    def release(self) -> None:
        """Release each owned claim exactly once, then reconsider pending bundles."""
        self.arbiter.release(self)


class TrafficArbiter:
    """Grant a section atomically, with FIFO among overlapping requests only."""

    def __init__(self, env: simpy.Environment) -> None:
        self.env = env
        self.pending: list[CapacityRequest] = []
        self._draining = False
        self.next_request_id = 0
        self.observer: Callable[[str, CapacityRequest], None] | None = None

    def request(self, demands: dict[CapacityPool, int], robot_id: str = "") -> CapacityRequest:
        """Validate every demand before changing any resource's occupancy."""
        if not demands:
            raise ValueError("a traffic bundle needs at least one resource")
        for resource, units in demands.items():
            if resource.arbiter is not self:
                raise ValueError("all resources must use the same traffic arbiter")
            if isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= resource.capacity:
                raise TrafficBlocked(
                    f"{resource.resource_id}: demand {units} exceeds {resource.capacity} {resource.unit}")
        request = CapacityRequest(self, demands, robot_id)
        self.pending.append(request)
        if self.observer:
            self.observer("requested", request)
        self.drain()
        return request

    def blocker(self, request: CapacityRequest) -> CapacityPool | None:
        """Identify the first capacity/FIFO blocker without acquiring a partial set."""
        earlier: set[CapacityPool] = set()
        for waiting in self.pending:
            if waiting is request:
                break
            earlier.update(waiting.demands)
        for resource, units in request.demands.items():
            if resource in earlier or resource.count + units > resource.capacity:
                return resource
        return None

    def drain(self) -> None:
        """Evaluate grants without yielding between individual resources."""
        if self._draining:
            return
        self._draining = True
        try:
            earlier: set[CapacityPool] = set()
            for request in list(self.pending):
                if any(units > resource.capacity for resource, units in request.demands.items()):
                    self.pending.remove(request)
                    request.closed = True
                    request.fail(TrafficBlocked("traffic capacity changed before entry; re-plan"))
                    request.defused = True
                    if self.observer:
                        self.observer("invalidated", request)
                    continue
                if (earlier.intersection(request.demands) or
                        any(resource.count + units > resource.capacity for resource, units in request.demands.items())):
                    earlier.update(request.demands)
                    continue
                self.pending.remove(request)
                request.granted = True
                request.usage_since = float(self.env.now)
                for resource, claim in request.claims.items():
                    resource.users.append(claim)
                if any(resource.count > resource.capacity for resource in request.demands):
                    raise AssertionError("traffic grant exceeded capacity")
                if self.observer:
                    self.observer("granted", request)
                request.succeed()
        finally:
            self._draining = False

    def release(self, request: CapacityRequest) -> None:
        """Cancel pending demand or free an admitted bundle idempotently."""
        if request.arbiter is not self:
            raise ValueError("request belongs to another traffic arbiter")
        if request.closed:
            return
        request.closed = True
        if request in self.pending:
            self.pending.remove(request)
        for resource, claim in request.claims.items():
            if claim in resource.users:
                resource.users.remove(claim)
        if self.observer:
            self.observer("released" if request.granted else "cancelled", request)
        self.drain()

    def resize(self, limits: dict[CapacityPool, int]) -> None:
        """Change all layout capacities before any pending request can be admitted."""
        for resource, capacity in limits.items():
            if (resource.arbiter is not self or isinstance(capacity, bool) or
                    not isinstance(capacity, int) or capacity < 0):
                raise ValueError("invalid capacity update")
        for resource, capacity in limits.items():
            resource.capacity = capacity
        self.drain()


class CapacityPool:
    """Integer capacity in an explicit unit, with stable identity and drain-on-shrink."""

    def __init__(self, env: simpy.Environment, capacity: int, *, resource_id: str = "",
                 unit: str = SLOT_UNIT, arbiter: TrafficArbiter | None = None) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise ValueError("capacity must be a nonnegative integer")
        if unit not in (WIDTH_UNIT, SLOT_UNIT):
            raise ValueError("traffic capacity unit must be mm or slot")
        self._env = env
        self.capacity = capacity
        self.resource_id = resource_id
        self.unit = unit
        self.arbiter = arbiter or TrafficArbiter(env)
        self.users: list[Claim] = []

    @property
    def count(self) -> int:
        """Return occupied units, including admitted entry reservations."""
        return sum(claim.units for claim in self.users)

    @property
    def queue(self) -> list[Claim]:
        """Return this resource's FIFO view of pending bundles."""
        return [request.claims[self] for request in self.arbiter.pending if self in request.claims]

    def request(self, units: int | None = None, robot_id: str = "") -> CapacityRequest:
        """Request explicit units; omitted demand claims the complete resource."""
        return self.arbiter.request({self: self.capacity if units is None else units}, robot_id)

    def release(self, request: CapacityRequest) -> simpy.Event:
        """Release through the common arbiter and return a completed event."""
        if self not in request.demands:
            raise ValueError("request does not own this resource")
        request.release()
        return self._env.event().succeed()

    def resize(self, capacity: int) -> None:
        """Retain admitted owners; apply the new admission limit immediately."""
        self.arbiter.resize({self: capacity})
