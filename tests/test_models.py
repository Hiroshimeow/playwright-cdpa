from __future__ import annotations

import pytest

from playwright_gpt_core.models import SendProvenance, TurnRecord, TurnState


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
