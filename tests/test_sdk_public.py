from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest


def test_public_exports_and_authoritative_names() -> None:
    import playwright_api

    expected = {
        "AttachmentInput",
        "ChatGPTClient",
        "ChatTarget",
        "ClientConfig",
        "Disposition",
        "Failure",
        "FailureCategory",
        "FailureCode",
        "ProjectMemoryScope",
        "ProjectRef",
        "Result",
        "SyncChatGPTClient",
        "TargetKind",
        "TurnIdentity",
        "TurnState",
    }
    assert set(playwright_api.__all__) == expected
    assert inspect.iscoroutinefunction(playwright_api.ChatGPTClient.send)
    assert inspect.iscoroutinefunction(playwright_api.ChatGPTClient.get)
    assert inspect.iscoroutinefunction(playwright_api.ChatGPTClient.cancel)
    assert not hasattr(playwright_api.ChatGPTClient, "watch")


def test_default_state_location_is_per_user_not_cwd(tmp_path, monkeypatch) -> None:
    from playwright_api import ClientConfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    config = ClientConfig().validated()
    assert config.state_dir == (tmp_path / "xdg" / "playwright-api" / "requests").resolve()
    assert config.coordination_dir == (
        tmp_path / "xdg" / "playwright-api" / "coordination"
    ).resolve()


def test_sync_client_delegates_without_browser_logic(monkeypatch) -> None:
    from playwright_api import ClientConfig, Result, SyncChatGPTClient, TurnState

    calls: list[tuple[str, str]] = []

    class AsyncClient:
        def __init__(self, config: ClientConfig | None = None) -> None:
            self.config = config

        async def get(self, request_id: str) -> Result:
            calls.append(("get", request_id))
            return Result(1, request_id, TurnState.COMPLETE, response="ok")

    monkeypatch.setattr("playwright_api.sync.ChatGPTClient", AsyncClient)
    client = SyncChatGPTClient(ClientConfig(state_dir=Path("/tmp/sdk-sync-state")))
    result = client.get("request-1")
    assert result.response == "ok"
    assert calls == [("get", "request-1")]


@pytest.mark.asyncio
async def test_sync_client_fails_inside_running_event_loop() -> None:
    from playwright_api import SyncChatGPTClient

    with pytest.raises(RuntimeError, match="running event loop"):
        SyncChatGPTClient().status("request-1")
    await asyncio.sleep(0)
