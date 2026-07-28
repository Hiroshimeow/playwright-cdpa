from __future__ import annotations

import time
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .errors import (
    AuthenticationRequiredError,
    CancellationUnprovenError,
    FrontendNotReadyError,
    NetworkError,
    OperationTimeoutError,
)

ORIGIN = "https://chatgpt.com"

_COMPOSER_SELECTORS = (
    'div[role="textbox"][contenteditable="true"]',
    '[contenteditable="true"][data-lexical-editor="true"]',
    'textarea[data-testid="prompt-textarea"]',
    "#prompt-textarea",
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


@dataclass(frozen=True, slots=True)
class FrontendState:
    url: str
    composer_present: bool
    composer_editable: bool
    composer_text: str
    attachment_count: int
    send_visible: bool
    send_enabled: bool
    stop_visible: bool
    choice_prompt: bool

    @property
    def send_ready(self) -> bool:
        return self.send_visible and self.send_enabled


async def observe_frontend(page: Page) -> FrontendState:
    try:
        raw = await page.evaluate(
            """() => {
              const visible = (element) => Boolean(element && element.getClientRects().length &&
                getComputedStyle(element).visibility !== 'hidden');
              const firstVisible = (selectors) => [...document.querySelectorAll(selectors)]
                .find(visible) || null;
              const composer = firstVisible(
                'div[role="textbox"][contenteditable="true"], ' +
                '[contenteditable="true"][data-lexical-editor="true"], ' +
                'textarea[data-testid="prompt-textarea"], #prompt-textarea'
              );
              const host = composer?.closest('form') ||
                composer?.closest('[data-testid="composer"]') || null;
              const send = firstVisible(
                'button[data-testid="send-button"], ' +
                'button[aria-label="Send prompt"], button[aria-label^="Send"]'
              );
              const stop = firstVisible(
                'button[data-testid="stop-button"], ' +
                'button[aria-label="Stop generating"], button[aria-label^="Stop"]'
              );
              const attachments = host ? [...host.querySelectorAll(
                '[data-filename], [data-file-name], ' +
                '[data-testid*="attachment"], [data-testid*="file"]'
              )].filter(visible) : [];
              const enabled = Boolean(send && !send.disabled &&
                send.getAttribute('aria-disabled') !== 'true');
              const choicePrompt = !composer && [
                ...document.querySelectorAll('button,[role="button"]')
              ]
                .filter(visible)
                .some((button) => /^(continue|allow|confirm|yes|choose|select)/i.test(
                  (button.innerText || button.getAttribute('aria-label') || '').trim()
                ));
              return {
                composer_present: Boolean(composer),
                composer_editable: Boolean(composer && !composer.disabled &&
                  composer.getAttribute('aria-disabled') !== 'true' &&
                  (composer.tagName === 'TEXTAREA' ||
                    composer.getAttribute('contenteditable') === 'true')),
                composer_text: String(
                  composer?.value ?? composer?.innerText ?? composer?.textContent ?? ''
                ),
                attachment_count: attachments.length,
                send_visible: Boolean(send),
                send_enabled: enabled,
                stop_visible: Boolean(stop),
                choice_prompt: choicePrompt,
              };
            }"""
        )
    except Exception as exc:
        raise FrontendNotReadyError("could not inspect ChatGPT composer state") from exc
    if not isinstance(raw, dict):
        raise FrontendNotReadyError("ChatGPT composer state is malformed")
    return FrontendState(
        url=str(page.url),
        composer_present=raw.get("composer_present") is True,
        composer_editable=raw.get("composer_editable") is True,
        composer_text=str(raw.get("composer_text") or ""),
        attachment_count=max(0, int(raw.get("attachment_count") or 0)),
        send_visible=raw.get("send_visible") is True,
        send_enabled=raw.get("send_enabled") is True,
        stop_visible=raw.get("stop_visible") is True,
        choice_prompt=raw.get("choice_prompt") is True,
    )


async def click_send_atomic(
    page: Page,
    prompt: str,
    *,
    conversation_id: str | None,
) -> None:
    """Revalidate the exact page/composer and click one enabled real Send button."""
    try:
        result = await page.evaluate(
            """({prompt: expectedPrompt, conversation_id: expectedConversationId}) => {
              const current = new URL(window.location.href);
              const exactOrigin = current.protocol === 'https:' &&
                ['chatgpt.com', 'www.chatgpt.com'].includes(current.hostname.toLowerCase()) &&
                !current.username && !current.password &&
                (current.port === '' || current.port === '443');
              const exactPath = expectedConversationId === null
                ? current.pathname === '/'
                : current.pathname === `/c/${expectedConversationId}`;
              const exactSuffix = current.search === '' && current.hash === '';
              if (!exactOrigin || !exactPath || !exactSuffix) {
                return {ok: false, reason: 'page_identity'};
              }
              const visible = (element) => Boolean(element && element.getClientRects().length &&
                getComputedStyle(element).visibility !== 'hidden');
              const composers = [...document.querySelectorAll(
                'div[role="textbox"][contenteditable="true"], ' +
                '[contenteditable="true"][data-lexical-editor="true"], ' +
                'textarea[data-testid="prompt-textarea"], #prompt-textarea'
              )].filter(visible);
              if (composers.length !== 1) return {ok: false, reason: 'composer_identity'};
              const composer = composers[0];
              const text = String(
                composer.value ?? composer.innerText ?? composer.textContent ?? ''
              ).trim();
              if (text !== expectedPrompt.trim()) {
                return {ok: false, reason: 'composer_changed'};
              }
              if (composer.disabled || composer.getAttribute('aria-disabled') === 'true') {
                return {ok: false, reason: 'composer_disabled'};
              }
              const host = composer.closest('form') ||
                composer.closest('[data-testid="composer"]') || null;
              const attachments = host ? [...host.querySelectorAll(
                '[data-filename], [data-file-name], ' +
                '[data-testid*="attachment"], [data-testid*="file"]'
              )].filter(visible) : [];
              if (attachments.length) {
                return {ok: false, reason: 'attachments_changed'};
              }
              const sends = [...document.querySelectorAll(
                'button[data-testid="send-button"], ' +
                'button[aria-label="Send prompt"], button[aria-label^="Send"]'
              )].filter((button) => visible(button) && !button.disabled &&
                button.getAttribute('aria-disabled') !== 'true');
              if (sends.length !== 1) return {ok: false, reason: 'send_identity'};
              sends[0].click();
              return {ok: true};
            }""",
            {"prompt": prompt, "conversation_id": conversation_id},
        )
    except Exception as exc:
        raise FrontendNotReadyError("could not dispatch the exact Send control") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        reason = result.get("reason") if isinstance(result, dict) else "malformed"
        raise FrontendNotReadyError(f"Send preflight changed before click: {reason}")


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


async def find_send_button(page: Page, *, timeout_ms: int = 3_000) -> Locator:
    return await _find_visible(page, _SEND_SELECTORS, "Send button", timeout_ms)


async def find_stop_button(page: Page) -> Locator | None:
    try:
        return await _find_visible(page, _STOP_SELECTORS, "Stop button", 1_000)
    except FrontendNotReadyError:
        return None
    except PlaywrightTimeoutError as exc:
        raise OperationTimeoutError("Stop control lookup timed out") from exc
    except PlaywrightError as exc:
        raise NetworkError("could not inspect the Stop control") from exc


async def click_stop_button(stop: Locator) -> None:
    try:
        await stop.click()
    except PlaywrightTimeoutError as exc:
        raise CancellationUnprovenError(
            "Stop click timed out after invocation; cancellation outcome is unknown"
        ) from exc
    except PlaywrightError as exc:
        raise CancellationUnprovenError(
            "Stop click failed after invocation; cancellation outcome is unknown"
        ) from exc


async def fill_composer(page: Page, prompt: str) -> None:
    composer = await find_composer(page)
    tag_name = str(await composer.evaluate("element => element.tagName")).upper()
    if tag_name == "TEXTAREA":
        await composer.fill(prompt)
    else:
        await composer.click()
        await composer.fill(prompt)
    await verify_composer(page, prompt)


async def verify_composer(page: Page, prompt: str) -> None:
    composer = await find_composer(page)
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
    deadline = time.monotonic() + max(timeout_ms, 1) / 1000
    while True:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if remaining_ms <= 0:
            break
        probe_ms = max(1, min(250, remaining_ms))
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=probe_ms)
                return locator
            except PlaywrightTimeoutError:
                continue
    raise FrontendNotReadyError(f"could not find visible ChatGPT {label}")
