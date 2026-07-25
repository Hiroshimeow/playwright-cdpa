from __future__ import annotations

import pytest

from playwright_gpt_core.errors import OwnershipConflictError
from playwright_gpt_core.storage import StateStore


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
