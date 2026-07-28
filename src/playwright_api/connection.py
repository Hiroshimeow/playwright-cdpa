from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .config import ClientConfig
from .errors import (
    BrowserOfflineError,
    ConflictingIdentityError,
    InvalidInputError,
    NetworkError,
    OperationTimeoutError,
    SchemaDriftError,
)
from .targets import conversation_url, normalize_conversation


class BrowserSession:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config.validated()
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserSession:  # noqa: PYI034
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


@dataclass(frozen=True, slots=True)
class ConversationPage:
    page: Page
    target_id: str
    owned: bool


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


async def resolve_conversation_page(
    context: BrowserContext,
    conversation_id: str,
    *,
    preferred_target_id: str | None = None,
) -> ConversationPage:
    """Borrow one exact conversation page or open one exact owned helper."""
    exact: list[tuple[Page, str]] = []
    preferred: tuple[Page, str] | None = None
    for page in list(context.pages):
        if page.is_closed():
            continue
        target_id = await page_target_id(context, page)
        try:
            page_conversation_id = normalize_conversation(page.url)
        except InvalidInputError:
            page_conversation_id = None
        if preferred_target_id is not None and target_id == preferred_target_id:
            preferred = (page, target_id)
            if page_conversation_id != conversation_id:
                raise ConflictingIdentityError(
                    "persisted helper target is not the exact conversation page"
                )
        if page_conversation_id == conversation_id:
            exact.append((page, target_id))

    if len(exact) > 1:
        raise ConflictingIdentityError("multiple browser pages matched the exact conversation")
    if preferred is not None:
        return ConversationPage(preferred[0], preferred[1], True)
    if exact:
        page, target_id = exact[0]
        return ConversationPage(page, target_id, False)

    try:
        page = await context.new_page()
    except PlaywrightTimeoutError as exc:
        raise OperationTimeoutError("creating an exact conversation page timed out") from exc
    except PlaywrightError as exc:
        raise NetworkError("could not create an exact conversation page") from exc
    try:
        target_id = await page_target_id(context, page)
        try:
            await page.goto(
                conversation_url(conversation_id),
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except PlaywrightTimeoutError as exc:
            raise OperationTimeoutError("exact conversation page navigation timed out") from exc
        except PlaywrightError as exc:
            raise NetworkError("could not navigate to the exact conversation page") from exc
        try:
            resolved_id = normalize_conversation(page.url)
        except InvalidInputError as exc:
            raise ConflictingIdentityError(
                "new helper did not remain on an exact conversation URL"
            ) from exc
        if resolved_id != conversation_id:
            raise ConflictingIdentityError("new helper resolved to a different conversation")
        return ConversationPage(page, target_id, True)
    except Exception:
        try:
            await page.close()
        except PlaywrightError:
            pass
        raise


async def close_page_by_target_id(context: BrowserContext, target_id: str) -> bool:
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
    try:
        await matches[0].close()
    except PlaywrightTimeoutError as exc:
        raise OperationTimeoutError("closing the exact helper page timed out") from exc
    except PlaywrightError as exc:
        raise NetworkError("could not close the exact helper page") from exc
    return True
