from __future__ import annotations

import re

import pytest

from playwright_api.errors import FrontendNotReadyError
from playwright_api.frontend import (
    _find_visible,
    click_send_atomic,
    fill_composer,
    observe_frontend,
    verify_composer,
)
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


_LIVE_PROMPT_LINES = (
    "# Deployment constructor",
    "",
    "```json",
    "{",
    '  "title": "multiline",',
    '  "steps": [',
    '    "verify",',
    '    "send"',
    "  ]",
    "}",
    "```",
    "",
    "- preserve blank lines",
    "  - preserve indentation",
)
_LIVE_PROMPT = "\n".join(_LIVE_PROMPT_LINES)


def _dom_lines(text: str) -> tuple[str, ...]:
    lines = []
    for line in text.split("\n"):
        indent = len(line) - len(line.lstrip(" "))
        lines.append("\u00a0" * indent + line[indent:])
    return tuple(lines)


def _uses_logical_reader(script: str) -> bool:
    return "childNodes" in script and "00a0" in script


class ComposerLocator:
    def __init__(self, text: str = "", *, tag_name: str = "DIV") -> None:
        self.tag_name = tag_name
        self.scripts: list[str] = []
        self.clicked = False
        self._set_text(text)

    @property
    def first(self):
        return self

    @property
    def logical_text(self) -> str:
        if self.tag_name == "TEXTAREA":
            return self.value
        return "\n".join(line.replace("\u00a0", " ") for line in self.lines)

    @property
    def raw_text(self) -> str:
        if self.tag_name == "TEXTAREA":
            return self.value
        return "\n\n".join(self.lines)

    def _set_text(self, text: str) -> None:
        self.value = text
        self.lines = _dom_lines(text)

    async def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "visible"
        assert timeout > 0

    async def evaluate(self, script: str):
        self.scripts.append(script)
        if script == "element => element.tagName":
            return self.tag_name
        if self.tag_name == "TEXTAREA" or _uses_logical_reader(script):
            return self.logical_text
        return self.raw_text

    async def click(self) -> None:
        self.clicked = True

    async def fill(self, text: str) -> None:
        self._set_text(text)


class ComposerPage:
    def __init__(self, text: str = "", *, tag_name: str = "DIV") -> None:
        self.composer = ComposerLocator(text, tag_name=tag_name)

    def locator(self, _selector: str):
        return self.composer


class LiveStatePage:
    url = "https://chatgpt.com/c/conversation-1"

    def __init__(self, text: str) -> None:
        self.composer = ComposerLocator(text)
        self.script = ""

    async def evaluate(self, script: str):
        self.script = script
        text = (
            self.composer.logical_text
            if _uses_logical_reader(script)
            else self.composer.raw_text
        )
        return {
            "composer_present": True,
            "composer_editable": True,
            "composer_text": text,
            "attachment_count": 0,
            "send_visible": True,
            "send_enabled": True,
            "stop_visible": False,
            "choice_prompt": False,
        }


class LiveAtomicPage:
    def __init__(
        self,
        text: str,
        *,
        tag_name: str = "DIV",
        path: str = "/c/conversation-1",
        composer_count: int = 1,
        disabled: bool = False,
        attachments: tuple[str, ...] = (),
    ) -> None:
        self.composer = ComposerLocator(text, tag_name=tag_name)
        self.path = path
        self.composer_count = composer_count
        self.disabled = disabled
        self.attachments = attachments
        self.send_clicks = 0
        self.script = ""

    async def evaluate(self, script: str, payload):
        self.script = script
        if re.fullmatch(payload["target_path_pattern"], self.path) is None:
            return {"ok": False, "reason": "page_identity"}
        if self.composer_count != 1:
            return {"ok": False, "reason": "composer_identity"}
        observed = (
            self.composer.logical_text
            if _uses_logical_reader(script)
            else self.composer.raw_text
        )
        expected = payload["prompt"]
        matches = observed == expected if _uses_logical_reader(script) else (
            observed.strip() == expected.strip()
        )
        if not matches:
            return {"ok": False, "reason": "composer_changed"}
        if self.disabled:
            return {"ok": False, "reason": "composer_disabled"}
        observed_names: dict[str, int] = {}
        for name in self.attachments:
            observed_names[name] = observed_names.get(name, 0) + 1
        if observed_names != payload["attachment_names"]:
            return {"ok": False, "reason": "attachments_changed"}
        self.send_clicks += 1
        return {"ok": True}


