from __future__ import annotations

import pytest

from playwright_gpt_core.config import CoreConfig
from playwright_gpt_core.models import SendProvenance, TurnIdentity, TurnRecord, TurnState
from playwright_gpt_core.service import ChatGPTCore
from tests.fixtures.graph_factory import graph, message


class Backend:
    def __init__(self, snapshot_graph):
        self.snapshot_graph = snapshot_graph

    async def snapshot(self, conversation_id: str):
        from playwright_gpt_core.monitor import MonitorSnapshot

        return MonitorSnapshot("COMPLETE", self.snapshot_graph)


@pytest.mark.asyncio
async def test_uncertain_existing_send_reconciles_one_graph_delta_without_resend(tmp_path) -> None:
    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("old-final", "assistant", "root", text="old", turn="old-turn", request="old-request"),
        message("new-user", "user", "old-final", text="unknown-body", turn="new-turn", request="new-request"),
        message("new-final", "assistant", "new-user", text="NEW", turn="new-turn", request="new-request"),
        current="new-final",
    )
    record = TurnRecord.new(
        request_id="req-1",
        prompt="not persisted",
        target_kind="conversation",
        target_conversation_id="conversation-1",
    ).transition(TurnState.PREPARING)
    record = record.with_identity(
        TurnIdentity(
            conversation_id="conversation-1",
            pre_send_current_node="old-final",
        )
    ).with_baseline({"root": "a", "old-final": "b"})
    record = record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
    record = record.transition(TurnState.UNKNOWN)
    record = core.store.save(record)
    core.store.claim_conversation("conversation-1", "req-1")
    identity = await core._ensure_monitorable_identity(record, Backend(snapshot))  # type: ignore[arg-type]
    assert identity.user_message_id == "new-user"
    assert identity.turn_exchange_id == "new-turn"
    assert identity.request_id == "new-request"
    assert core.store.load("req-1").state == TurnState.RUNNING


@pytest.mark.asyncio
async def test_uncertain_send_without_graph_anchor_cannot_reconcile(tmp_path) -> None:
    core = ChatGPTCore(CoreConfig(state_dir=tmp_path))
    record = TurnRecord.new(request_id="req-1", prompt="not persisted")
    record = record.transition(TurnState.PREPARING)
    record = record.with_identity(TurnIdentity(conversation_id="conversation-1"))
    record = record.with_send_provenance(SendProvenance.CLICK_BOUNDARY_ENTERED)
    record = core.store.save(record.transition(TurnState.UNKNOWN))
    with pytest.raises(Exception):
        await core._ensure_monitorable_identity(record, Backend({}))  # type: ignore[arg-type]
