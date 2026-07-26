from __future__ import annotations

import pytest

from playwright_gpt_core.errors import GraphConvergenceTimeout
from playwright_gpt_core.models import TurnIdentity
from playwright_gpt_core.monitor import monitor_snapshots
from tests.fixtures.graph_factory import graph, message

IDENTITY = TurnIdentity(
    conversation_id="conv-1",
    turn_exchange_id="turn-1",
    request_id="req-1",
    stream_topic_id="topic-1",
    user_message_id="user-1",
    parent_message_id="root",
)


def snapshot(text: str, status: str = "finished_successfully"):
    return graph(
        message("root", "system", None, turn=None, request=None),
        message("user-1", "user", "root"),
        message("assistant-1", "assistant", "user-1", text=text, status=status),
        current="assistant-1",
    )


@pytest.mark.asyncio
async def test_delayed_graph_convergence_requires_stable_samples() -> None:
    result = await monitor_snapshots(
        IDENTITY,
        [
            ("RUNNING", snapshot("partial", "in_progress")),
            ("COMPLETE", snapshot("almost")),
            ("COMPLETE", snapshot("final")),
            ("COMPLETE", snapshot("final")),
        ],
        stable_samples=2,
    )
    assert result.text == "final"


@pytest.mark.asyncio
async def test_complete_without_convergence_fails() -> None:
    with pytest.raises(GraphConvergenceTimeout):
        await monitor_snapshots(
            IDENTITY,
            [
                ("COMPLETE", snapshot("one")),
                ("COMPLETE", snapshot("two")),
            ],
            stable_samples=2,
        )


@pytest.mark.asyncio
async def test_terminal_backend_failure_is_distinct_from_convergence() -> None:
    from playwright_gpt_core.errors import BackendError

    with pytest.raises(BackendError):
        await monitor_snapshots(
            IDENTITY,
            [("FAILED", snapshot("not-a-success"))],
            stable_samples=2,
        )


@pytest.mark.asyncio
async def test_idle_status_can_converge_an_exact_final_graph() -> None:
    result = await monitor_snapshots(
        IDENTITY,
        [("IDLE", snapshot("idle-final")), ("IDLE", snapshot("idle-final"))],
        stable_samples=2,
    )
    assert result.text == "idle-final"


@pytest.mark.asyncio
async def test_not_found_without_exact_graph_is_convergence_failure() -> None:
    with pytest.raises(GraphConvergenceTimeout):
        await monitor_snapshots(
            IDENTITY,
            [
                (
                    "NOT_FOUND",
                    graph(
                        message("root", "system", None, turn=None, request=None), current="root"
                    ),
                )
            ],
            stable_samples=2,
        )


@pytest.mark.asyncio
async def test_terminal_samples_must_be_consecutive_after_running_status() -> None:
    with pytest.raises(GraphConvergenceTimeout):
        await monitor_snapshots(
            IDENTITY,
            [
                ("COMPLETE", snapshot("final")),
                ("RUNNING", snapshot("final")),
                ("COMPLETE", snapshot("final")),
            ],
            stable_samples=2,
        )


@pytest.mark.asyncio
async def test_terminal_samples_reset_when_exact_candidate_disappears() -> None:
    missing = graph(
        message("root", "system", None, turn=None, request=None),
        current="root",
    )
    with pytest.raises(GraphConvergenceTimeout):
        await monitor_snapshots(
            IDENTITY,
            [
                ("COMPLETE", snapshot("final")),
                ("COMPLETE", missing),
                ("COMPLETE", snapshot("final")),
            ],
            stable_samples=2,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_text", "second_text"),
    [
        ("proof_token=FIRST-SECRET", "proof_token=SECOND-SECRET"),
        ("Authorization: Basic FIRST-SECRET", "Authorization: Basic SECOND-SECRET"),
        (
            "GET https://example.test/api/token/FIRST-SECRET failed",
            "GET https://example.test/api/token/SECOND-SECRET failed",
        ),
    ],
)
async def test_redaction_equivalent_raw_responses_require_fresh_stable_samples(
    first_text: str, second_text: str
) -> None:
    with pytest.raises(GraphConvergenceTimeout):
        await monitor_snapshots(
            IDENTITY,
            [
                ("COMPLETE", snapshot(first_text)),
                ("COMPLETE", snapshot(second_text)),
            ],
            stable_samples=2,
        )

    result = await monitor_snapshots(
        IDENTITY,
        [
            ("COMPLETE", snapshot(first_text)),
            ("COMPLETE", snapshot(second_text)),
            ("COMPLETE", snapshot(second_text)),
        ],
        stable_samples=2,
    )
    assert result.text == second_text
