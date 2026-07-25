from __future__ import annotations

import pytest

from playwright_gpt_core.errors import ConflictingIdentityError, IdentityMissingError
from playwright_gpt_core.graph import GraphResolver, fingerprint_node
from playwright_gpt_core.models import TurnIdentity
from tests.fixtures.graph_factory import graph, message


def identity(**changes: str | None) -> TurnIdentity:
    values = {
        "conversation_id": "conv-1",
        "turn_exchange_id": "turn-1",
        "request_id": "req-1",
        "stream_topic_id": "topic-1",
        "user_message_id": "user-1",
        "parent_message_id": "root",
        "pre_send_current_node": "root",
    }
    values.update(changes)
    return TurnIdentity(**values)


def test_exact_current_branch_final_is_resolved() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root", text="prompt"),
        message("assistant-1", "assistant", "user-1", text="OK"),
        current="assistant-1",
    )
    candidate = GraphResolver(identity()).resolve(snapshot)
    assert candidate.text == "OK"
    assert candidate.message_id == "assistant-1"


def test_stale_history_and_regenerated_sibling_are_rejected() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("old-user", "user", "root", turn="turn-old", request="req-old"),
        message(
            "old-final",
            "assistant",
            "old-user",
            text="STALE",
            turn="turn-old",
            request="req-old",
        ),
        message("user-1", "user", "old-final", text="prompt"),
        message(
            "sibling",
            "assistant",
            "user-1",
            text="WRONG",
            turn="turn-other",
            request="req-other",
        ),
        message("assistant-1", "assistant", "user-1", text="RIGHT"),
        current="assistant-1",
    )
    candidate = GraphResolver(
        identity(parent_message_id="old-final", pre_send_current_node="old-final")
    ).resolve(snapshot)
    assert candidate.text == "RIGHT"


def test_missing_turn_identity_never_wildcards() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root", turn=None, request=None),
        message(
            "assistant-1",
            "assistant",
            "user-1",
            text="UNPROVEN",
            turn=None,
            request=None,
        ),
        current="assistant-1",
    )
    with pytest.raises(IdentityMissingError):
        GraphResolver(
            identity(turn_exchange_id=None, request_id=None, stream_topic_id=None)
        ).resolve(snapshot)


def test_nearest_terminal_assistant_on_exact_chain_wins() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-a", "assistant", "user-1", text="EARLY"),
        message("assistant-b", "assistant", "assistant-a", text="FINAL"),
        current="assistant-b",
    )
    assert GraphResolver(identity()).resolve(snapshot).text == "FINAL"


def test_present_conflicting_descendant_identity_fails_closed() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message(
            "assistant-1",
            "assistant",
            "user-1",
            text="WRONG",
            turn="turn-other",
        ),
        current="assistant-1",
    )
    with pytest.raises(ConflictingIdentityError):
        GraphResolver(identity()).resolve(snapshot)


def test_same_message_id_mutation_changes_fingerprint() -> None:
    first = message(
        "assistant-1",
        "assistant",
        "user-1",
        text="partial",
        status="in_progress",
    )[1]
    second = message(
        "assistant-1",
        "assistant",
        "user-1",
        text="final",
        status="finished_successfully",
    )[1]
    assert fingerprint_node(first) != fingerprint_node(second)


def test_active_exact_branch_can_be_validated_without_terminal_final() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message(
            "tool-call",
            "assistant",
            "user-1",
            text="{}",
            recipient="tool.backend",
            status="in_progress",
            content_type="code",
        ),
        current="tool-call",
    )
    _mapping, exact_nodes = GraphResolver(identity()).validate_exact_branch(snapshot)
    assert exact_nodes == ["user-1", "tool-call"]


def test_terminal_tool_call_result_chain_allows_final_resolution() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message(
            "tool-call",
            "assistant",
            "user-1",
            text="{}",
            recipient="tool.backend",
            status="finished_successfully",
            content_type="code",
        ),
        message(
            "tool-result",
            "tool",
            "tool-call",
            text="result",
            status="finished_successfully",
            content_type="text",
        ),
        message("assistant-final", "assistant", "tool-result", text="TOOL_OK"),
        current="assistant-final",
    )
    assert GraphResolver(identity()).resolve(snapshot).text == "TOOL_OK"


def test_tool_call_without_result_blocks_final_candidate() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message(
            "tool-call",
            "assistant",
            "user-1",
            text="{}",
            recipient="tool.backend",
            status="finished_successfully",
            content_type="code",
        ),
        message("assistant-final", "assistant", "tool-call", text="UNPROVEN"),
        current="assistant-final",
    )
    with pytest.raises(IdentityMissingError):
        GraphResolver(identity()).resolve(snapshot)


def test_tool_parented_internal_user_subturn_may_change_graph_ids() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root", turn="turn-1", request="req-1"),
        message(
            "tool-call-1",
            "assistant",
            "user-1",
            text="{}",
            turn="turn-1",
            request="req-1",
            recipient="tool.backend",
            status="finished_successfully",
            content_type="code",
        ),
        message(
            "tool-result-1",
            "tool",
            "tool-call-1",
            text="first result",
            turn="turn-1",
            request="req-1",
            status="finished_successfully",
        ),
        message(
            "internal-user",
            "user",
            "tool-result-1",
            text="internal continuation",
            turn="turn-2",
            request="req-2",
        ),
        message(
            "tool-call-2",
            "assistant",
            "internal-user",
            text="{}",
            turn="turn-2",
            request="req-2",
            recipient="tool.backend",
            status="finished_successfully",
            content_type="code",
        ),
        message(
            "tool-result-2",
            "tool",
            "tool-call-2",
            text="second result",
            turn="turn-2",
            request="req-2",
            status="finished_successfully",
        ),
        message(
            "assistant-final",
            "assistant",
            "tool-result-2",
            text="TOOL_SUBTURN_OK",
            turn="turn-2",
            request="req-2",
        ),
        current="assistant-final",
    )
    assert GraphResolver(identity()).resolve(snapshot).text == "TOOL_SUBTURN_OK"


def test_later_human_user_turn_below_assistant_is_rejected() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root", turn="turn-1", request="req-1"),
        message("assistant-1", "assistant", "user-1", text="FIRST"),
        message(
            "later-user",
            "user",
            "assistant-1",
            text="new human turn",
            turn="turn-2",
            request="req-2",
        ),
        message(
            "later-final",
            "assistant",
            "later-user",
            text="WRONG TURN",
            turn="turn-2",
            request="req-2",
        ),
        current="later-final",
    )
    with pytest.raises(ConflictingIdentityError):
        GraphResolver(identity()).resolve(snapshot)


def test_conflicting_identity_inside_one_graph_segment_is_rejected() -> None:
    snapshot = graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root", turn="turn-1", request="req-1"),
        message(
            "assistant-1",
            "assistant",
            "user-1",
            text="WRONG",
            turn="turn-other",
            request="req-1",
        ),
        current="assistant-1",
    )
    with pytest.raises(ConflictingIdentityError):
        GraphResolver(identity()).resolve(snapshot)
