from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import (
    BackendError,
    GraphConvergenceTimeout,
    IdentityMissingError,
    OperationTimeoutError,
)
from .graph import GraphCandidate, GraphResolver
from .models import TurnIdentity

_GRAPH_TERMINAL_STATUSES = {"COMPLETE", "COMPLETED", "IDLE", "NOT_FOUND"}


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    stream_status: str
    graph: dict[str, Any] | None


class SnapshotSource(Protocol):
    async def snapshot(self, conversation_id: str) -> MonitorSnapshot: ...


@dataclass(slots=True)
class CandidateConvergence:
    stable_samples: int
    stable_seconds: float
    fingerprint: str | None = None
    samples: int = 0
    stable_since: float | None = None

    def reset(self) -> None:
        self.fingerprint = None
        self.samples = 0
        self.stable_since = None

    def observe(self, candidate: GraphCandidate, *, now: float | None = None) -> bool:
        observed_at = time.monotonic() if now is None else now
        fingerprint = f"{candidate.fingerprint}:{candidate.chain_fingerprint}"
        if fingerprint == self.fingerprint:
            self.samples += 1
        else:
            self.fingerprint = fingerprint
            self.samples = 1
            self.stable_since = observed_at
        return bool(
            self.samples >= self.stable_samples
            and self.stable_since is not None
            and observed_at - self.stable_since >= self.stable_seconds
        )


async def monitor_snapshots(
    identity: TurnIdentity,
    snapshots: Iterable[tuple[str, dict[str, Any]]],
    *,
    stable_samples: int = 2,
) -> GraphCandidate:
    resolver = GraphResolver(identity)
    convergence = CandidateConvergence(stable_samples, 0.0)
    complete_seen = False
    last_error: Exception | None = None
    for raw_status, graph in snapshots:
        status = raw_status.upper()
        if status in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
            raise BackendError(f"turn ended with backend status {status}")
        if status not in _GRAPH_TERMINAL_STATUSES:
            convergence.reset()
            continue
        complete_seen = True
        try:
            candidate = resolver.resolve(graph)
        except IdentityMissingError as exc:
            last_error = exc
            convergence.reset()
            continue
        if convergence.observe(candidate):
            return candidate
    if complete_seen:
        suffix = f": {last_error}" if last_error else ""
        raise GraphConvergenceTimeout(f"exact final graph did not converge{suffix}")
    raise OperationTimeoutError("no terminal stream status observed")


async def monitor_live(
    identity: TurnIdentity,
    source: SnapshotSource,
    *,
    timeout: float,
    poll: float,
    stable_samples: int = 2,
    stable_seconds: float = 0.5,
) -> GraphCandidate:
    if not identity.conversation_id:
        raise IdentityMissingError("conversation_id is required to monitor")
    deadline = time.monotonic() + timeout
    resolver = GraphResolver(identity)
    convergence = CandidateConvergence(stable_samples, stable_seconds)
    complete_seen = False
    last_identity_error: Exception | None = None
    while time.monotonic() < deadline:
        snapshot = await source.snapshot(identity.conversation_id)
        status = snapshot.stream_status.upper()
        if status in {"FAILED", "ERROR"}:
            raise BackendError(f"turn ended with backend status {status}")
        if status in {"CANCELLED", "CANCELED"}:
            raise BackendError("turn was cancelled")
        if status in _GRAPH_TERMINAL_STATUSES:
            complete_seen = True
            if snapshot.graph is None:
                convergence.reset()
            else:
                try:
                    candidate = resolver.resolve(snapshot.graph)
                except IdentityMissingError as exc:
                    last_identity_error = exc
                    convergence.reset()
                else:
                    if convergence.observe(candidate):
                        return candidate
        else:
            convergence.reset()
        await asyncio.sleep(poll)
    if complete_seen:
        suffix = f": {last_identity_error}" if last_identity_error else ""
        raise GraphConvergenceTimeout(
            f"terminal stream did not yield a converged exact graph{suffix}"
        )
    raise OperationTimeoutError(f"timed out after {timeout:g}s waiting for exact turn")
