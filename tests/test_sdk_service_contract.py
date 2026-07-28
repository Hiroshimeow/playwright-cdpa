from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from playwright_api import AttachmentInput, ChatGPTClient, ChatTarget, ClientConfig
from playwright_api.errors import InvalidInputError


def client(tmp_path: Path) -> ChatGPTClient:
    return ChatGPTClient(
        ClientConfig(
            state_dir=tmp_path / "state",
            coordination_dir=tmp_path / "coordination",
            deployment_id="sdk-service-contract",
        )
    )


def test_send_uses_one_canonical_target_parameter() -> None:
    signature = inspect.signature(ChatGPTClient.send)
    assert tuple(signature.parameters) == (
        "self",
        "prompt",
        "request_id",
        "target",
        "attachments",
    )
    assert "fresh" not in signature.parameters
    assert "conversation" not in signature.parameters


@pytest.mark.asyncio
async def test_changed_attachment_fails_before_state_or_browser(tmp_path: Path, monkeypatch) -> None:
    import playwright_api.service as service_module

    class ForbiddenBrowserSession:
        def __init__(self, _config) -> None:
            raise AssertionError("invalid attachment must fail before browser construction")

    monkeypatch.setattr(service_module, "BrowserSession", ForbiddenBrowserSession)
    path = tmp_path / "note.txt"
    path.write_text("one", encoding="utf-8")
    attachment = AttachmentInput.from_path(path)
    path.write_text("changed", encoding="utf-8")
    sdk = client(tmp_path)

    with pytest.raises(InvalidInputError, match="changed"):
        await sdk.send(
            "read it",
            request_id="changed-attachment",
            target=ChatTarget.fresh(),
            attachments=(attachment,),
        )

    assert not sdk.store.turn_path("changed-attachment").exists()


@pytest.mark.asyncio
async def test_duplicate_attachment_identity_fails_before_state(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("one", encoding="utf-8")
    attachment = AttachmentInput.from_path(path)
    sdk = client(tmp_path)

    with pytest.raises(InvalidInputError, match="duplicate"):
        await sdk.send(
            "read it",
            request_id="duplicate-attachment",
            target=ChatTarget.fresh(),
            attachments=(attachment, attachment),
        )

    assert not sdk.store.turn_path("duplicate-attachment").exists()
