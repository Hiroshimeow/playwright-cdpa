from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    Page,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .backend import AuthenticatedBackend
from .config import CoreConfig
from .connection import (
    BrowserSession,
    ConversationPage,
    close_page_by_target_id,
    page_target_id,
    resolve_conversation_page,
)
from .coordination import CoordinationStore
from .errors import (
    AmbiguousOutcomeError,
    CancellationUnprovenError,
    ConcurrentStateError,
    CoreError,
    Failure,
    FailureCategory,
    FrontendNotReadyError,
    IdentityMissingError,
    InvalidInputError,
    NetworkError,
    OperationTimeoutError,
    OwnershipConflictError,
    OwnerWaitTimeoutError,
    SchemaDriftError,
)
from .frontend import (
    ORIGIN,
    FrontendState,
    click_stop_button,
    fill_composer,
    find_stop_button,
    observe_frontend,
    verify_authenticated,
)
from .graph import GraphResolver, graph_fingerprints
from .identity import discover_user_identity, merge_identity
from .models import Result, SendProvenance, TurnIdentity, TurnRecord, TurnState
from .monitor import CandidateConvergence, monitor_live
from .schema import decode_optional_identifier
from .storage import StateStore
from .targets import normalize_conversation
from .transport import FrontendAcceptance, send_real

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


def _require_request_id(value: object) -> str:
    if type(value) is not str:
        raise InvalidInputError("request_id must be exactly a string")
    if not _REQUEST_ID_RE.fullmatch(value):
        raise InvalidInputError(
            "request_id must contain 1-160 ASCII letters, digits, dot, underscore, or hyphen"
        )
    return value


