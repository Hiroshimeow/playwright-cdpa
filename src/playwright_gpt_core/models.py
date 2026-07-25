from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .errors import Failure


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TurnState(str, Enum):
    NEW = "NEW"
    PREPARING = "PREPARING"
    SENT = "SENT"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class SendProvenance(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    CLICK_BOUNDARY_ENTERED = "CLICK_BOUNDARY_ENTERED"
    FRONTEND_ACCEPTED = "FRONTEND_ACCEPTED"
    USER_IDENTITY_BOUND = "USER_IDENTITY_BOUND"
    DURABLE_HANDOFF = "DURABLE_HANDOFF"
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    RETRY_PROHIBITED = "RETRY_PROHIBITED"


_ALLOWED_TRANSITIONS: dict[TurnState, frozenset[TurnState]] = {
    TurnState.NEW: frozenset({TurnState.PREPARING, TurnState.CANCELLED, TurnState.FAILED}),
    TurnState.PREPARING: frozenset(
        {TurnState.SENT, TurnState.RUNNING, TurnState.UNKNOWN, TurnState.CANCELLED, TurnState.FAILED}
    ),
    TurnState.SENT: frozenset(
        {TurnState.RUNNING, TurnState.COMPLETE, TurnState.UNKNOWN, TurnState.CANCELLED, TurnState.FAILED}
    ),
    TurnState.RUNNING: frozenset(
        {TurnState.COMPLETE, TurnState.UNKNOWN, TurnState.CANCELLED, TurnState.FAILED}
    ),
    TurnState.UNKNOWN: frozenset(
        {TurnState.RUNNING, TurnState.COMPLETE, TurnState.CANCELLED, TurnState.FAILED}
    ),
    TurnState.COMPLETE: frozenset(),
    TurnState.FAILED: frozenset(),
    TurnState.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    conversation_id: str | None = None
    turn_exchange_id: str | None = None
    request_id: str | None = None
    stream_topic_id: str | None = None
    user_message_id: str | None = None
    parent_message_id: str | None = None
    pre_send_current_node: str | None = None
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def has_turn_correlation(self) -> bool:
        return bool(self.turn_exchange_id or self.request_id or self.stream_topic_id)

    @property
    def monitorable(self) -> bool:
        return bool(self.conversation_id and self.user_message_id and self.has_turn_correlation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "turn_exchange_id": self.turn_exchange_id,
            "request_id": self.request_id,
            "stream_topic_id": self.stream_topic_id,
            "user_message_id": self.user_message_id,
            "parent_message_id": self.parent_message_id,
            "pre_send_current_node": self.pre_send_current_node,
            "sources": dict(self.sources),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> TurnIdentity | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("identity must be an object")
        names = (
            "conversation_id",
            "turn_exchange_id",
            "request_id",
            "stream_topic_id",
            "user_message_id",
            "parent_message_id",
            "pre_send_current_node",
        )
        fields: dict[str, Any] = {
            name: (str(value[name]) if value.get(name) is not None else None) for name in names
        }
        raw_sources = value.get("sources")
        fields["sources"] = (
            {str(key): str(item) for key, item in raw_sources.items()}
            if isinstance(raw_sources, dict)
            else {}
        )
        return cls(**fields)

    def safe_summary(self) -> dict[str, str | None]:
        def short(item: str | None) -> str | None:
            if item is None:
                return None
            return item if len(item) <= 16 else f"{item[:8]}…{item[-4:]}"

        return {
            key: short(value)
            for key, value in self.to_dict().items()
            if key != "sources" and isinstance(value, (str, type(None)))
        }


@dataclass(frozen=True, slots=True)
class TurnRecord:
    schema_version: int
    request_id: str
    state: TurnState
    send_provenance: SendProvenance
    prompt_sha256: str
    prompt_length: int
    target_kind: str
    target_conversation_id: str | None
    identity: TurnIdentity | None
    baseline_node_fingerprints: dict[str, str]
    revision: int
    created_at: str
    updated_at: str
    failure: Failure | None = None
    response_sha256: str | None = None
    response_length: int | None = None
    cancellation_requested_at: str | None = None

    @classmethod
    def new(
        cls,
        *,
        request_id: str,
        prompt: str,
        target_kind: str = "fresh",
        target_conversation_id: str | None = None,
    ) -> TurnRecord:
        if target_kind not in {"fresh", "conversation"}:
            raise ValueError("target_kind must be fresh or conversation")
        if target_kind == "conversation" and not target_conversation_id:
            raise ValueError("conversation target requires target_conversation_id")
        now = utc_now()
        return cls(
            schema_version=2,
            request_id=request_id,
            state=TurnState.NEW,
            send_provenance=SendProvenance.NOT_ATTEMPTED,
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            prompt_length=len(prompt),
            target_kind=target_kind,
            target_conversation_id=target_conversation_id,
            identity=None,
            baseline_node_fingerprints={},
            revision=0,
            created_at=now,
            updated_at=now,
        )

    @property
    def retry_allowed(self) -> bool:
        return self.send_provenance in {
            SendProvenance.NOT_ATTEMPTED,
            SendProvenance.SAFE_TO_RETRY,
        } and self.state not in {TurnState.COMPLETE, TurnState.CANCELLED}

    @property
    def terminal(self) -> bool:
        return self.state in {TurnState.COMPLETE, TurnState.FAILED, TurnState.CANCELLED}

    def transition(self, state: TurnState, *, failure: Failure | None = None) -> TurnRecord:
        if state == self.state:
            return replace(self, failure=failure or self.failure, updated_at=utc_now())
        if state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"invalid turn transition {self.state.value} -> {state.value}")
        provenance = self.send_provenance
        if state == TurnState.UNKNOWN and provenance not in {
            SendProvenance.NOT_ATTEMPTED,
            SendProvenance.SAFE_TO_RETRY,
        }:
            provenance = SendProvenance.RETRY_PROHIBITED
        return replace(
            self,
            state=state,
            send_provenance=provenance,
            failure=failure,
            updated_at=utc_now(),
        )

    def with_send_provenance(self, value: SendProvenance) -> TurnRecord:
        allowed_order = {
            SendProvenance.NOT_ATTEMPTED: 0,
            SendProvenance.CLICK_BOUNDARY_ENTERED: 1,
            SendProvenance.FRONTEND_ACCEPTED: 2,
            SendProvenance.USER_IDENTITY_BOUND: 3,
            SendProvenance.DURABLE_HANDOFF: 4,
        }
        current_rank = allowed_order.get(self.send_provenance)
        new_rank = allowed_order.get(value)
        if current_rank is not None and new_rank is not None and new_rank < current_rank:
            raise ValueError("send provenance cannot move backwards")
        return replace(self, send_provenance=value, updated_at=utc_now())

    def with_identity(self, identity: TurnIdentity) -> TurnRecord:
        return replace(self, identity=identity, updated_at=utc_now())

    def with_baseline(self, fingerprints: dict[str, str]) -> TurnRecord:
        return replace(self, baseline_node_fingerprints=dict(fingerprints), updated_at=utc_now())

    def with_response(self, response: str) -> TurnRecord:
        return replace(
            self,
            response_sha256=hashlib.sha256(response.encode("utf-8")).hexdigest(),
            response_length=len(response),
            updated_at=utc_now(),
        )

    def request_cancellation(self) -> TurnRecord:
        return replace(self, cancellation_requested_at=utc_now(), updated_at=utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "state": self.state.value,
            "send_provenance": self.send_provenance.value,
            "prompt_sha256": self.prompt_sha256,
            "prompt_length": self.prompt_length,
            "target_kind": self.target_kind,
            "target_conversation_id": self.target_conversation_id,
            "identity": self.identity.to_dict() if self.identity else None,
            "baseline_node_fingerprints": dict(self.baseline_node_fingerprints),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "failure": self.failure.to_dict() if self.failure else None,
            "response_sha256": self.response_sha256,
            "response_length": self.response_length,
            "cancellation_requested_at": self.cancellation_requested_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TurnRecord:
        from .errors import FailureCategory

        if value.get("schema_version") != 2:
            raise ValueError("unsupported turn record schema")
        raw_failure = value.get("failure")
        failure = None
        if raw_failure is not None:
            if not isinstance(raw_failure, dict):
                raise ValueError("failure must be an object")
            failure = Failure(
                FailureCategory(str(raw_failure["category"])),
                str(raw_failure["message"]),
                bool(raw_failure.get("retryable")),
                bool(raw_failure.get("external")),
            )
        raw_baseline = value.get("baseline_node_fingerprints")
        if not isinstance(raw_baseline, dict):
            raise ValueError("baseline_node_fingerprints must be an object")
        return cls(
            schema_version=2,
            request_id=str(value["request_id"]),
            state=TurnState(str(value["state"])),
            send_provenance=SendProvenance(str(value["send_provenance"])),
            prompt_sha256=str(value["prompt_sha256"]),
            prompt_length=int(value["prompt_length"]),
            target_kind=str(value["target_kind"]),
            target_conversation_id=(
                str(value["target_conversation_id"])
                if value.get("target_conversation_id") is not None
                else None
            ),
            identity=TurnIdentity.from_dict(value.get("identity")),
            baseline_node_fingerprints={str(k): str(v) for k, v in raw_baseline.items()},
            revision=int(value["revision"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            failure=failure,
            response_sha256=(str(value["response_sha256"]) if value.get("response_sha256") else None),
            response_length=(
                int(value["response_length"])
                if value.get("response_length") is not None
                else None
            ),
            cancellation_requested_at=(
                str(value["cancellation_requested_at"])
                if value.get("cancellation_requested_at")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    schema_version: int
    conversation_id: str
    active_request_id: str | None
    revision: int
    updated_at: str
    last_terminal_request_id: str | None = None

    @classmethod
    def new(cls, conversation_id: str) -> ConversationRecord:
        return cls(1, conversation_id, None, 0, utc_now())

    def claim(self, request_id: str) -> ConversationRecord:
        if self.active_request_id not in {None, request_id}:
            raise ValueError(f"conversation already claimed by {self.active_request_id}")
        return replace(self, active_request_id=request_id, updated_at=utc_now())

    def release(self, request_id: str, *, terminal: bool) -> ConversationRecord:
        if self.active_request_id not in {None, request_id}:
            raise ValueError("cannot release another request owner")
        return replace(
            self,
            active_request_id=None,
            last_terminal_request_id=request_id if terminal else self.last_terminal_request_id,
            updated_at=utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "conversation_id": self.conversation_id,
            "active_request_id": self.active_request_id,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "last_terminal_request_id": self.last_terminal_request_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ConversationRecord:
        if value.get("schema_version") != 1:
            raise ValueError("unsupported conversation record schema")
        return cls(
            schema_version=1,
            conversation_id=str(value["conversation_id"]),
            active_request_id=(
                str(value["active_request_id"]) if value.get("active_request_id") else None
            ),
            revision=int(value["revision"]),
            updated_at=str(value["updated_at"]),
            last_terminal_request_id=(
                str(value["last_terminal_request_id"])
                if value.get("last_terminal_request_id")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class Result:
    schema_version: int
    request_id: str
    state: TurnState
    response: str | None = None
    identity: TurnIdentity | None = None
    failure: Failure | None = None

    @property
    def success(self) -> bool:
        return self.state == TurnState.COMPLETE and self.response is not None and self.failure is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "state": self.state.value,
            "success": self.success,
            "response": self.response,
            "identity": self.identity.safe_summary() if self.identity else None,
            "failure": self.failure.to_dict() if self.failure else None,
        }
