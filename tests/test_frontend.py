from __future__ import annotations

import pytest

from playwright_gpt_core.frontend import _find_visible


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
