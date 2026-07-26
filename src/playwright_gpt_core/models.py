from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .errors import Failure
from .schema import (
    decode_bounded_text,
    decode_enum,
    decode_identifier,
    decode_optional_identifier,
    decode_required_bool,
    decode_required_int,
    decode_sha256,
    decode_timestamp,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_helper_target_id(value: str) -> str:
    if type(value) is not str:
        raise ValueError("helper_page_target_id must be a string")
    if not value or len(value) > 256:
        raise ValueError("helper_page_target_id must contain 1 to 256 characters")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ValueError("helper_page_target_id must contain printable ASCII without spaces")
    return value


def _validate_helper_closed_at(value: str) -> str:
    if not value or len(value) > 64:
        raise ValueError("helper_page_closed_at must be a bounded ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("helper_page_closed_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("helper_page_closed_at must include a timezone")
    return value


def _decode_helper_ownership(
    value: dict[str, Any], raw_schema: int
) -> tuple[str | None, bool, str | None]:
    if raw_schema < 4:
        return None, False, None

    required = (
        "helper_page_target_id",
        "helper_page_keep",
        "helper_page_closed_at",
    )
    for field_name in required:
        if field_name not in value:
            raise ValueError(f"schema 4 requires {field_name}")

    raw_target = value["helper_page_target_id"]
    if raw_target is None:
        target_id = None
    elif type(raw_target) is not str:
        raise ValueError("helper_page_target_id must be a string or null")
    else:
        target_id = _validate_helper_target_id(raw_target)

    raw_keep = value["helper_page_keep"]
    if type(raw_keep) is not bool:
        raise ValueError("helper_page_keep must be exactly boolean")
    keep = raw_keep

    raw_closed_at = value["helper_page_closed_at"]
    if raw_closed_at is None:
        closed_at = None
    elif type(raw_closed_at) is not str:
        raise ValueError("helper_page_closed_at must be a string or null")
    else:
        closed_at = _validate_helper_closed_at(raw_closed_at)

    if target_id is None and keep:
        raise ValueError("helper_page_keep=true requires helper_page_target_id")
    if target_id is None and closed_at is not None:
        raise ValueError("helper_page_closed_at requires helper_page_target_id")
    return target_id, keep, closed_at


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
        {
            TurnState.SENT,
            TurnState.RUNNING,
            TurnState.UNKNOWN,
            TurnState.CANCELLED,
            TurnState.FAILED,
        }
    ),
    TurnState.SENT: frozenset(
        {
            TurnState.RUNNING,
            TurnState.COMPLETE,
            TurnState.UNKNOWN,
            TurnState.CANCELLED,
            TurnState.FAILED,
        }
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
    transport_turn_exchange_id: str | None = None
    transport_request_id: str | None = None
    stream_topic_id: str | None = None
    turn_exchange_id: str | None = None
    request_id: str | None = None
    working_turn_id: str | None = None
    user_message_id: str | None = None
    frontend_parent_message_id: str | None = None
    parent_message_id: str | None = None
    pre_send_current_node: str | None = None
    sources: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "conversation_id",
            "transport_turn_exchange_id",
            "transport_request_id",
            "stream_topic_id",
            "turn_exchange_id",
            "request_id",
            "working_turn_id",
            "user_message_id",
            "frontend_parent_message_id",
            "parent_message_id",
            "pre_send_current_node",
        ):
            decode_optional_identifier(getattr(self, name), f"identity.{name}")
        validated_sources: dict[str, str] = {}
        for raw_key, raw_value in self.sources.items():
            key = decode_identifier(raw_key, "identity source key", max_length=160)
            value = decode_identifier(raw_value, "identity source value", max_length=160)
            assert key is not None and value is not None
            validated_sources[key] = value
        object.__setattr__(self, "sources", validated_sources)

    @property
    def has_transport_correlation(self) -> bool:
        return bool(
            self.transport_turn_exchange_id or self.transport_request_id or self.stream_topic_id
        )

    @property
    def has_graph_correlation(self) -> bool:
        return bool(self.turn_exchange_id or self.request_id or self.working_turn_id)

    @property
    def monitorable(self) -> bool:
        return bool(
            self.conversation_id and self.user_message_id and self.has_graph_correlation
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "transport_turn_exchange_id": self.transport_turn_exchange_id,
            "transport_request_id": self.transport_request_id,
            "stream_topic_id": self.stream_topic_id,
            "turn_exchange_id": self.turn_exchange_id,
            "request_id": self.request_id,
            "working_turn_id": self.working_turn_id,
            "user_message_id": self.user_message_id,
            "frontend_parent_message_id": self.frontend_parent_message_id,
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
            "transport_turn_exchange_id",
            "transport_request_id",
            "stream_topic_id",
            "turn_exchange_id",
            "request_id",
            "working_turn_id",
            "user_message_id",
            "frontend_parent_message_id",
            "parent_message_id",
            "pre_send_current_node",
        )
        fields: dict[str, Any] = {
            name: decode_optional_identifier(value.get(name), f"identity.{name}")
            for name in names
        }
        raw_sources = value.get("sources", {})
        if not isinstance(raw_sources, dict):
            raise ValueError("identity sources must be an object")
        sources: dict[str, str] = {}
        for raw_key, raw_item in raw_sources.items():
            key = decode_identifier(raw_key, "identity source key", max_length=160)
            item = decode_identifier(raw_item, "identity source value", max_length=160)
            assert key is not None and item is not None
            sources[key] = item
        fields["sources"] = sources
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
    helper_page_target_id: str | None = None
    helper_page_keep: bool = False
    helper_page_closed_at: str | None = None

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
            schema_version=4,
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
        return replace(
            self,
            baseline_node_fingerprints=dict(fingerprints),
            updated_at=utc_now(),
        )

    def with_response(self, response: str) -> TurnRecord:
        return replace(
            self,
            response_sha256=hashlib.sha256(response.encode("utf-8")).hexdigest(),
            response_length=len(response),
            updated_at=utc_now(),
        )

    def request_cancellation(self) -> TurnRecord:
        return replace(self, cancellation_requested_at=utc_now(), updated_at=utc_now())

    def with_helper_page(self, target_id: str, *, keep: bool) -> TurnRecord:
        if type(keep) is not bool:
            raise ValueError("helper_page_keep must be exactly boolean")
        target_id = _validate_helper_target_id(target_id)
        if self.helper_page_target_id not in {None, target_id}:
            raise ValueError("helper target identity cannot change")
        return replace(
            self,
            helper_page_target_id=target_id,
            helper_page_keep=keep,
            helper_page_closed_at=None,
            updated_at=utc_now(),
        )

    def with_helper_page_closed(self) -> TurnRecord:
        if self.helper_page_target_id is None:
            return self
        return replace(self, helper_page_closed_at=utc_now(), updated_at=utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 4,
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
            "helper_page_target_id": self.helper_page_target_id,
            "helper_page_keep": self.helper_page_keep,
            "helper_page_closed_at": self.helper_page_closed_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TurnRecord:
        from .errors import FailureCategory

        if type(value) is not dict:
            raise ValueError("turn record must be an object")
        raw_schema = decode_required_int(value.get("schema_version"), "schema_version")
        if raw_schema not in {2, 3, 4}:
            raise ValueError("unsupported turn record schema")

        known_fields = {
            "schema_version",
            "request_id",
            "state",
            "send_provenance",
            "prompt_sha256",
            "prompt_length",
            "target_kind",
            "target_conversation_id",
            "identity",
            "baseline_node_fingerprints",
            "revision",
            "created_at",
            "updated_at",
            "failure",
            "response_sha256",
            "response_length",
            "cancellation_requested_at",
            "helper_page_target_id",
            "helper_page_keep",
            "helper_page_closed_at",
        }
        unknown = set(value) - known_fields
        if unknown:
            raise ValueError(f"turn record contains unsupported fields: {sorted(unknown)!r}")
        required = known_fields - {
            "helper_page_target_id",
            "helper_page_keep",
            "helper_page_closed_at",
        }
        missing = required - set(value)
        if missing:
            raise ValueError(f"turn record is missing fields: {sorted(missing)!r}")

        raw_failure = value["failure"]
        failure = None
        if raw_failure is not None:
            if type(raw_failure) is not dict:
                raise ValueError("failure must be an object")
            required_failure = {"category", "message", "retryable", "external"}
            if set(raw_failure) != required_failure:
                raise ValueError(
                    "failure must contain exactly category, message, retryable, external"
                )
            failure = Failure(
                decode_enum(raw_failure["category"], "failure.category", FailureCategory),
                decode_bounded_text(raw_failure["message"], "failure.message", max_length=8192)
                or "",
                decode_required_bool(raw_failure["retryable"], "failure.retryable"),
                decode_required_bool(raw_failure["external"], "failure.external"),
            )

        raw_baseline = value["baseline_node_fingerprints"]
        if type(raw_baseline) is not dict:
            raise ValueError("baseline_node_fingerprints must be an object")
        baseline: dict[str, str] = {}
        for raw_key, raw_fingerprint in raw_baseline.items():
            key = decode_identifier(raw_key, "baseline node id")
            fingerprint = decode_identifier(
                raw_fingerprint, "baseline fingerprint", max_length=128
            )
            assert key is not None and fingerprint is not None
            baseline[key] = fingerprint

        raw_identity = value["identity"]
        if raw_schema == 2 and isinstance(raw_identity, dict):
            migrated = dict(raw_identity)
            old_turn = migrated.pop("turn_exchange_id", None)
            old_request = migrated.pop("request_id", None)
            old_parent = migrated.pop("parent_message_id", None)
            migrated["transport_turn_exchange_id"] = old_turn
            migrated["transport_request_id"] = old_request
            migrated["frontend_parent_message_id"] = old_parent
            migrated["turn_exchange_id"] = None
            migrated["request_id"] = None
            migrated["working_turn_id"] = None
            migrated["parent_message_id"] = None
            raw_sources = migrated.get("sources")
            if type(raw_sources) is not dict:
                raise ValueError("schema 2 identity sources must be an object")
            sources = dict(raw_sources)
            if old_turn is not None:
                sources["transport_turn_exchange_id"] = sources.pop(
                    "turn_exchange_id", "schema-2-migration"
                )
            if old_request is not None:
                sources["transport_request_id"] = sources.pop(
                    "request_id", "schema-2-migration"
                )
            if old_parent is not None:
                sources["frontend_parent_message_id"] = sources.pop(
                    "parent_message_id", "schema-2-migration"
                )
            migrated["sources"] = sources
            raw_identity = migrated

        state = decode_enum(value["state"], "state", TurnState)
        provenance = decode_enum(value["send_provenance"], "send_provenance", SendProvenance)
        target_kind = decode_identifier(value["target_kind"], "target_kind", max_length=32)
        if target_kind not in {"fresh", "conversation"}:
            raise ValueError("target_kind must be fresh or conversation")
        target_conversation_id = decode_optional_identifier(
            value["target_conversation_id"], "target_conversation_id"
        )
        if target_kind == "conversation" and target_conversation_id is None:
            raise ValueError("conversation target requires target_conversation_id")
        if target_kind == "fresh" and target_conversation_id is not None:
            raise ValueError("fresh target must not contain target_conversation_id")

        response_sha256 = decode_sha256(
            value["response_sha256"], "response_sha256", optional=True
        )
        response_length = (
            None
            if value["response_length"] is None
            else decode_required_int(value["response_length"], "response_length")
        )
        if (response_sha256 is None) != (response_length is None):
            raise ValueError("response_sha256 and response_length must be present together")
        if state == TurnState.COMPLETE and response_sha256 is None:
            raise ValueError("COMPLETE state requires response metadata")
        if state != TurnState.COMPLETE and response_sha256 is not None:
            raise ValueError("response metadata is allowed only for COMPLETE state")

        if failure is not None and state not in {TurnState.FAILED, TurnState.UNKNOWN}:
            raise ValueError("failure is allowed only for FAILED or UNKNOWN state")
        if state == TurnState.FAILED and failure is None:
            raise ValueError("FAILED state requires failure")

        valid_provenance = {
            TurnState.NEW: {SendProvenance.NOT_ATTEMPTED},
            TurnState.PREPARING: {
                SendProvenance.NOT_ATTEMPTED,
                SendProvenance.CLICK_BOUNDARY_ENTERED,
            },
            TurnState.SENT: {SendProvenance.FRONTEND_ACCEPTED},
            TurnState.RUNNING: {
                SendProvenance.DURABLE_HANDOFF,
                SendProvenance.RETRY_PROHIBITED,
            },
            TurnState.COMPLETE: {
                SendProvenance.DURABLE_HANDOFF,
                SendProvenance.RETRY_PROHIBITED,
            },
            TurnState.FAILED: {
                SendProvenance.NOT_ATTEMPTED,
                SendProvenance.SAFE_TO_RETRY,
            },
            TurnState.UNKNOWN: {SendProvenance.RETRY_PROHIBITED},
            TurnState.CANCELLED: set(SendProvenance),
        }
        if provenance not in valid_provenance[state]:
            raise ValueError(
                f"state {state.value} is incompatible with provenance {provenance.value}"
            )

        identity = TurnIdentity.from_dict(raw_identity)
        if (
            target_conversation_id is not None
            and identity is not None
            and identity.conversation_id is not None
            and identity.conversation_id != target_conversation_id
        ):
            raise ValueError("target and identity conversation IDs conflict")

        helper_target_id, helper_keep, helper_closed_at = _decode_helper_ownership(
            value, raw_schema
        )
        return cls(
            schema_version=4,
            request_id=decode_identifier(value["request_id"], "request_id") or "",
            state=state,
            send_provenance=provenance,
            prompt_sha256=decode_sha256(value["prompt_sha256"], "prompt_sha256") or "",
            prompt_length=decode_required_int(value["prompt_length"], "prompt_length"),
            target_kind=target_kind,
            target_conversation_id=target_conversation_id,
            identity=identity,
            baseline_node_fingerprints=baseline,
            revision=decode_required_int(value["revision"], "revision"),
            created_at=decode_timestamp(value["created_at"], "created_at") or "",
            updated_at=decode_timestamp(value["updated_at"], "updated_at") or "",
            failure=failure,
            response_sha256=response_sha256,
            response_length=response_length,
            cancellation_requested_at=decode_timestamp(
                value["cancellation_requested_at"],
                "cancellation_requested_at",
                optional=True,
            ),
            helper_page_target_id=helper_target_id,
            helper_page_keep=helper_keep,
            helper_page_closed_at=helper_closed_at,
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
        if type(value) is not dict:
            raise ValueError("conversation record must be an object")
        expected = {
            "schema_version",
            "conversation_id",
            "active_request_id",
            "revision",
            "updated_at",
            "last_terminal_request_id",
        }
        if set(value) != expected:
            raise ValueError("conversation record has an invalid field set")
        schema_version = decode_required_int(value["schema_version"], "schema_version")
        if schema_version != 1:
            raise ValueError("unsupported conversation record schema")
        return cls(
            schema_version=1,
            conversation_id=decode_identifier(value["conversation_id"], "conversation_id")
            or "",
            active_request_id=decode_optional_identifier(
                value["active_request_id"], "active_request_id"
            ),
            revision=decode_required_int(value["revision"], "revision"),
            updated_at=decode_timestamp(value["updated_at"], "updated_at") or "",
            last_terminal_request_id=decode_optional_identifier(
                value["last_terminal_request_id"],
                "last_terminal_request_id",
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
        return (
            self.state == TurnState.COMPLETE
            and self.response is not None
            and self.failure is None
        )

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
