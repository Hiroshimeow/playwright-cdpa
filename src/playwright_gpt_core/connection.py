from __future__ import annotations

from types import TracebackType

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from .config import CoreConfig
from .errors import BrowserOfflineError


class BrowserSession:
    def __init__(self, config: CoreConfig) -> None:
        self.config = config.validated()
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserSession:
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(
                self.config.cdp_endpoint
            )
        except Exception as exc:
            await self.playwright.stop()
            self.playwright = None
            raise BrowserOfflineError(
                f"could not connect to CDP endpoint {self.config.cdp_endpoint}"
            ) from exc
        if not self.browser.contexts:
            await self.playwright.stop()
            self.playwright = None
            raise BrowserOfflineError("CDP browser has no persistent context")
        self.context = self.browser.contexts[0]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Detach Playwright only. Never call browser.close() on persistent Chromium.
        if self.playwright is not None:
            await self.playwright.stop()
        self.playwright = None
        self.browser = None
        self.context = None
