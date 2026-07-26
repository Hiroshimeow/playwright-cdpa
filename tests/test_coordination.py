from __future__ import annotations

import json
import stat

import pytest

from playwright_gpt_core.config import CoreConfig
from playwright_gpt_core.errors import OwnershipConflictError
from playwright_gpt_core.service import ChatGPTCore


def test_different_result_stores_share_one_deployment_claim(tmp_path) -> None:
    coordination = tmp_path / "coordination"
    first = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "repo-a",
            coordination_dir=coordination,
            deployment_id="shared-browser",
        )
    )
    second = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "repo-b",
            coordination_dir=coordination,
            deployment_id="shared-browser",
        )
    )

    first.coordination.claim("conversation-1", "request-a")

    with pytest.raises(OwnershipConflictError):
        second.coordination.claim("conversation-1", "request-b")
    assert second.coordination.load("conversation-1").active_request_id == "request-a"
    assert not second.store.turn_path("request-a").exists()


def test_deployment_namespaces_isolate_explicit_browser_profiles(tmp_path) -> None:
    first = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "repo-a",
            coordination_dir=tmp_path / "coordination",
            deployment_id="profile-a",
        )
    )
    second = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "repo-b",
            coordination_dir=tmp_path / "coordination",
            deployment_id="profile-b",
        )
    )

    first.coordination.claim("conversation-1", "request-a")
    second.coordination.claim("conversation-1", "request-b")

    assert first.coordination.root != second.coordination.root


def test_coordination_state_is_private_and_contains_no_result_store_path(tmp_path) -> None:
    state_dir = tmp_path / "private-repository-state"
    core = ChatGPTCore(
        CoreConfig(
            state_dir=state_dir,
            coordination_dir=tmp_path / "coordination",
            deployment_id="shared-browser",
        )
    )

    core.coordination.claim("conversation-1", "request-a")
    files = list((core.coordination.root / "conversations").glob("*.json"))

    assert len(files) == 1
    assert stat.S_IMODE(core.coordination.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert str(state_dir) not in files[0].read_text(encoding="utf-8")
    assert set(payload) == {
        "active_request_id",
        "conversation_id",
        "last_terminal_request_id",
        "revision",
        "schema_version",
        "updated_at",
    }


@pytest.mark.asyncio
async def test_foreign_claim_fails_without_loading_foreign_turn_state(tmp_path) -> None:
    coordination = tmp_path / "coordination"
    owner = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "repo-a",
            coordination_dir=coordination,
            deployment_id="shared-browser",
        )
    )
    contender = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "repo-b",
            coordination_dir=coordination,
            deployment_id="shared-browser",
        )
    )
    owner.coordination.claim("conversation-1", "foreign-request")

    with pytest.raises(OwnershipConflictError, match="foreign-request"):
        await contender.send("next", conversation="conversation-1")

    assert not contender.store.turn_path("foreign-request").exists()


@pytest.mark.asyncio
async def test_wait_idle_polls_shared_claim_without_foreign_state_lookup(tmp_path) -> None:
    coordination = tmp_path / "coordination"
    owner = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "repo-a",
            coordination_dir=coordination,
            deployment_id="shared-browser",
        )
    )
    contender = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "repo-b",
            coordination_dir=coordination,
            deployment_id="shared-browser",
            timeout=0.01,
            poll=0.001,
        )
    )
    owner.coordination.claim("conversation-1", "foreign-request")

    with pytest.raises(OwnershipConflictError, match="remains owned"):
        await contender.send(
            "next",
            conversation="conversation-1",
            wait_idle=True,
        )

    assert not contender.store.turn_path("foreign-request").exists()


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("schema_version", 1.0),
        ("active_request_id", False),
        ("revision", "1"),
        ("revision", True),
        ("revision", -1),
        ("updated_at", "2026-07-26T00:00:00"),
        ("last_terminal_request_id", 123),
    ],
)
def test_coordination_record_rejects_coerced_scalars(tmp_path, field, malformed) -> None:
    from playwright_gpt_core.errors import CorruptStateError

    core = ChatGPTCore(
        CoreConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="strict-coordination",
        )
    )
    core.coordination.claim("conversation-strict", "request-a")
    path = core.coordination._store.conversation_path("conversation-strict")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = malformed
    raw = json.dumps(payload, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(CorruptStateError):
        core.coordination.load("conversation-strict")

    assert path.read_text(encoding="utf-8") == raw
