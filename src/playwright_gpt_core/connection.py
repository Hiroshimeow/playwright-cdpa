from __future__ import annotations

from types import TracebackType

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)

from .config import CoreConfig
from .errors import (
    BrowserOfflineError,
    ConflictingIdentityError,
    NetworkError,
    SchemaDriftError,
)


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


async def page_target_id(context: BrowserContext, page: Page) -> str:
    """Return the Chromium target ID for one exact page without exposing page content."""
    try:
        cdp = await context.new_cdp_session(page)
    except PlaywrightError as exc:
        raise NetworkError("could not attach CDP session to helper page") from exc
    try:
        value = await cdp.send("Target.getTargetInfo")
    except PlaywrightError as exc:
        raise NetworkError("could not identify helper page target") from exc
    finally:
        try:
            await cdp.detach()
        except PlaywrightError:
            pass
    target_info = value.get("targetInfo") if isinstance(value, dict) else None
    target_id = target_info.get("targetId") if isinstance(target_info, dict) else None
    if not isinstance(target_id, str) or not target_id or len(target_id) > 256:
        raise SchemaDriftError("CDP helper page has no valid target ID")
    return target_id


async def close_page_by_target_id(
    context: BrowserContext, target_id: str
) -> bool:
    """Close only the page with the exact durable Chromium target ID."""
    if not target_id or len(target_id) > 256:
        raise SchemaDriftError("invalid persisted helper target ID")
    matches: list[Page] = []
    for page in list(context.pages):
        if page.is_closed():
            continue
        current_target_id = await page_target_id(context, page)
        if current_target_id == target_id:
            matches.append(page)
    if len(matches) > 1:
        raise ConflictingIdentityError(
            "multiple browser pages matched one durable helper target ID"
        )
    if not matches:
        return False
    await matches[0].close()
    return True
