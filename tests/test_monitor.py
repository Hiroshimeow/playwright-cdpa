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
