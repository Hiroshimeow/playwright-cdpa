from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


PROTOTYPE_PATH = (
    Path(__file__).resolve().parents[1] / "reference" / "ssgpt_v5_prototype.py"
)


def load_prototype() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ssgpt_v5_prototype", PROTOTYPE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pytestmark = pytest.mark.xfail(
    strict=True,
    reason="immutable prototype characterization; production regressions must pass separately",
)


@pytest.fixture(scope="module")
def prototype() -> ModuleType:
    return load_prototype()


def node(
    message_id: str,
    *,
    role: str,
    parent: str | None,
    text: str,
    turn_exchange_id: str | None = "turn-1",
    create_time: float = 1.0,
    status: str = "finished_successfully",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if turn_exchange_id is not None:
        metadata["turn_exchange_id"] = turn_exchange_id
    return {
        "id": message_id,
        "parent": parent,
        "children": [],
        "message": {
            "id": message_id,
            "author": {"role": role},
            "recipient": "all",
            "create_time": create_time,
            "status": status,
            "content": {"content_type": "text", "parts": [text]},
            "metadata": metadata,
        },
    }


def conversation(*entries: tuple[str, dict[str, Any]], current: str) -> dict[str, Any]:
    mapping = dict(entries)
    for message_id, item in mapping.items():
        parent = item.get("parent")
        if parent in mapping:
            mapping[parent]["children"].append(message_id)
    return {"mapping": mapping, "current_node": current}


def test_mutable_same_message_id_must_be_reprocessed(prototype: ModuleType) -> None:
    mutable = node(
        "assistant-1",
        role="assistant",
        parent="user-1",
        text="partial",
        status="in_progress",
    )
    snapshot = conversation(("assistant-1", mutable), current="assistant-1")
    seen: set[str] = set()

    first = prototype.log_new_messages(
        snapshot,
        seen,
        turn_exchange_id="turn-1",
        compact=True,
    )
    assert first == "partial"

    mutable["message"]["content"]["parts"] = ["final"]
    mutable["message"]["status"] = "finished_successfully"
    second = prototype.log_new_messages(
        snapshot,
        seen,
        turn_exchange_id="turn-1",
        compact=True,
    )

    assert second == "final"


def test_missing_message_turn_identity_must_not_match_exact_turn(
    prototype: ModuleType,
) -> None:
    assert prototype.belongs_to_turn({}, "turn-1") is False


def test_final_candidate_must_be_restricted_to_current_branch(
    prototype: ModuleType,
) -> None:
    root = node(
        "root",
        role="system",
        parent=None,
        text="",
        turn_exchange_id=None,
        create_time=0,
    )
    user = node(
        "user-1",
        role="user",
        parent="root",
        text="prompt",
        create_time=1,
    )
    current = node(
        "assistant-current",
        role="assistant",
        parent="user-1",
        text="CURRENT",
        create_time=2,
    )
    stale_sibling = node(
        "assistant-stale",
        role="assistant",
        parent="user-1",
        text="STALE",
        create_time=99,
    )
    snapshot = conversation(
        ("root", root),
        ("user-1", user),
        ("assistant-current", current),
        ("assistant-stale", stale_sibling),
        current="assistant-current",
    )

    result = prototype.log_new_messages(
        snapshot,
        set(),
        turn_exchange_id="turn-1",
        compact=True,
    )

    assert result == "CURRENT"


@pytest.mark.asyncio
async def test_complete_requires_bounded_graph_convergence(
    prototype: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.graph_calls = 0

        async def get(self, path: str) -> tuple[int, str]:
            if path.endswith("/stream_status"):
                return 200, json.dumps({"status": "COMPLETE"})

            self.graph_calls += 1
            if self.graph_calls == 1:
                graph = conversation(
                    (
                        "user-1",
                        node(
                            "user-1",
                            role="user",
                            parent=None,
                            text="prompt",
                        ),
                    ),
                    current="user-1",
                )
            elif self.graph_calls == 2:
                graph = conversation(
                    (
                        "assistant-unstable",
                        node(
                            "assistant-unstable",
                            role="assistant",
                            parent="user-1",
                            text="UNSTABLE",
                        ),
                    ),
                    current="assistant-unstable",
                )
            else:
                graph = conversation(
                    (
                        "assistant-stable",
                        node(
                            "assistant-stable",
                            role="assistant",
                            parent="user-1",
                            text="STABLE",
                        ),
                    ),
                    current="assistant-stable",
                )
            return 200, json.dumps(graph)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(prototype.asyncio, "sleep", no_sleep)
    backend = FakeBackend()
    result = await prototype.monitor_conversation(
        backend,
        "conversation-1",
        turn_exchange_id="turn-1",
        baseline_message_ids=set(),
        timeout_seconds=2,
        poll_seconds=0.01,
        compact=True,
    )

    assert backend.graph_calls >= 3
    assert result == "STABLE"


def test_resume_token_is_never_logged(
    prototype: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prototype.log_json(
        "EVENT",
        {"resume_conversation_token": "resume-secret-value"},
        compact=False,
    )
    output = capsys.readouterr().out
    assert "resume-secret-value" not in output
    assert "<redacted>" in output
