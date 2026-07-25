from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from .backend import AuthenticatedBackend
from .config import CoreConfig
from .connection import BrowserSession
from .errors import (
    AmbiguousOutcomeError,
    CancellationUnprovenError,
    CoreError,
    Failure,
    FailureCategory,
    IdentityMissingError,
    InvalidInputError,
    OwnershipConflictError,
)
from .frontend import (
    ORIGIN,
    clear_composer,
    fill_composer,
    find_stop_button,
    verify_authenticated,
)
from .graph import GraphResolver, graph_fingerprints
from .identity import discover_user_identity, merge_identity
from .locking import ConversationLock
from .models import Result, SendProvenance, TurnIdentity, TurnRecord, TurnState
from .monitor import monitor_live
from .storage import StateStore
from .targets import conversation_url, normalize_conversation
from .transport import FrontendAcceptance, send_real

_TERMINAL_STREAM = {
    "COMPLETE",
    "COMPLETED",
    "FAILED",
    "ERROR",
    "CANCELLED",
    "CANCELED",
    "IDLE",
    "NOT_FOUND",
}


class ChatGPTCore:
    def __init__(self, config: CoreConfig | None = None) -> None:
        self.config = (config or CoreConfig()).validated()
        self.store = StateStore(self.config.state_dir)

    async def send(
        self,
        prompt: str,
        *,
        fresh: bool = False,
        conversation: str | None = None,
        wait_idle: bool = False,
        request_id: str | None = None,
    ) -> Result:
        prompt = str(prompt)
        if not prompt.strip():
            raise InvalidInputError("prompt must not be empty")
        if fresh == (conversation is not None):
            raise InvalidInputError("select exactly one send target: fresh or conversation")
        conversation_id = normalize_conversation(conversation) if conversation is not None else None
        request_id = request_id or uuid.uuid4().hex

        if conversation_id is not None:
            active = self.store.load_conversation(conversation_id).active_request_id
            if active and active != request_id:
                if not wait_idle:
                    raise OwnershipConflictError(
                        f"conversation has active request {active}"
                    )
                result = await self.watch(active)
                if result.state not in {
                    TurnState.COMPLETE,
                    TurnState.FAILED,
                    TurnState.CANCELLED,
                }:
                    raise OwnershipConflictError(
                        f"active request {active} did not reach a terminal state"
                    )

        record = TurnRecord.new(
            request_id=request_id,
            prompt=prompt,
            target_kind="fresh" if fresh else "conversation",
            target_conversation_id=conversation_id,
        )
        record = self.store.create(record)
        return await self._send_record(record, prompt, conversation_id)

    async def wait_idle_and_send(
        self, prompt: str, *, conversation: str, request_id: str | None = None
    ) -> Result:
        return await self.send(
            prompt,
            conversation=conversation,
            wait_idle=True,
            request_id=request_id,
        )

    async def watch(self, request_id: str) -> Result:
        record = self.store.load(request_id)
        if record.state == TurnState.CANCELLED:
            return self._result(record)
        identity = record.identity
        if identity is None or not identity.conversation_id:
            failure = IdentityMissingError(
                "persisted request lacks exact conversation identity"
            ).as_failure()
            return Result(
                1,
                request_id,
                record.state,
                identity=identity,
                failure=failure,
            )
        try:
            async with BrowserSession(self.config) as session:
                assert session.context is not None
                backend = AuthenticatedBackend(session.context)
                identity = await self._ensure_monitorable_identity(record, backend)
                candidate = await monitor_live(
                    identity,
                    backend,
                    timeout=self.config.timeout,
                    poll=self.config.poll,
                    stable_samples=self.config.stable_samples,
                    stable_seconds=self.config.stable_seconds,
                )
            record = self.store.load(request_id)
            if record.state != TurnState.COMPLETE:
                record = self.store.save(
                    record.with_response(candidate.text).transition(TurnState.COMPLETE),
                    expected_revision=record.revision,
                )
            if identity.conversation_id:
                self._release_if_owned(identity.conversation_id, request_id, terminal=True)
            return self._result(record, response=candidate.text)
        except CoreError as exc:
            return await self._record_watch_failure(record, exc)

    async def recover(self, request_id: str) -> Result:
        return await self.watch(request_id)

    def get(self, request_id: str) -> Result:
        return self._result(self.store.load(request_id))

    async def cancel(self, request_id: str) -> Result:
        record = self.store.load(request_id)
        if record.terminal:
            return self._result(record)
        record = self.store.save(
            record.request_cancellation(), expected_revision=record.revision
        )
        if record.send_provenance == SendProvenance.NOT_ATTEMPTED:
            record = self.store.save(
                record.transition(TurnState.CANCELLED), expected_revision=record.revision
            )
            if record.target_conversation_id:
                self._release_if_owned(
                    record.target_conversation_id, request_id, terminal=True
                )
            return self._result(record)
        identity = record.identity
        if identity is None or not identity.conversation_id:
            return self._persist_cancellation_unproven(
                record,
                "cannot prove cancellation without durable conversation identity",
            )
        conversation_id = identity.conversation_id
        page: Page | None = None
        try:
            with ConversationLock(self.config.state_dir, conversation_id, timeout=2.0):
                async with BrowserSession(self.config) as session:
                    assert session.context is not None
                    backend = AuthenticatedBackend(session.context)
                    identity = await self._ensure_monitorable_identity(record, backend)
                    preflight = await backend.snapshot(conversation_id)
                    resolver = GraphResolver(identity)
                    resolver.validate_exact_branch(preflight.graph or {})
                    terminal = await self._terminal_after_cancel_observation(
                        request_id,
                        identity,
                        preflight.stream_status,
                        preflight.graph or {},
                    )
                    if terminal is not None:
                        return terminal
                    page = await session.context.new_page()
                    try:
                        await page.goto(
                            conversation_url(conversation_id),
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                        await verify_authenticated(page)
                        stop = await find_stop_button(page)
                        if stop is None:
                            raise CancellationUnprovenError(
                                "the exact active conversation has no visible Stop control"
                            )
                        await stop.click()
                        deadline = time.monotonic() + min(20.0, self.config.timeout)
                        observed = ""
                        while time.monotonic() < deadline:
                            snapshot = await backend.snapshot(conversation_id)
                            observed = snapshot.stream_status.upper()
                            terminal = await self._terminal_after_cancel_observation(
                                request_id,
                                identity,
                                observed,
                                snapshot.graph or {},
                            )
                            if terminal is not None:
                                return terminal
                            await asyncio.sleep(self.config.poll)
                        raise CancellationUnprovenError(
                            "Stop was clicked but cancellation was not proven; "
                            f"last status {observed or 'unknown'}"
                        )
                    finally:
                        await page.close()
                        page = None
        except CancellationUnprovenError as exc:
            return self._persist_cancellation_unproven(
                self.store.load(request_id), str(exc)
            )
        except CoreError as exc:
            current = self.store.load(request_id)
            if not current.terminal:
                current = self.store.save(
                    current.transition(TurnState.UNKNOWN, failure=exc.as_failure()),
                    expected_revision=current.revision,
                )
            return self._result(current)
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _terminal_after_cancel_observation(
        self,
        request_id: str,
        identity: TurnIdentity,
        status: str,
        graph: dict[str, Any],
    ) -> Result | None:
        normalized = status.upper()
        if normalized in {"CANCELLED", "CANCELED"}:
            current = self.store.load(request_id)
            if not current.terminal:
                current = self.store.save(
                    current.transition(TurnState.CANCELLED),
                    expected_revision=current.revision,
                )
            if identity.conversation_id:
                self._release_if_owned(
                    identity.conversation_id, request_id, terminal=True
                )
            return self._result(current)
        if normalized in {"COMPLETE", "COMPLETED", "IDLE", "NOT_FOUND"}:
            try:
                candidate = GraphResolver(identity).resolve(graph)
            except CoreError:
                if normalized in {"COMPLETE", "COMPLETED"}:
                    raise CancellationUnprovenError(
                        "backend is terminal but no exact final or cancellation proof exists"
                    )
                return None
            current = self.store.load(request_id)
            if not current.terminal:
                current = self.store.save(
                    current.with_response(candidate.text).transition(TurnState.COMPLETE),
                    expected_revision=current.revision,
                )
            if identity.conversation_id:
                self._release_if_owned(
                    identity.conversation_id, request_id, terminal=True
                )
            return self._result(current, response=candidate.text)
        return None

    def _persist_cancellation_unproven(
        self, record: TurnRecord, message: str
    ) -> Result:
        failure = CancellationUnprovenError(message).as_failure()
        if not record.terminal:
            record = self.store.save(
                record.transition(TurnState.UNKNOWN, failure=failure),
                expected_revision=record.revision,
            )
        return self._result(record)

    async def _send_record(
        self,
        record: TurnRecord,
        prompt: str,
        conversation_id: str | None,
    ) -> Result:
        claimed_conversation: str | None = None
        page: Page | None = None
        click_entered = False
        durable_handoff = False
        try:
            if conversation_id is not None:
                with ConversationLock(
                    self.config.state_dir, conversation_id, timeout=2.0
                ):
                    self.store.claim_conversation(conversation_id, record.request_id)
                claimed_conversation = conversation_id

            async with BrowserSession(self.config) as session:
                assert session.context is not None
                backend = AuthenticatedBackend(session.context)
                page = await session.context.new_page()
                target_url = (
                    ORIGIN if conversation_id is None else conversation_url(conversation_id)
                )
                await page.goto(
                    target_url, wait_until="domcontentloaded", timeout=60_000
                )
                await verify_authenticated(page)

                baseline_graph: dict[str, Any] = {}
                pre_send_current: str | None = None
                if conversation_id is not None:
                    snapshot = await backend.snapshot(conversation_id)
                    if snapshot.stream_status.upper() not in _TERMINAL_STREAM:
                        raise OwnershipConflictError(
                            "conversation backend is active during locked preflight"
                        )
                    baseline_graph = snapshot.graph or {}
                    current = baseline_graph.get("current_node")
                    pre_send_current = str(current) if current is not None else None

                partial = TurnIdentity(
                    conversation_id=conversation_id,
                    pre_send_current_node=pre_send_current,
                    sources={
                        key: "preflight"
                        for key, value in (
                            ("conversation_id", conversation_id),
                            ("pre_send_current_node", pre_send_current),
                        )
                        if value is not None
                    },
                )
                record = self.store.load(record.request_id)
                record = self.store.save(
                    record.transition(TurnState.PREPARING)
                    .with_identity(partial)
                    .with_baseline(graph_fingerprints(baseline_graph)),
                    expected_revision=record.revision,
                )
                await fill_composer(page, prompt)
                record = self.store.save(
                    record.with_send_provenance(
                        SendProvenance.CLICK_BOUNDARY_ENTERED
                    ),
                    expected_revision=record.revision,
                )
                click_entered = True

                async def on_accepted(acceptance: FrontendAcceptance) -> None:
                    nonlocal record
                    current = self.store.load(record.request_id)
                    request_identity = TurnIdentity(
                        transport_request_id=acceptance.request_id,
                        user_message_id=acceptance.user_message_id,
                        frontend_parent_message_id=acceptance.frontend_parent_message_id,
                        sources={
                            key: "frontend-request"
                            for key, value in (
                                ("transport_request_id", acceptance.request_id),
                                ("user_message_id", acceptance.user_message_id),
                                ("frontend_parent_message_id", acceptance.frontend_parent_message_id),
                            )
                            if value is not None
                        },
                    )
                    merged = merge_identity(
                        current.identity or TurnIdentity(),
                        request_identity,
                        source="frontend-request",
                    )
                    record = self.store.save(
                        current.with_identity(merged)
                        .with_send_provenance(SendProvenance.FRONTEND_ACCEPTED)
                        .transition(TurnState.SENT),
                        expected_revision=current.revision,
                    )

                handoff = await send_real(
                    page,
                    send_timeout=self.config.send_timeout,
                    on_accepted=on_accepted,
                )
                current = self.store.load(record.request_id)
                identity = merge_identity(
                    current.identity or TurnIdentity(),
                    handoff.identity,
                    source="frontend-response",
                )
                if conversation_id is not None and identity.conversation_id != conversation_id:
                    raise AmbiguousOutcomeError(
                        "frontend accepted Send under a different conversation"
                    )
                if identity.conversation_id is None:
                    raise AmbiguousOutcomeError(
                        "accepted Send has no durable conversation identity"
                    )
                if claimed_conversation is None:
                    with ConversationLock(
                        self.config.state_dir, identity.conversation_id, timeout=2.0
                    ):
                        self.store.claim_conversation(
                            identity.conversation_id, record.request_id
                        )
                    claimed_conversation = identity.conversation_id

                record = self.store.save(
                    current.with_identity(identity).transition(TurnState.SENT),
                    expected_revision=current.revision,
                )
                identity = await self._bind_user_identity(
                    backend,
                    record,
                    identity,
                    prompt,
                )
                record = self.store.load(record.request_id)
                record = self.store.save(
                    record.with_identity(identity)
                    .with_send_provenance(SendProvenance.USER_IDENTITY_BOUND)
                    .with_send_provenance(SendProvenance.DURABLE_HANDOFF)
                    .transition(TurnState.RUNNING),
                    expected_revision=record.revision,
                )
                durable_handoff = True
                if not self.config.keep_helper_tab:
                    await page.close()
                    page = None

                candidate = await monitor_live(
                    identity,
                    backend,
                    timeout=self.config.timeout,
                    poll=self.config.poll,
                    stable_samples=self.config.stable_samples,
                    stable_seconds=self.config.stable_seconds,
                )
                record = self.store.load(record.request_id)
                record = self.store.save(
                    record.with_response(candidate.text).transition(TurnState.COMPLETE),
                    expected_revision=record.revision,
                )
                self._release_if_owned(
                    identity.conversation_id, record.request_id, terminal=True
                )
                return self._result(record, response=candidate.text)
        except CoreError as exc:
            current = self.store.load(record.request_id)
            failure = exc.as_failure()
            state = (
                TurnState.UNKNOWN
                if click_entered and not current.terminal
                else TurnState.FAILED
            )
            if not current.terminal:
                current = self.store.save(
                    current.transition(state, failure=failure),
                    expected_revision=current.revision,
                )
            if claimed_conversation and state == TurnState.FAILED:
                self._release_if_owned(
                    claimed_conversation, current.request_id, terminal=True
                )
            return self._result(current)
        except Exception as exc:
            current = self.store.load(record.request_id)
            failure = Failure(
                category=FailureCategory.INVARIANT,
                message=f"local invariant failure: {type(exc).__name__}: {exc}",
                retryable=False,
                external=False,
            )
            state = TurnState.UNKNOWN if click_entered else TurnState.FAILED
            if not current.terminal:
                current = self.store.save(
                    current.transition(state, failure=failure),
                    expected_revision=current.revision,
                )
            return self._result(current)
        finally:
            if page is not None and (not click_entered or durable_handoff):
                try:
                    await page.close()
                except Exception:
                    pass

    async def _ensure_monitorable_identity(
        self,
        record: TurnRecord,
        backend: AuthenticatedBackend,
    ) -> TurnIdentity:
        identity = record.identity
        if identity is None or not identity.conversation_id:
            raise IdentityMissingError(
                "persisted request lacks exact conversation identity"
            )
        current = self.store.load(record.request_id)
        if not identity.monitorable:
            structural_reconcile = bool(
                current.send_provenance == SendProvenance.RETRY_PROHIBITED
                and identity.pre_send_current_node
                and current.baseline_node_fingerprints
                and current.target_kind == "conversation"
                and current.target_conversation_id == identity.conversation_id
                and self.store.load_conversation(identity.conversation_id).active_request_id
                == current.request_id
            )
            if not identity.has_transport_correlation and not structural_reconcile:
                raise IdentityMissingError(
                    "persisted request lacks safe transport or graph-delta reconciliation evidence"
                )
            snapshot = await backend.snapshot(identity.conversation_id)
            if snapshot.graph is None:
                raise IdentityMissingError("conversation graph is unavailable for recovery")
            identity = discover_user_identity(
                snapshot.graph,
                identity,
                baseline_node_ids=set(current.baseline_node_fingerprints),
                prompt=None,
            )
            current = self.store.save(
                current.with_identity(identity)
                .with_send_provenance(SendProvenance.USER_IDENTITY_BOUND)
                .with_send_provenance(SendProvenance.DURABLE_HANDOFF)
                .transition(TurnState.RUNNING),
                expected_revision=current.revision,
            )
        elif current.state == TurnState.UNKNOWN:
            current = self.store.save(
                current.transition(TurnState.RUNNING),
                expected_revision=current.revision,
            )
        return current.identity or identity

    async def _bind_user_identity(
        self,
        backend: AuthenticatedBackend,
        record: TurnRecord,
        identity: TurnIdentity,
        prompt: str | None,
    ) -> TurnIdentity:
        if identity.conversation_id is None:
            raise IdentityMissingError("conversation_id is required for user binding")
        deadline = time.monotonic() + self.config.identity_timeout
        baseline_ids = set(record.baseline_node_fingerprints)
        last_error: CoreError | None = None
        while time.monotonic() < deadline:
            snapshot = await backend.snapshot(identity.conversation_id)
            if snapshot.graph is not None:
                try:
                    return discover_user_identity(
                        snapshot.graph,
                        identity,
                        baseline_node_ids=baseline_ids,
                        prompt=prompt,
                    )
                except CoreError as exc:
                    last_error = exc
            await asyncio.sleep(self.config.poll)
        if last_error is not None:
            raise last_error
        raise IdentityMissingError("exact submitted user identity did not appear")

    async def _record_watch_failure(
        self, record: TurnRecord, exc: CoreError
    ) -> Result:
        current = self.store.load(record.request_id)
        if current.terminal:
            return Result(
                schema_version=1,
                request_id=current.request_id,
                state=current.state,
                identity=current.identity,
                failure=exc.as_failure(),
            )
        target_state = (
            TurnState.FAILED
            if current.send_provenance == SendProvenance.NOT_ATTEMPTED
            else TurnState.UNKNOWN
        )
        current = self.store.save(
            current.transition(target_state, failure=exc.as_failure()),
            expected_revision=current.revision,
        )
        return self._result(current)

    def _release_if_owned(
        self, conversation_id: str, request_id: str, *, terminal: bool
    ) -> None:
        current = self.store.load_conversation(conversation_id)
        if current.active_request_id == request_id:
            self.store.release_conversation(
                conversation_id, request_id, terminal=terminal
            )

    @staticmethod
    def _result(record: TurnRecord, *, response: str | None = None) -> Result:
        return Result(
            schema_version=1,
            request_id=record.request_id,
            state=record.state,
            response=response,
            identity=record.identity,
            failure=record.failure,
        )
