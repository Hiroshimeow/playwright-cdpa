from __future__ import annotations

import pytest

from playwright_gpt_core.models import SendProvenance, TurnIdentity, TurnRecord, TurnState


def test_uncertain_send_is_never_retryable() -> None:
    record = TurnRecord.new(request_id="req-1", prompt="hello")
    record = record.transition(TurnState.PREPARING)
    record = record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
    record = record.transition(TurnState.UNKNOWN)
    assert record.retry_allowed is False
    assert record.send_provenance == SendProvenance.RETRY_PROHIBITED


def test_click_boundary_can_be_durably_round_tripped(tmp_path) -> None:
    from playwright_gpt_core.storage import StateStore

    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id="req-1", prompt="hello").transition(TurnState.PREPARING)
    saved = store.save(record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED))
    recovered = StateStore(tmp_path).load("req-1")
    assert recovered.revision == saved.revision
    assert recovered.send_provenance == SendProvenance.CLICK_BOUNDARY_ENTERED
    assert recovered.retry_allowed is False


def test_invalid_terminal_transition_rejected() -> None:
    record = TurnRecord.new(request_id="req-1", prompt="hello").transition(TurnState.CANCELLED)
    with pytest.raises(ValueError):
        record.transition(TurnState.RUNNING)


def test_prompt_body_is_not_serialized() -> None:
    record = TurnRecord.new(request_id="req-1", prompt="secret prompt body")
    payload = record.to_dict()
    assert "secret prompt body" not in str(payload)
    assert payload["prompt_length"] == len("secret prompt body")


def test_helper_target_ownership_round_trips_in_schema_four(tmp_path) -> None:
    from playwright_gpt_core.storage import StateStore

    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id="helper", prompt="hello").with_helper_page(
        "target-123", keep=False
    )
    saved = store.save(record)
    loaded = store.load("helper")
    assert saved.schema_version == 4
    assert loaded.helper_page_target_id == "target-123"
    assert loaded.helper_page_closed_at is None
    closed = store.save(loaded.with_helper_page_closed(), expected_revision=loaded.revision)
    assert closed.helper_page_closed_at is not None


def test_schema_three_record_migrates_without_helper_ownership() -> None:
    payload = TurnRecord.new(request_id="legacy", prompt="hello").to_dict()
    payload["schema_version"] = 3
    payload.pop("helper_page_target_id")
    payload.pop("helper_page_keep")
    payload.pop("helper_page_closed_at")
    migrated = TurnRecord.from_dict(payload)
    assert migrated.schema_version == 4
    assert migrated.helper_page_target_id is None
    assert migrated.helper_page_keep is False
    assert migrated.helper_page_closed_at is None


@pytest.mark.parametrize(
    "malformed",
    [123, False, [], {}, "", "   ", "bad\u0000identity"],
)
def test_persisted_turn_identity_rejects_noncanonical_identifiers(malformed) -> None:
    payload = TurnIdentity(conversation_id="conversation-1").to_dict()
    payload["conversation_id"] = malformed

    with pytest.raises(ValueError, match="conversation_id"):
        TurnIdentity.from_dict(payload)


def test_persisted_identity_sources_require_string_keys_and_values() -> None:
    payload = TurnIdentity(conversation_id="conversation-1").to_dict()
    payload["sources"] = {"conversation_id": ["frontend"]}

    with pytest.raises(ValueError, match="identity source"):
        TurnIdentity.from_dict(payload)


@pytest.mark.parametrize("missing", ["conversation_id", "sources"])
def test_persisted_identity_requires_exact_field_set(missing: str) -> None:
    payload = TurnIdentity(conversation_id="conversation-1").to_dict()
    payload.pop(missing)

    with pytest.raises(ValueError, match="identity"):
        TurnIdentity.from_dict(payload)


def test_persisted_identity_rejects_unsupported_fields() -> None:
    payload = TurnIdentity(conversation_id="conversation-1").to_dict()
    payload["unexpected_runtime_pointer"] = "foreign-id"

    with pytest.raises(ValueError, match="unsupported"):
        TurnIdentity.from_dict(payload)
