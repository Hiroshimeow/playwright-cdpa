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