@pytest.mark.asyncio
async def test_fill_composer_accepts_live_shaped_contenteditable_normalization() -> None:
    page = ComposerPage()

    await fill_composer(page, _LIVE_PROMPT)  # type: ignore[arg-type]

    assert page.composer.logical_text == _LIVE_PROMPT
    assert _uses_logical_reader(page.composer.scripts[-1])


@pytest.mark.asyncio
async def test_observe_frontend_returns_logical_contenteditable_text() -> None:
    page = LiveStatePage(_LIVE_PROMPT)

    state = await observe_frontend(page)  # type: ignore[arg-type]

    assert state.composer_text == _LIVE_PROMPT
    assert _uses_logical_reader(page.script)


_CONTENT_DRIFT_CASES = (
    _LIVE_PROMPT.replace('"verify"', '"verified"', 1),
    _LIVE_PROMPT.replace("\n- preserve blank lines", "", 1),
    _LIVE_PROMPT.replace("\n- preserve blank lines", "\n- unexpected\n- preserve blank lines", 1),
    _LIVE_PROMPT.replace('  "title"', '   "title"', 1),
    _LIVE_PROMPT.replace("```\n\n- preserve", "```\n\n\n- preserve", 1),
    _LIVE_PROMPT.replace("```\n\n- preserve", "```\n- preserve", 1),
    _LIVE_PROMPT.replace(
        '    "verify",\n    "send"',
        '    "send",\n    "verify"',
        1,
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", _CONTENT_DRIFT_CASES)
async def test_verify_composer_rejects_substantive_contenteditable_drift(actual: str) -> None:
    page = ComposerPage(actual)

    with pytest.raises(FrontendNotReadyError, match="could not be verified"):
        await verify_composer(page, _LIVE_PROMPT)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_verify_composer_keeps_textarea_whitespace_exact() -> None:
    page = ComposerPage(f" {_LIVE_PROMPT} ", tag_name="TEXTAREA")

    with pytest.raises(FrontendNotReadyError, match="could not be verified"):
        await verify_composer(page, _LIVE_PROMPT)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_atomic_send_reaches_attachment_boundary_after_logical_text_match() -> None:
    page = LiveAtomicPage(_LIVE_PROMPT)

    with pytest.raises(FrontendNotReadyError, match="attachments_changed"):
        await click_send_atomic(
            page,  # type: ignore[arg-type]
            _LIVE_PROMPT,
            target=ChatTarget.conversation("conversation-1"),
            expected_attachment_names={"missing.txt": 1},
        )

    assert page.send_clicks == 0
    assert _uses_logical_reader(page.script)


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", _CONTENT_DRIFT_CASES)
async def test_atomic_send_rejects_contenteditable_drift_without_click(actual: str) -> None:
    page = LiveAtomicPage(actual)

    with pytest.raises(FrontendNotReadyError, match="composer_changed"):
        await click_send_atomic(
            page,  # type: ignore[arg-type]
            _LIVE_PROMPT,
            target=ChatTarget.conversation("conversation-1"),
        )

    assert page.send_clicks == 0


@pytest.mark.asyncio
async def test_atomic_send_keeps_textarea_whitespace_exact_without_click() -> None:
    page = LiveAtomicPage(f" {_LIVE_PROMPT} ", tag_name="TEXTAREA")

    with pytest.raises(FrontendNotReadyError, match="composer_changed"):
        await click_send_atomic(
            page,  # type: ignore[arg-type]
            _LIVE_PROMPT,
            target=ChatTarget.conversation("conversation-1"),
        )

    assert page.send_clicks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page", "reason"),
    [
        (LiveAtomicPage(_LIVE_PROMPT, disabled=True), "composer_disabled"),
        (
            LiveAtomicPage(_LIVE_PROMPT, attachments=("unexpected.txt",)),
            "attachments_changed",
        ),
        (LiveAtomicPage(_LIVE_PROMPT, composer_count=2), "composer_identity"),
        (LiveAtomicPage(_LIVE_PROMPT, path="/c/foreign"), "page_identity"),
    ],
)
async def test_atomic_send_preserves_non_text_preflight_failures_without_click(
    page: LiveAtomicPage, reason: str
) -> None:
    with pytest.raises(FrontendNotReadyError, match=reason):
        await click_send_atomic(
            page,  # type: ignore[arg-type]
            _LIVE_PROMPT,
            target=ChatTarget.conversation("conversation-1"),
        )

    assert page.send_clicks == 0
