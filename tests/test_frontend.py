from __future__ import annotations

import pytest

from playwright_api.frontend import _find_visible, click_send_atomic, observe_frontend
from playwright_api.targets import ChatTarget


class Locator:
    def __init__(self, succeeds: bool) -> None:
        self.succeeds = succeeds
        self.waited = False

    @property
    def first(self):
        return self

    async def wait_for(self, *, state: str, timeout: int) -> None:
        self.waited = True
        if not self.succeeds:
            from playwright.async_api import TimeoutError

            raise TimeoutError("not ready")


class Page:
    def __init__(self) -> None:
        self.locators = {"late": Locator(True)}

    def locator(self, selector: str):
        return self.locators[selector]


@pytest.mark.asyncio
async def test_find_visible_waits_for_late_hydration_without_count_gate() -> None:
    page = Page()
    locator = await _find_visible(page, ("late",), "composer", 5000)  # type: ignore[arg-type]
    assert locator.waited is True


class StatePage:
    url = "https://chatgpt.com/c/conversation-1"

    async def evaluate(self, _script: str):
        return {
            "composer_present": True,
            "composer_editable": True,
            "composer_text": "queued prompt",
            "attachment_count": 0,
            "send_visible": True,
            "send_enabled": True,
            "stop_visible": True,
            "choice_prompt": False,
        }


@pytest.mark.asyncio
async def test_observe_frontend_reports_steering_ready_state() -> None:
    state = await observe_frontend(StatePage())  # type: ignore[arg-type]
    assert state.url == "https://chatgpt.com/c/conversation-1"
    assert state.composer_text == "queued prompt"
    assert state.send_ready is True
    assert state.stop_visible is True
    assert state.attachment_count == 0


class AtomicScriptPage:
    def __init__(self) -> None:
        self.script = ""
        self.payload = None

    async def evaluate(self, script: str, payload):
        self.script = script
        self.payload = payload
        return {"ok": True}


@pytest.mark.asyncio
async def test_atomic_send_rejects_query_and_fragment_in_browser_callback() -> None:
    page = AtomicScriptPage()

    await click_send_atomic(
        page,  # type: ignore[arg-type]
        "prompt",
        target=ChatTarget.conversation("conversation-1"),
        expected_attachment_names={},
    )

    assert "current.search === ''" in page.script
    assert "current.hash === ''" in page.script
    assert page.payload == {
        "prompt": "prompt",
        "target_path": "/c/conversation-1",
        "target_kind": "conversation",
        "project_id": None,
        "conversation_id": "conversation-1",
        "attachment_names": {},
    }
