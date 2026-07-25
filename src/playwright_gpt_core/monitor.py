from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterable
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


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    stream_status: str
    graph: dict[str, Any] | None


class SnapshotSource(Protocol):
    async def snapshot(self, conversation_id: str) -> MonitorSnapshot: ...


async def monitor_snapshots(
    identity: TurnIdentity,
    snapshots: Iterable[tuple[str, dict[str, Any]]],
    *,
    stable_samples: int = 2,
) -> GraphCandidate:
    resolver = GraphResolver(identity)
    complete_seen = False
    last_fingerprint: str | None = None
    stable = 0
    last_error: Exception | None = None
    for raw_status, graph in snapshots:
        status = raw_status.upper()
        if status in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
            raise BackendError(f"turn ended with backend status {status}")
        if status != "COMPLETE":
            continue
        complete_seen = True
        try:
            candidate = resolver.resolve(graph)
        except IdentityMissingError as exc:
            last_error = exc
            continue
        if f"{candidate.fingerprint}:{candidate.chain_fingerprint}" == last_fingerprint:
            stable += 1
        else:
            last_fingerprint = f"{candidate.fingerprint}:{candidate.chain_fingerprint}"
            stable = 1
        if stable >= stable_samples:
            return candidate
    if complete_seen:
        suffix = f": {last_error}" if last_error else ""
        raise GraphConvergenceTimeout(f"exact final graph did not converge{suffix}")
    raise OperationTimeoutError("no COMPLETE stream status observed")


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
    last_fingerprint: str | None = None
    stable = 0
    stable_since: float | None = None
    complete_seen = False
    last_identity_error: Exception | None = None
    while time.monotonic() < deadline:
        snapshot = await source.snapshot(identity.conversation_id)
        status = snapshot.stream_status.upper()
        if status in {"FAILED", "ERROR"}:
            raise BackendError(f"turn ended with backend status {status}")
        if status in {"CANCELLED", "CANCELED"}:
            raise BackendError("turn was cancelled")
        if status == "COMPLETE":
            complete_seen = True
            if snapshot.graph is not None:
                try:
                    candidate = resolver.resolve(snapshot.graph)
                except IdentityMissingError as exc:
                    last_identity_error = exc
                else:
                    now = time.monotonic()
                    if f"{candidate.fingerprint}:{candidate.chain_fingerprint}" == last_fingerprint:
                        stable += 1
                    else:
                        last_fingerprint = f"{candidate.fingerprint}:{candidate.chain_fingerprint}"
                        stable = 1
                        stable_since = now
                    if stable >= stable_samples and stable_since is not None and now - stable_since >= stable_seconds:
                        return candidate
        await asyncio.sleep(poll)
    if complete_seen:
        suffix = f": {last_identity_error}" if last_identity_error else ""
        raise GraphConvergenceTimeout(f"stream completed but exact graph did not converge{suffix}")
    raise OperationTimeoutError(f"timed out after {timeout:g}s waiting for exact turn")
