from __future__ import annotations

import pytest

from playwright_gpt_core.config import CoreConfig
from playwright_gpt_core.models import TurnRecord, TurnState
from playwright_gpt_core.service import ChatGPTCore


@pytest.mark.asyncio
async def test_cancel_before_send_is_proven_without_browser(tmp_path) -> None:
    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    core.store.save(TurnRecord.new(request_id="req-1", prompt="draft"))
    result = await core.cancel("req-1")
    assert result.state == TurnState.CANCELLED


@pytest.mark.asyncio
async def test_watch_without_exact_identity_fails_closed_without_browser(tmp_path) -> None:
    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    core.store.save(TurnRecord.new(request_id="req-1", prompt="draft"))
    result = await core.watch("req-1")
    assert result.failure is not None
    assert result.failure.category.value == "identity_missing"


@pytest.mark.asyncio
async def test_duplicate_public_request_id_fails_before_browser_access(tmp_path) -> None:
    from playwright_gpt_core.errors import OwnershipConflictError

    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    core.store.create(TurnRecord.new(request_id="duplicate", prompt="first"))
    with pytest.raises(OwnershipConflictError):
        await core.send("second", fresh=True, request_id="duplicate")


@pytest.mark.asyncio
async def test_preparing_cancel_blocks_stale_sender_before_click_boundary(tmp_path) -> None:
    from playwright_gpt_core.errors import ConcurrentStateError
    from playwright_gpt_core.models import SendProvenance

    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    record = core.store.create(
        TurnRecord.new(
            request_id="preparing",
            prompt="draft",
            target_kind="conversation",
            target_conversation_id="conversation-1",
        )
    )
    core.store.claim_conversation("conversation-1", "preparing")
    preparing = core.store.save(
        record.transition(TurnState.PREPARING), expected_revision=record.revision
    )

    result = await core.cancel("preparing")

    assert result.state == TurnState.CANCELLED
    assert core.store.load_conversation("conversation-1").active_request_id is None
    with pytest.raises(ConcurrentStateError):
        core.store.save(
            preparing.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED),
            expected_revision=preparing.revision,
        )
    assert core.store.load("preparing").send_provenance == SendProvenance.NOT_ATTEMPTED