class _PreserveUserPageError(FrontendNotReadyError):
    """Pre-click user state was observed and the exact page must remain open."""


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
        self._coordination: CoordinationStore | None = None

    @property
    def coordination(self) -> CoordinationStore:
        if self._coordination is None:
            self._coordination = CoordinationStore(self.config.coordination_root)
        return self._coordination

    async def send(
        self,
        prompt: str,
        *,
        fresh: bool = False,
        conversation: str | None = None,
        request_id: str | None = None,
    ) -> Result:
        if type(prompt) is not str:
            raise InvalidInputError("prompt must be exactly a string")
        if not prompt.strip():
            raise InvalidInputError("prompt must not be empty")
        if fresh == (conversation is not None):
            raise InvalidInputError("select exactly one send target: fresh or conversation")
        conversation_id = (
            normalize_conversation(conversation) if conversation is not None else None
        )
        request_id = uuid.uuid4().hex if request_id is None else _require_request_id(request_id)
        request_path = self.store.turn_path(request_id)
        if request_path.exists():
            self.store.load(request_id)
            raise OwnershipConflictError(f"request_id {request_id!r} already exists")

        claimed_conversation: str | None = None
        if conversation_id is not None:
            try:
                await self._claim_when_idle(conversation_id, request_id)
            except OwnerWaitTimeoutError as exc:
                return Result(
                    schema_version=1,
                    request_id=request_id,
                    state=TurnState.FAILED,
                    failure=exc.as_failure(),
                )
            claimed_conversation = conversation_id

        try:
            record = self.store.create(
                TurnRecord.new(
                    request_id=request_id,
                    prompt=prompt,
                    target_kind="fresh" if fresh else "conversation",
                    target_conversation_id=conversation_id,
                )
            )
        except Exception:
            if claimed_conversation is not None:
                self._release_if_owned(claimed_conversation, request_id, terminal=False)
            raise
        return await self._send_record(
            record,
            prompt,
            conversation_id,
            claimed_conversation=claimed_conversation,
        )

    async def _claim_when_idle(self, conversation_id: str, request_id: str) -> None:
        deadline = time.monotonic() + self.config.timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                with self.coordination.lock(
                    conversation_id,
                    timeout=min(2.0, max(0.0, remaining)),
                ):
                    active = self.coordination.load(conversation_id).active_request_id
                    if active is None:
                        self.coordination.claim(conversation_id, request_id)
                        return
                    if time.monotonic() >= deadline:
                        raise OwnerWaitTimeoutError(
                            f"conversation remains owned by active request {active}"
                        )
            except OwnershipConflictError as exc:
                if time.monotonic() >= deadline:
                    raise OwnerWaitTimeoutError(str(exc)) from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            await asyncio.sleep(min(self.config.poll, remaining))

    def status(self, request_id: str) -> Result:
        request_id = _require_request_id(request_id)
        return self._result(self.store.load(request_id))

    async def get(self, request_id: str) -> Result:
        request_id = _require_request_id(request_id)
        record = self.store.load(request_id)
        record = await self._await_attachable_record(record)
        if record.state in {TurnState.FAILED, TurnState.CANCELLED}:
            try:
                await self._cleanup_terminal_helper(request_id)
            except CoreError as exc:
                return Result(
                    schema_version=1,
                    request_id=record.request_id,
                    state=record.state,
                    identity=record.identity,
                    failure=exc.as_failure(),
                )
            return self._result(self.store.load(request_id))
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
                context = session.context
                preferred_target = (
                    record.helper_page_target_id
                    if record.helper_page_closed_at is None
                    else None
                )
                lease = await resolve_conversation_page(
                    context,
                    identity.conversation_id,
                    preferred_target_id=preferred_target,
                )
                if lease.owned and lease.target_id != record.helper_page_target_id:
                    current = self.store.load(request_id)
                    record = self.store.save(
                        current.with_recovered_helper_page(lease.target_id),
                        expected_revision=current.revision,
                    )
                await verify_authenticated(lease.page)
                backend = AuthenticatedBackend(context)
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
                    try:
                        record = self.store.save(
                            record.with_response(candidate.text).transition(TurnState.COMPLETE),
                            expected_revision=record.revision,
                        )
                    except ConcurrentStateError:
                        record = self.store.load(request_id)
                        if record.state != TurnState.COMPLETE:
                            raise
                if lease.owned:
                    await self._close_owned_helper(context, request_id)
            self._release_if_owned(identity.conversation_id, request_id, terminal=True)
            return self._result(self.store.load(request_id), response=candidate.text)
        except CoreError as exc:
            return await self._record_watch_failure(record, exc)

    async def _await_attachable_record(self, record: TurnRecord) -> TurnRecord:
        if record.state not in {TurnState.PREPARING, TurnState.SENT}:
            return record
        deadline = time.monotonic() + self.config.identity_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self.config.poll)
            current = self.store.load(record.request_id)
            if current.state not in {TurnState.PREPARING, TurnState.SENT}:
                return current
            record = current
        return record

    async def cancel(self, request_id: str) -> Result:
        request_id = _require_request_id(request_id)
        record = self.store.load(request_id)
        if record.terminal:
            try:
                await self._cleanup_terminal_helper(request_id)
            except CoreError as exc:
                return Result(
                    schema_version=1,
                    request_id=record.request_id,
                    state=record.state,
                    identity=record.identity,
                    failure=exc.as_failure(),
                )
            return self._result(self.store.load(request_id))
        if record.send_provenance == SendProvenance.NOT_ATTEMPTED:
            record = self.store.save(
                record.request_cancellation().transition(TurnState.CANCELLED),
                expected_revision=record.revision,
            )
            if record.target_conversation_id:
                self._release_if_owned(record.target_conversation_id, request_id, terminal=True)
            return self._result(record)
        record = self.store.save(
            record.request_cancellation(), expected_revision=record.revision
        )
        identity = record.identity
        if identity is None or not identity.conversation_id:
            return self._persist_cancellation_unproven(
                record,
                "cannot prove cancellation without durable conversation identity",
            )
        conversation_id = identity.conversation_id
        lease: ConversationPage | None = None
        try:
            with self.coordination.lock(conversation_id, timeout=2.0):
                owner = self.coordination.load(conversation_id).active_request_id
                if owner != request_id:
                    raise CancellationUnprovenError(
                        "cancellation requires the exact shared conversation owner"
                    )
                async with BrowserSession(self.config) as session:
                    assert session.context is not None
                    context = session.context
                    backend = AuthenticatedBackend(context)
                    identity = await self._ensure_monitorable_identity(record, backend)
                    resolver = GraphResolver(identity)
                    convergence = CandidateConvergence(
                        self.config.stable_samples,
                        self.config.stable_seconds,
                    )
                    deadline = time.monotonic() + min(20.0, self.config.timeout)
                    while True:
                        preflight = await backend.snapshot(conversation_id)
                        terminal = await self._terminal_after_cancel_observation(
                            request_id,
                            identity,
                            preflight.stream_status,
                            preflight.graph or {},
                            convergence,
                        )
                        if terminal is not None:
                            await self._close_owned_helper(context, request_id)
                            return terminal
                        if preflight.stream_status.upper() not in {
                            "COMPLETE",
                            "COMPLETED",
                            "IDLE",
                            "NOT_FOUND",
                        }:
                            resolver.validate_exact_branch(preflight.graph or {})
                            break
                        if time.monotonic() >= deadline:
                            raise CancellationUnprovenError(
                                "terminal backend status did not yield "
                                "converged exact completion"
                            )
                        await asyncio.sleep(self.config.poll)

                    preferred_target = (
                        record.helper_page_target_id
                        if record.helper_page_closed_at is None
                        else None
                    )
                    lease = await resolve_conversation_page(
                        context,
                        conversation_id,
                        preferred_target_id=preferred_target,
                    )
                    await verify_authenticated(lease.page)
                    stop = await find_stop_button(lease.page)
                    if stop is None:
                        raise CancellationUnprovenError(
                            "the exact active conversation has no visible Stop control"
                        )
                    await click_stop_button(stop)
                    observed = ""
                    while time.monotonic() < deadline:
                        snapshot = await backend.snapshot(conversation_id)
                        observed = snapshot.stream_status.upper()
                        terminal = await self._terminal_after_cancel_observation(
                            request_id,
                            identity,
                            observed,
                            snapshot.graph or {},
                            convergence,
                        )
                        if terminal is not None:
                            return terminal
                        await asyncio.sleep(self.config.poll)
                    raise CancellationUnprovenError(
                        "Stop was clicked but cancellation was not proven; "
                        f"last status {observed or 'unknown'}"
                    )
        except CancellationUnprovenError as exc:
            return self._persist_cancellation_unproven(self.store.load(request_id), str(exc))
        except CoreError as exc:
            current = self.store.load(request_id)
            if current.terminal:
                return Result(
                    schema_version=1,
                    request_id=current.request_id,
                    state=current.state,
                    identity=current.identity,
                    failure=exc.as_failure(),
                )
            current = self.store.save(
                current.transition(TurnState.UNKNOWN, failure=exc.as_failure()),
                expected_revision=current.revision,
            )
            return self._result(current)
        finally:
            if lease is not None and lease.owned:
                try:
                    await lease.page.close()
                except PlaywrightError:
                    pass
                else:
                    current = self.store.load(request_id)
                    if (
                        lease.page.is_closed()
                        and current.helper_page_target_id == lease.target_id
                        and current.helper_page_closed_at is None
                    ):
                        self._mark_helper_closed(request_id)

    async def _terminal_after_cancel_observation(
        self,
        request_id: str,
        identity: TurnIdentity,
        status: str,
        graph: dict[str, Any],
        convergence: CandidateConvergence,
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
                self._release_if_owned(identity.conversation_id, request_id, terminal=True)
            return self._result(current)
        if normalized in {"FAILED", "ERROR"}:
            raise CancellationUnprovenError(
                f"backend ended with {normalized} without exact cancellation proof"
            )
        if normalized in {"COMPLETE", "COMPLETED", "IDLE", "NOT_FOUND"}:
            try:
                candidate = GraphResolver(identity).resolve(graph)
            except IdentityMissingError:
                convergence.reset()
                return None
            if not convergence.observe(candidate):
                return None
            current = self.store.load(request_id)
            if not current.terminal:
                current = self.store.save(
                    current.with_response(candidate.text).transition(TurnState.COMPLETE),
                    expected_revision=current.revision,
                )
            if identity.conversation_id:
                self._release_if_owned(identity.conversation_id, request_id, terminal=True)
            return self._result(current, response=candidate.text)
        convergence.reset()
        return None

    def _persist_cancellation_unproven(self, record: TurnRecord, message: str) -> Result:
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
        *,
        claimed_conversation: str | None = None,
    ) -> Result:
        click_entered = False
        page: Page | None = None
        owned_page = False
        owned_target_id: str | None = None
        durable_handoff = False
        preserve_user_page = False
        try:
            async with BrowserSession(self.config) as session:
                assert session.context is not None
                context = session.context
                backend = AuthenticatedBackend(context)
                if conversation_id is None:
                    page = await context.new_page()
                    target_id = await page_target_id(context, page)
                    await page.goto(ORIGIN, wait_until="domcontentloaded", timeout=60_000)
                    lease = ConversationPage(page, target_id, True)
                else:
                    lease = await resolve_conversation_page(context, conversation_id)
                    page = lease.page
                owned_page = lease.owned
                owned_target_id = lease.target_id if owned_page else None
                if owned_page:
                    current = self.store.load(record.request_id)
                    record = self.store.save(
                        current.with_helper_page(lease.target_id, keep=False),
                        expected_revision=current.revision,
                    )
                await verify_authenticated(page)
                await self._wait_for_initial_composer(page, conversation_id)
                initial_user_graph: dict[str, str] = {}
                if conversation_id is not None:
                    initial_snapshot = await backend.snapshot(conversation_id)
                    initial_user_graph = self._user_graph_fingerprints(
                        initial_snapshot.graph or {}
                    )
                expected_revision = self.store.load(record.request_id).revision
                await fill_composer(page, prompt)
                await self._wait_for_send_ready(
                    page,
                    backend,
                    record.request_id,
                    prompt,
                    conversation_id,
                    lease.target_id,
                    expected_revision,
                    initial_user_graph,
                )

                baseline_graph: dict[str, Any] = {}
                pre_send_current: str | None = None
                if conversation_id is not None:
                    snapshot = await backend.snapshot(conversation_id)
                    baseline_graph = snapshot.graph or {}
                    if self._user_graph_fingerprints(baseline_graph) != initial_user_graph:
                        raise FrontendNotReadyError(
                            "conversation user graph changed while waiting for Send"
                        )
                    try:
                        pre_send_current = decode_optional_identifier(
                            baseline_graph.get("current_node"), "current_node"
                        )
                    except ValueError as exc:
                        raise SchemaDriftError(str(exc)) from exc

                current = self.store.load(record.request_id)
                if current.revision != expected_revision or current.cancellation_requested_at:
                    raise ConcurrentStateError(
                        "request changed while waiting for the Send boundary"
                    )
                if conversation_id is not None:
                    self._require_exact_owner(conversation_id, record.request_id)
                await self._validate_ready_state(
                    page,
                    prompt,
                    conversation_id,
                    lease.target_id,
                )
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
                record = self.store.save(
                    current.transition(TurnState.PREPARING)
                    .with_identity(partial)
                    .with_baseline(
                        graph_fingerprints(baseline_graph)
                        if conversation_id is not None
                        else {}
                    ),
                    expected_revision=current.revision,
                )
                record = self.store.save(
                    record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED),
                    expected_revision=record.revision,
                )
                click_entered = True

                async def on_accepted(acceptance: FrontendAcceptance) -> None:
                    nonlocal record
                    current_record = self.store.load(record.request_id)
                    request_identity = TurnIdentity(
                        transport_request_id=acceptance.request_id,
                        user_message_id=acceptance.user_message_id,
                        frontend_parent_message_id=acceptance.frontend_parent_message_id,
                        sources={
                            key: "frontend-request"
                            for key, value in (
                                ("transport_request_id", acceptance.request_id),
                                ("user_message_id", acceptance.user_message_id),
                                (
                                    "frontend_parent_message_id",
                                    acceptance.frontend_parent_message_id,
                                ),
                            )
                            if value is not None
                        },
                    )
                    merged = merge_identity(
                        current_record.identity or TurnIdentity(),
                        request_identity,
                        source="frontend-request",
                    )
                    record = self.store.save(
                        current_record.with_identity(merged)
                        .with_send_provenance(SendProvenance.FRONTEND_ACCEPTED)
                        .transition(TurnState.SENT),
                        expected_revision=current_record.revision,
                    )

                handoff = await send_real(
                    page,
                    prompt=prompt,
                    target_conversation_id=conversation_id,
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
                    with self.coordination.lock(identity.conversation_id, timeout=2.0):
                        self.coordination.claim(identity.conversation_id, record.request_id)
                    claimed_conversation = identity.conversation_id

                record = self.store.save(
                    current.with_identity(identity).transition(TurnState.SENT),
                    expected_revision=current.revision,
                )
                identity = await self._bind_user_identity(backend, record, identity, prompt)
                record = self.store.load(record.request_id)
                record = self.store.save(
                    record.with_identity(identity)
                    .with_send_provenance(SendProvenance.USER_IDENTITY_BOUND)
                    .with_send_provenance(SendProvenance.DURABLE_HANDOFF)
                    .transition(TurnState.RUNNING),
                    expected_revision=record.revision,
                )
                durable_handoff = True
                if owned_page and not record.helper_page_keep:
                    await self._close_current_helper(page, record.request_id)
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
            preserve_user_page = isinstance(exc, _PreserveUserPageError)
            attempts = 4 if preserve_user_page else 1
            for _attempt in range(attempts):
                current = self.store.load(record.request_id)
                updated = current
                if not current.terminal:
                    state = TurnState.UNKNOWN if click_entered else TurnState.FAILED
                    updated = current.transition(state, failure=exc.as_failure())
                if preserve_user_page and owned_page:
                    if (
                        owned_target_id is None
                        or current.helper_page_target_id != owned_target_id
                        or current.helper_page_closed_at is not None
                    ):
                        raise ConcurrentStateError(
                            "owned helper identity changed before user-page preservation"
                        )
                    if not current.helper_page_keep:
                        updated = updated.with_helper_page(owned_target_id, keep=True)
                if updated == current:
                    break
                try:
                    current = self.store.save(
                        updated,
                        expected_revision=current.revision,
                    )
                    break
                except ConcurrentStateError:
                    if not preserve_user_page:
                        raise
            else:
                raise ConcurrentStateError(
                    "request changed repeatedly during user-page preservation"
                )
            if claimed_conversation and not click_entered:
                self._release_if_owned(claimed_conversation, current.request_id, terminal=True)
            return self._result(current)
        except Exception as exc:  # noqa: BLE001
            current = self.store.load(record.request_id)
            failure = self._unexpected_send_failure(exc, click_entered=click_entered)
            state = TurnState.UNKNOWN if click_entered else TurnState.FAILED
            if not current.terminal:
                current = self.store.save(
                    current.transition(state, failure=failure),
                    expected_revision=current.revision,
                )
            if claimed_conversation and not click_entered:
                self._release_if_owned(claimed_conversation, current.request_id, terminal=True)
            return self._result(current)
        finally:
            if (
                page is not None
                and owned_page
                and not preserve_user_page
                and (not click_entered or (durable_handoff and not record.helper_page_keep))
            ):
                try:
                    await self._close_current_helper(page, record.request_id)
                except Exception:  # noqa: BLE001,S110
                    pass

    async def _wait_for_initial_composer(
        self, page: Page, conversation_id: str | None
    ) -> FrontendState:
        deadline = time.monotonic() + self.config.identity_timeout
        last_state: FrontendState | None = None
        while time.monotonic() < deadline:
            state = await observe_frontend(page)
            last_state = state
            if state.choice_prompt:
                raise _PreserveUserPageError("choice prompt blocks automated Send")
            if state.composer_text.strip():
                raise _PreserveUserPageError(
                    "composer contains manual text; automated mutation is blocked"
                )
            if state.attachment_count:
                raise _PreserveUserPageError(
                    "composer contains manual attachments; automated mutation is blocked"
                )
            if state.composer_present and state.composer_editable:
                self._validate_initial_send_state(state, conversation_id)
                return state
            await asyncio.sleep(self.config.poll)
        if last_state is not None:
            self._validate_initial_send_state(last_state, conversation_id)
        raise FrontendNotReadyError("ChatGPT composer did not become safely editable")

    def _validate_initial_send_state(
        self, state: FrontendState, conversation_id: str | None
    ) -> None:
        if conversation_id is not None:
            try:
                observed = normalize_conversation(state.url)
            except InvalidInputError as exc:
                raise FrontendNotReadyError(
                    "exact conversation page was replaced before composer fill"
                ) from exc
            if observed != conversation_id:
                raise FrontendNotReadyError(
                    "exact conversation page changed before composer fill"
                )
        else:
            self._validate_fresh_send_url(state.url, "before composer fill")
        if state.choice_prompt:
            raise _PreserveUserPageError("choice prompt blocks automated Send")
        if not state.composer_present or not state.composer_editable:
            raise FrontendNotReadyError("ChatGPT composer is not safely editable")
        if state.composer_text.strip():
            raise _PreserveUserPageError(
                "composer contains manual text; automated mutation is blocked"
            )
        if state.attachment_count:
            raise _PreserveUserPageError(
                "composer contains manual attachments; automated mutation is blocked"
            )

    async def _validate_ready_state(
        self,
        page: Page,
        prompt: str,
        conversation_id: str | None,
        target_id: str,
    ) -> FrontendState:
        if await page_target_id(page.context, page) != target_id:
            raise FrontendNotReadyError("page target ownership changed before Send")
        state = await observe_frontend(page)
        if conversation_id is not None:
            try:
                observed = normalize_conversation(state.url)
            except InvalidInputError as exc:
                raise FrontendNotReadyError(
                    "exact conversation URL changed before Send"
                ) from exc
            if observed != conversation_id:
                raise FrontendNotReadyError("exact conversation URL changed before Send")
        else:
            self._validate_fresh_send_url(state.url, "before Send")
        if state.choice_prompt:
            raise _PreserveUserPageError("choice prompt appeared before Send")
        if not state.composer_present or not state.composer_editable:
            raise FrontendNotReadyError("composer became unavailable before Send")
        if state.composer_text.strip() != prompt.strip():
            raise _PreserveUserPageError("composer text changed before Send")
        if state.attachment_count:
            raise _PreserveUserPageError("attachments changed before Send")
        if not state.send_ready:
            raise FrontendNotReadyError("real Send control is not enabled")
        return state

    async def _wait_for_send_ready(
        self,
        page: Page,
        backend: AuthenticatedBackend,
        request_id: str,
        prompt: str,
        conversation_id: str | None,
        target_id: str,
        expected_revision: int,
        initial_user_graph: dict[str, str],
    ) -> FrontendState:
        deadline = time.monotonic() + self.config.timeout
        while time.monotonic() < deadline:
            current = self.store.load(request_id)
            if current.revision != expected_revision or current.cancellation_requested_at:
                raise ConcurrentStateError("request changed while waiting for Send readiness")
            if conversation_id is not None:
                self._require_exact_owner(conversation_id, request_id)
            if await page_target_id(page.context, page) != target_id:
                raise FrontendNotReadyError("page target ownership changed while waiting")
            state = await observe_frontend(page)
            if conversation_id is not None:
                try:
                    observed = normalize_conversation(state.url)
                except InvalidInputError as exc:
                    raise FrontendNotReadyError(
                        "exact conversation URL changed while waiting"
                    ) from exc
                if observed != conversation_id:
                    raise FrontendNotReadyError("exact conversation URL changed while waiting")
                snapshot = await backend.snapshot(conversation_id)
                if self._user_graph_fingerprints(snapshot.graph or {}) != initial_user_graph:
                    raise FrontendNotReadyError(
                        "conversation user graph changed while waiting for Send"
                    )
            else:
                self._validate_fresh_send_url(state.url, "while waiting for Send")
            if state.choice_prompt:
                raise _PreserveUserPageError("choice prompt appeared while waiting for Send")
            if not state.composer_present or not state.composer_editable:
                raise FrontendNotReadyError("composer became unavailable while waiting")
            if state.composer_text.strip() != prompt.strip():
                raise _PreserveUserPageError("composer text changed while waiting for Send")
            if state.attachment_count:
                raise _PreserveUserPageError("attachments changed while waiting for Send")
            if state.send_ready:
                return state
            await asyncio.sleep(self.config.poll)
        raise OperationTimeoutError(
            "real Send control did not become enabled before the ownership timeout"
        )

    @staticmethod
    def _validate_fresh_send_url(url: str, phase: str) -> None:
        parsed = urlparse(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise FrontendNotReadyError(f"fresh Send page was replaced {phase}") from exc
        supported = (
            parsed.scheme == "https"
            and (parsed.hostname or "").casefold() in {"chatgpt.com", "www.chatgpt.com"}
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
            and parsed.path in {"", "/"}
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        )
        if not supported:
            raise FrontendNotReadyError(f"fresh Send page was replaced {phase}")

    def _require_exact_owner(self, conversation_id: str, request_id: str) -> None:
        owner = self.coordination.load(conversation_id).active_request_id
        if owner != request_id:
            raise OwnershipConflictError("exact conversation ownership changed before Send")

    @staticmethod
    def _user_graph_fingerprints(graph: dict[str, Any]) -> dict[str, str]:
        fingerprints = graph_fingerprints(graph)
        mapping = graph.get("mapping")
        if not isinstance(mapping, dict):
            return {}
        result: dict[str, str] = {}
        for raw_node_id, raw_node in mapping.items():
            if not isinstance(raw_node_id, str) or not isinstance(raw_node, dict):
                continue
            message = raw_node.get("message")
            author = message.get("author") if isinstance(message, dict) else None
            role = author.get("role") if isinstance(author, dict) else None
            if role == "user" and raw_node_id in fingerprints:
                result[raw_node_id] = fingerprints[raw_node_id]
        return result

    async def _cleanup_terminal_helper(self, request_id: str) -> None:
        record = self.store.load(request_id)
        if (
            record.helper_page_keep
            or record.helper_page_target_id is None
            or record.helper_page_closed_at is not None
        ):
            return
        async with BrowserSession(self.config) as session:
            assert session.context is not None
            await self._close_owned_helper(session.context, request_id)

    async def _close_current_helper(self, page: Page, request_id: str) -> None:
        try:
            await page.close()
        finally:
            if page.is_closed():
                self._mark_helper_closed(request_id)

    async def _close_owned_helper(self, context: Any, request_id: str) -> None:
        for _attempt in range(4):
            record = self.store.load(request_id)
            if (
                record.helper_page_keep
                or record.helper_page_target_id is None
                or record.helper_page_closed_at is not None
            ):
                return
            target_id = record.helper_page_target_id
            await close_page_by_target_id(context, target_id)
            current = self.store.load(request_id)
            if current.helper_page_closed_at is not None:
                return
            if current.helper_page_target_id != target_id:
                raise AmbiguousOutcomeError(
                    "durable helper target identity changed during cleanup"
                )
            try:
                self.store.save(
                    current.with_helper_page_closed(),
                    expected_revision=current.revision,
                )
                return
            except ConcurrentStateError:
                continue
        raise AmbiguousOutcomeError(
            "helper cleanup state changed repeatedly during exact recovery"
        )

    def _mark_helper_closed(self, request_id: str) -> None:
        for _attempt in range(4):
            current = self.store.load(request_id)
            if (
                current.helper_page_target_id is None
                or current.helper_page_closed_at is not None
            ):
                return
            try:
                self.store.save(
                    current.with_helper_page_closed(),
                    expected_revision=current.revision,
                )
                return
            except ConcurrentStateError:
                continue
        raise AmbiguousOutcomeError("helper closure state changed repeatedly")

    @staticmethod
    def _unexpected_send_failure(exc: Exception, *, click_entered: bool) -> Failure:
        if click_entered:
            return AmbiguousOutcomeError(
                "browser operation failed after the irreversible Send boundary: "
                f"{type(exc).__name__}"
            ).as_failure()
        if isinstance(exc, PlaywrightTimeoutError):
            return OperationTimeoutError(
                "browser operation timed out before the Send boundary"
            ).as_failure()
        if isinstance(exc, PlaywrightError):
            return NetworkError(
                "browser operation failed before the Send boundary"
            ).as_failure()
        return Failure(
            category=FailureCategory.INVARIANT,
            message=f"local invariant failure: {type(exc).__name__}",
            retryable=False,
            external=False,
        )

    async def _ensure_monitorable_identity(
        self,
        record: TurnRecord,
        backend: AuthenticatedBackend,
    ) -> TurnIdentity:
        identity = record.identity
        if identity is None or not identity.conversation_id:
            raise IdentityMissingError("persisted request lacks exact conversation identity")
        current = self.store.load(record.request_id)
        if not identity.monitorable:
            structural_reconcile = bool(
                current.send_provenance == SendProvenance.RETRY_PROHIBITED
                and identity.pre_send_current_node
                and current.baseline_node_fingerprints
                and current.target_kind == "conversation"
                and current.target_conversation_id == identity.conversation_id
                and self.coordination.load(identity.conversation_id).active_request_id
                == current.request_id
            )
            if not identity.has_transport_correlation and not structural_reconcile:
                raise IdentityMissingError(
                    "persisted request lacks safe transport or graph-delta "
                    "reconciliation evidence"
                )
            snapshot = await backend.snapshot(identity.conversation_id)
            if snapshot.graph is None:
                raise IdentityMissingError("conversation graph is unavailable for recovery")
            identity = discover_user_identity(
                snapshot.graph,
                identity,
                baseline_node_fingerprints=current.baseline_node_fingerprints,
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
        baseline_fingerprints = record.baseline_node_fingerprints
        last_error: CoreError | None = None
        while time.monotonic() < deadline:
            snapshot = await backend.snapshot(identity.conversation_id)
            if snapshot.graph is not None:
                try:
                    return discover_user_identity(
                        snapshot.graph,
                        identity,
                        baseline_node_fingerprints=baseline_fingerprints,
                        prompt=prompt,
                    )
                except CoreError as exc:
                    last_error = exc
            await asyncio.sleep(self.config.poll)
        if last_error is not None:
            raise last_error
        raise IdentityMissingError("exact submitted user identity did not appear")

    async def _record_watch_failure(self, record: TurnRecord, exc: CoreError) -> Result:
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
        self.coordination.release_if_owned(conversation_id, request_id, terminal=terminal)

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
