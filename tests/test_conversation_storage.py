from __future__ import annotations

import pytest

from playwright_api.errors import OwnershipConflictError
from playwright_api.storage import StateStore


def test_durable_conversation_claim_blocks_second_sender(tmp_path) -> None:
    store = StateStore(tmp_path)
    first = store.claim_conversation("conv-1", "req-1")
    assert first.active_request_id == "req-1"
    with pytest.raises(OwnershipConflictError):
        store.claim_conversation("conv-1", "req-2")
    released = store.release_conversation("conv-1", "req-1", terminal=True)
    assert released.active_request_id is None
    assert released.last_terminal_request_id == "req-1"


def test_conversation_claim_survives_new_store_instance(tmp_path) -> None:
    StateStore(tmp_path).claim_conversation("conv-1", "req-1")
    assert StateStore(tmp_path).load_conversation("conv-1").active_request_id == "req-1"


def test_release_if_owned_is_idempotent_and_preserves_new_owner(tmp_path) -> None:
    store = StateStore(tmp_path)
    store.claim_conversation("conv-1", "completed-request")

    first = store.release_conversation_if_owned("conv-1", "completed-request", terminal=True)
    assert first.active_request_id is None
    assert first.last_terminal_request_id == "completed-request"

    claimed = store.claim_conversation("conv-1", "next-request")
    stale = store.release_conversation_if_owned("conv-1", "completed-request", terminal=True)

    assert stale.active_request_id == "next-request"
    assert stale.revision == claimed.revision
    assert store.load_conversation("conv-1").active_request_id == "next-request"


def test_release_if_owned_is_noop_for_empty_or_foreign_owner(tmp_path) -> None:
    store = StateStore(tmp_path)
    empty = store.release_conversation_if_owned("conv-1", "stale", terminal=True)
    assert empty.active_request_id is None
    assert empty.revision == 0

    claimed = store.claim_conversation("conv-1", "foreign")
    foreign = store.release_conversation_if_owned("conv-1", "stale", terminal=True)
    assert foreign.active_request_id == "foreign"
    assert foreign.revision == claimed.revision
