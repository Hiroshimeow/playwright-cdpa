from __future__ import annotations

import json

import pytest

from playwright_gpt_core.errors import ConflictingIdentityError
from playwright_gpt_core.identity import discover_user_identity
from playwright_gpt_core.models import TurnIdentity, TurnRecord
from tests.fixtures.graph_factory import graph, message


def test_transport_and_graph_turn_namespaces_may_differ() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root", text="prompt", turn="graph-turn", request="graph-request"),
        message("assistant-1", "assistant", "user-1", text="OK", turn="graph-turn", request="graph-request"),
        current="assistant-1",
    )
    result = discover_user_identity(
        snapshot,
        TurnIdentity(
            conversation_id="conv-1",
            transport_turn_exchange_id="transport-turn",
            stream_topic_id="conversation:transport-turn",
            user_message_id="user-1",
            frontend_parent_message_id="client-created-root",
        ),
        baseline_node_ids=set(),
    )
    assert result.transport_turn_exchange_id == "transport-turn"
    assert result.turn_exchange_id == "graph-turn"
    assert result.request_id == "graph-request"
    assert result.frontend_parent_message_id == "client-created-root"
    assert result.parent_message_id == "root"


def test_existing_conversation_pre_send_graph_anchor_is_enforced() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root", turn="graph-turn", request="graph-request"),
        current="user-1",
    )
    with pytest.raises(ConflictingIdentityError):
        discover_user_identity(
            snapshot,
            TurnIdentity(
                transport_turn_exchange_id="transport-turn",
                user_message_id="user-1",
                pre_send_current_node="different-parent",
            ),
            baseline_node_ids=set(),
        )


def test_schema_two_identity_migrates_transport_fields_without_guessing() -> None:
    original = TurnRecord.new(request_id="req-1", prompt="prompt").to_dict()
    original["schema_version"] = 2
    original["identity"] = {
        "conversation_id": "conv-1",
        "turn_exchange_id": "transport-turn",
        "request_id": "transport-request",
        "stream_topic_id": "topic-1",
        "user_message_id": "user-1",
        "parent_message_id": "client-created-root",
        "pre_send_current_node": None,
        "sources": {},
    }
    migrated = TurnRecord.from_dict(json.loads(json.dumps(original)))
    assert migrated.schema_version == 3
    assert migrated.identity is not None
    assert migrated.identity.transport_turn_exchange_id == "transport-turn"
    assert migrated.identity.transport_request_id == "transport-request"
    assert migrated.identity.turn_exchange_id is None
    assert migrated.identity.request_id is None
    assert migrated.identity.frontend_parent_message_id == "client-created-root"
    assert migrated.identity.parent_message_id is None
