from __future__ import annotations

import pytest

from playwright_gpt_core.errors import AmbiguousIdentityError, ConflictingIdentityError
from playwright_gpt_core.identity import discover_user_identity, merge_identity
from playwright_gpt_core.models import TurnIdentity
from tests.fixtures.graph_factory import graph, message


def test_staged_identity_merge_accepts_nonconflicting_fields() -> None:
    first = TurnIdentity(request_id="req-1", pre_send_current_node="root")
    merged = merge_identity(
        first,
        TurnIdentity(conversation_id="conv-1", turn_exchange_id="turn-1"),
        source="frontend",
    )
    assert merged.request_id == "req-1"
    assert merged.conversation_id == "conv-1"
    assert merged.sources["conversation_id"] == "frontend"


def test_staged_identity_merge_rejects_fieldwise_turn_mixing() -> None:
    with pytest.raises(ConflictingIdentityError):
        merge_identity(
            TurnIdentity(turn_exchange_id="turn-a"),
            TurnIdentity(turn_exchange_id="turn-b"),
            source="graph",
        )


def test_exact_user_node_is_discovered_from_current_branch_and_baseline() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root", text="prompt"),
        message("assistant-1", "assistant", "user-1", text="partial", status="in_progress"),
        current="assistant-1",
    )
    identity = discover_user_identity(
        snapshot,
        TurnIdentity(
            turn_exchange_id="turn-1",
            request_id="req-1",
            pre_send_current_node="root",
        ),
        baseline_node_ids={"root"},
        prompt="prompt",
    )
    assert identity.user_message_id == "user-1"
    assert identity.parent_message_id == "root"


def test_ambiguous_user_candidates_fail_closed() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-a", "user", "root", text="prompt"),
        message("user-b", "user", "user-a", text="prompt"),
        current="user-b",
    )
    with pytest.raises(AmbiguousIdentityError):
        discover_user_identity(
            snapshot,
            TurnIdentity(turn_exchange_id="turn-1", request_id="req-1"),
            baseline_node_ids={"root"},
            prompt="prompt",
        )
