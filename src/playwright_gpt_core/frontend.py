from __future__ import annotations

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .errors import AuthenticationRequiredError, FrontendNotReadyError

ORIGIN = "https://chatgpt.com"

_COMPOSER_SELECTORS = (
    'div[role="textbox"][contenteditable="true"]',
    '[contenteditable="true"][data-lexical-editor="true"]',
    'textarea[data-testid="prompt-textarea"]',
    '#prompt-textarea',
)
_SEND_SELECTORS = (
    'button[data-testid="send-button"]:visible',
    'button[aria-label="Send prompt"]:visible',
    'button[aria-label^="Send"]:visible',
)
_STOP_SELECTORS = (
    'button[data-testid="stop-button"]:visible',
    'button[aria-label="Stop generating"]:visible',
    'button[aria-label^="Stop"]:visible',
)


async def verify_authenticated(page: Page) -> None:
    try:
        result = await page.evaluate(
            """async () => {
              const response = await fetch('/api/auth/session', {credentials: 'include'});
              let body = null;
              try { body = await response.json(); } catch {}
              return {
                status: response.status,
                ok: response.ok,
                hasToken: Boolean(body?.accessToken ?? body?.access_token),
              };
            }"""
        )
    except Exception as exc:
        raise FrontendNotReadyError("could not verify ChatGPT frontend session") from exc
    if not isinstance(result, dict) or not result.get("ok") or not result.get("hasToken"):
        status = result.get("status") if isinstance(result, dict) else None
        raise AuthenticationRequiredError(
            f"ChatGPT browser session is unavailable (HTTP {status or 'unknown'})"
        )


async def find_composer(page: Page) -> Locator:
    return await _find_visible(page, _COMPOSER_SELECTORS, "composer", 5_000)


async def find_send_button(page: Page) -> Locator:
    return await _find_visible(page, _SEND_SELECTORS, "Send button", 3_000)


async def find_stop_button(page: Page) -> Locator | None:
    try:
        return await _find_visible(page, _STOP_SELECTORS, "Stop button", 1_000)
    except FrontendNotReadyError:
        return None


async def fill_composer(page: Page, prompt: str) -> None:
    composer = await find_composer(page)
    tag_name = str(await composer.evaluate("element => element.tagName")).upper()
    if tag_name == "TEXTAREA":
        await composer.fill(prompt)
    else:
        await composer.click()
        await composer.fill(prompt)
    observed = await composer.evaluate(
        "element => element.value ?? element.innerText ?? element.textContent ?? ''"
    )
    if str(observed).strip() != prompt.strip():
        raise FrontendNotReadyError("composer content could not be verified before Send")


async def clear_composer(page: Page) -> None:
    composer = await find_composer(page)
    await composer.fill("")
    observed = await composer.evaluate(
        "element => element.value ?? element.innerText ?? element.textContent ?? ''"
    )
    if str(observed).strip():
        raise FrontendNotReadyError("composer could not be cleared")


async def _find_visible(
    page: Page, selectors: tuple[str, ...], label: str, timeout_ms: int
) -> Locator:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except PlaywrightTimeoutError:
            continue
    raise FrontendNotReadyError(f"could not find visible ChatGPT {label}")
