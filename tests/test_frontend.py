from __future__ import annotations

import re

import pytest

from playwright_api.errors import FrontendNotReadyError
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
        "target_path_pattern": r"^/c/conversation\-1$",
        "attachment_names": {},
    }


class AtomicProjectRoutePage:
    def __init__(self, path: str) -> None:
        self.path = path
        self.script = ""
        self.payload = None

    async def evaluate(self, script: str, payload):
        self.script = script
        self.payload = payload
        exact = re.fullmatch(payload["target_path_pattern"], self.path) is not None
        return {"ok": True} if exact else {"ok": False, "reason": "page_identity"}


_PROJECT_ID = "g-p-0123456789abcdef0123456789abcdef"
_CONVERSATION_ID = "conversation-123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "path", "accepted"),
    [
        (
            ChatTarget.project(_PROJECT_ID),
            f"/g/{_PROJECT_ID}-sdk-project/project",
            True,
        ),
        (
            ChatTarget.project(_PROJECT_ID),
            f"/g/{_PROJECT_ID}-sdk-project/extra/project",
            False,
        ),
        (
            ChatTarget.project_conversation(_PROJECT_ID, _CONVERSATION_ID),
            f"/g/{_PROJECT_ID}-sdk-project/c/{_CONVERSATION_ID}",
            True,
        ),
        (
            ChatTarget.project_conversation(_PROJECT_ID, _CONVERSATION_ID),
            f"/g/{_PROJECT_ID}-sdk-project/extra/c/{_CONVERSATION_ID}",
            False,
        ),
    ],
)
async def test_atomic_send_enforces_exact_project_route_at_click_boundary(
    target: ChatTarget, path: str, accepted: bool
) -> None:
    page = AtomicProjectRoutePage(path)
    if accepted:
        await click_send_atomic(page, "prompt", target=target)  # type: ignore[arg-type]
        return
    with pytest.raises(FrontendNotReadyError, match="page_identity"):
        await click_send_atomic(page, "prompt", target=target)  # type: ignore[arg-type]
