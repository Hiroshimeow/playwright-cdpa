from __future__ import annotations

import json

import pytest

from playwright_gpt_core.errors import CorruptStateError
from playwright_gpt_core.models import TurnRecord, TurnState
from playwright_gpt_core.storage import StateStore


def test_atomic_round_trip_and_revision(tmp_path) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id="req-a", prompt="hello")
    saved = store.save(record)
    assert saved.revision == 1
    loaded = store.load("req-a")
    assert loaded.request_id == "req-a"
    assert loaded.revision == 1
    saved2 = store.save(loaded.transition(TurnState.PREPARING), expected_revision=1)
    assert saved2.revision == 2


def test_corrupt_state_is_preserved_and_rejected(tmp_path) -> None:
    store = StateStore(tmp_path)
    path = store.turn_path("req-b")
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(CorruptStateError):
        store.load("req-b")
    assert path.read_text(encoding="utf-8") == "{broken"


def test_state_file_contains_no_prompt_or_secret(tmp_path) -> None:
    store = StateStore(tmp_path)
    store.save(TurnRecord.new(request_id="req-c", prompt="Bearer top.secret.value"))
    raw = store.turn_path("req-c").read_text(encoding="utf-8")
    assert "Bearer" not in raw
    assert "top.secret.value" not in raw
    json.loads(raw)


def test_create_rejects_duplicate_request_identity(tmp_path) -> None:
    from playwright_gpt_core.errors import OwnershipConflictError

    store = StateStore(tmp_path)
    store.create(TurnRecord.new(request_id="same-request", prompt="first"))
    with pytest.raises(OwnershipConflictError):
        store.create(TurnRecord.new(request_id="same-request", prompt="second"))
    persisted = store.load("same-request")
    assert persisted.prompt_sha256 == TurnRecord.new(
        request_id="same-request", prompt="first"
    ).prompt_sha256
