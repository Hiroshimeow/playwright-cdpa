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


def _write_schema_four_helper_variant(store: StateStore, request_id: str, **changes) -> str:
    record = TurnRecord.new(request_id=request_id, prompt="prompt").to_dict()
    record.update(changes)
    path = store.turn_path(request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(record, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")
    return raw


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"helper_page_keep": "false"}, "helper_page_keep"),
        ({"helper_page_keep": []}, "helper_page_keep"),
        ({"helper_page_keep": {}}, "helper_page_keep"),
        ({"helper_page_keep": None}, "helper_page_keep"),
        ({"helper_page_keep": 0}, "helper_page_keep"),
        ({"helper_page_target_id": 123}, "helper_page_target_id"),
        ({"helper_page_target_id": []}, "helper_page_target_id"),
        ({"helper_page_target_id": ""}, "helper_page_target_id"),
        ({"helper_page_target_id": "bad\ncontrol"}, "helper_page_target_id"),
        ({"helper_page_target_id": "x" * 257}, "helper_page_target_id"),
        ({"helper_page_closed_at": 123}, "helper_page_closed_at"),
        ({"helper_page_closed_at": []}, "helper_page_closed_at"),
        ({"helper_page_closed_at": "not-a-timestamp"}, "helper_page_closed_at"),
        ({"helper_page_closed_at": "2026-07-25T08:00:00"}, "helper_page_closed_at"),
        (
            {
                "helper_page_target_id": None,
                "helper_page_closed_at": "2026-07-25T08:00:00+00:00",
            },
            "requires helper_page_target_id",
        ),
        (
            {"helper_page_target_id": None, "helper_page_keep": True},
            "requires helper_page_target_id",
        ),
    ],
)
def test_schema_four_rejects_malformed_helper_ownership(
    tmp_path, changes, message
) -> None:
    store = StateStore(tmp_path)
    raw = _write_schema_four_helper_variant(store, "bad-helper", **changes)

    with pytest.raises(CorruptStateError, match=message):
        store.load("bad-helper")

    assert store.turn_path("bad-helper").read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    "missing_field",
    ["helper_page_target_id", "helper_page_keep", "helper_page_closed_at"],
)
def test_schema_four_requires_all_helper_ownership_fields(
    tmp_path, missing_field
) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="missing-helper", prompt="prompt").to_dict()
    value.pop(missing_field)
    path = store.turn_path("missing-helper")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(CorruptStateError, match=missing_field):
        store.load("missing-helper")


def test_schema_three_ignores_untrusted_helper_fields(tmp_path) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="schema-three", prompt="prompt").to_dict()
    value["schema_version"] = 3
    value["helper_page_target_id"] = "injected-target"
    value["helper_page_keep"] = True
    value["helper_page_closed_at"] = "2026-07-25T08:00:00+00:00"
    path = store.turn_path("schema-three")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")

    loaded = store.load("schema-three")

    assert loaded.schema_version == 4
    assert loaded.helper_page_target_id is None
    assert loaded.helper_page_keep is False
    assert loaded.helper_page_closed_at is None
