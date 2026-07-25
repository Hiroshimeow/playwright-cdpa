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
