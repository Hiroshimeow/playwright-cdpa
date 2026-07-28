from __future__ import annotations

import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .attachments import AttachmentInput, validate_attachments_unchanged
from .errors import (
    AuthenticationRequiredError,
    CancellationUnprovenError,
    FrontendNotReadyError,
    NetworkError,
    OperationTimeoutError,
)
from .projects import ProjectMemoryScope, ProjectRef
from .targets import ChatTarget, TargetKind

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


async def list_projects_frontend(page: Page) -> tuple[ProjectRef, ...]:
    try:
        await page.goto(f"{ORIGIN}/projects", wait_until="domcontentloaded", timeout=60_000)
        raw = await page.evaluate(
            """() => {
              const visible = (element) => Boolean(element && element.getClientRects().length &&
                getComputedStyle(element).visibility !== 'hidden');
              return [...document.querySelectorAll('a[href^="/g/g-p-"][href$="/project"]')]
                .filter(visible)
                .map((anchor) => {
                  const card = anchor.closest('article,[role="listitem"],[data-testid*="project"]') || anchor;
                  const name = String(
                    anchor.getAttribute('aria-label') ||
                    card.querySelector('h1,h2,h3,[data-testid*="name"]')?.textContent ||
                    anchor.textContent || ''
                  ).trim();
                  const text = String(card.textContent || '');
                  return {
                    href: anchor.href,
                    name,
                    memory_scope: /project[- ]only memory/i.test(text)
                      ? 'project_only' : 'default',
                  };
                });
            }"""
        )
    except PlaywrightTimeoutError as exc:
        raise OperationTimeoutError("project list navigation timed out") from exc
    except PlaywrightError as exc:
        raise NetworkError("could not inspect ChatGPT projects") from exc
    if not isinstance(raw, list):
        raise FrontendNotReadyError("project list response is malformed")
    projects: list[ProjectRef] = []
    for item in raw:
        if not isinstance(item, dict):
            raise FrontendNotReadyError("project list item is malformed")
        try:
            target = ChatTarget.parse(str(item.get("href") or ""))
            if target.kind is not TargetKind.PROJECT or target.project_id is None:
                raise ValueError("not a project root")
            projects.append(
                ProjectRef(
                    project_id=target.project_id,
                    canonical_url=target.canonical_url,
                    name=str(item.get("name") or ""),
                    memory_scope=ProjectMemoryScope(str(item.get("memory_scope") or "default")),
                )
            )
        except (ValueError, TypeError) as exc:
            raise FrontendNotReadyError("project identity could not be proven") from exc
    return tuple(projects)


async def create_project_frontend(
    page: Page,
    *,
    name: str,
    memory_scope: ProjectMemoryScope,
) -> ProjectRef:
    try:
        await page.goto(f"{ORIGIN}/projects", wait_until="domcontentloaded", timeout=60_000)
        create_control = await _find_visible(
            page,
            (
                'button[data-testid="create-project-button"]',
                'button[aria-label="New project"]',
                'button[aria-label="Create project"]',
            ),
            "Create project control",
            5_000,
        )
        await create_control.click()
        name_input = await _find_visible(
            page,
            (
                '[role="dialog"] input[name="name"]',
                '[role="dialog"] input[placeholder*="project name" i]',
                'input[data-testid="project-name-input"]',
            ),
            "project name input",
            5_000,
        )
        await name_input.fill(name)
        if memory_scope is ProjectMemoryScope.PROJECT_ONLY:
            option = page.get_by_text("Project-only memory", exact=False).first
            await option.click(timeout=5_000)
        submit = await _find_visible(
            page,
            (
                '[role="dialog"] button[data-testid="create-project-button"]',
                '[role="dialog"] button[type="submit"]',
                '[role="dialog"] button[aria-label="Create project"]',
            ),
            "project creation submit control",
            5_000,
        )
        await submit.click()
        await page.wait_for_url("**/g/g-p-*/project", timeout=60_000)
    except PlaywrightTimeoutError as exc:
        raise OperationTimeoutError("project creation outcome is unknown") from exc
    except PlaywrightError as exc:
        raise NetworkError("project creation frontend flow failed") from exc
    try:
        target = ChatTarget.parse(page.url)
    except Exception as exc:
        raise FrontendNotReadyError("created project URL could not be proven") from exc
    if target.kind is not TargetKind.PROJECT or target.project_id is None:
        raise FrontendNotReadyError("created project did not resolve to an exact project root")
    return ProjectRef(
        project_id=target.project_id,
        canonical_url=target.canonical_url,
        name=name,
        memory_scope=memory_scope,
    )


async def attachment_names(page: Page) -> tuple[str, ...]:
    try:
        raw = await page.evaluate(
            """() => {
              const visible = (element) => Boolean(element && element.getClientRects().length &&
                getComputedStyle(element).visibility !== 'hidden');
              const composer = [...document.querySelectorAll(
                'div[role="textbox"][contenteditable="true"], ' +
                '[contenteditable="true"][data-lexical-editor="true"], ' +
                'textarea[data-testid="prompt-textarea"], #prompt-textarea'
              )].find(visible) || null;
              const host = composer?.closest('form') ||
                composer?.closest('[data-testid="composer"]') || null;
              if (!host) return [];
              const selector = '[data-filename], [data-file-name], ' +
                '[data-testid*="attachment"], [data-testid*="file"]';
              return [...host.querySelectorAll(selector)]
                .filter((element) => visible(element) &&
                  !element.parentElement?.closest(selector))
                .map((element) => String(
                  element.getAttribute('data-filename') ||
                  element.getAttribute('data-file-name') ||
                  element.getAttribute('aria-label') ||
                  element.textContent || ''
                ).trim())
                .filter(Boolean);
            }"""
        )
    except Exception as exc:
        raise FrontendNotReadyError("could not inspect attachment identity") from exc
    if not isinstance(raw, list) or any(type(item) is not str or not item for item in raw):
        raise FrontendNotReadyError("attachment identity response is malformed")
    return tuple(raw)


async def upload_attachments(
    page: Page,
    attachments: Sequence[AttachmentInput],
    *,
    timeout: float = 60.0,
) -> None:
    expected = tuple(attachments)
    if not expected:
        return
    validate_attachments_unchanged(expected)
    inputs = page.locator('input[type="file"]')
    count = await inputs.count()
    if count == 0:
        opened = False
        for selector in (
            'button[data-testid="composer-plus-btn"]',
            'button[aria-label="Attach files"]',
            'button[aria-label="Upload files"]',
            'button[aria-label^="Add files"]',
        ):
            control = page.locator(selector).first
            try:
                await control.click(timeout=2_000)
                opened = True
                break
            except (PlaywrightError, PlaywrightTimeoutError):
                continue
        if not opened:
            raise FrontendNotReadyError("could not open the ChatGPT attachment menu")
        inputs = page.locator('input[type="file"]')
        count = await inputs.count()
    if count != 1:
        raise FrontendNotReadyError("could not identify one exact ChatGPT file input")
    try:
        await inputs.first.set_input_files([str(item.path) for item in expected])
    except PlaywrightTimeoutError as exc:
        raise OperationTimeoutError("attachment upload invocation timed out") from exc
    except PlaywrightError as exc:
        raise NetworkError("attachment upload invocation failed") from exc

    expected_names = Counter(item.path.name for item in expected)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = Counter(await attachment_names(page))
        if observed == expected_names:
            return
        if observed and any(observed[name] > expected_names[name] for name in observed):
            raise FrontendNotReadyError("unexpected or duplicate attachment identity appeared")
        await page.wait_for_timeout(200)
    raise FrontendNotReadyError("exact attachment identity was not proven after upload")


async def click_send_atomic(
    page: Page,
    prompt: str,
    *,
    target: ChatTarget,
    expected_attachment_names: Counter[str] | dict[str, int] | None = None,
) -> None:
    """Revalidate the exact target, composer, attachments, then click Send once."""
    expected_names = Counter(expected_attachment_names or {})
    target_path = target.canonical_url.removeprefix(ORIGIN)
    try:
        result = await page.evaluate(
            """({prompt: expectedPrompt, target_path: expectedPath,
                    attachment_names: expectedAttachmentNames}) => {
              const current = new URL(window.location.href);
              const exactOrigin = current.protocol === 'https:' &&
                ['chatgpt.com', 'www.chatgpt.com'].includes(current.hostname.toLowerCase()) &&
                !current.username && !current.password &&
                (current.port === '' || current.port === '443');
              const exactPath = current.pathname === expectedPath;
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
              const selector = '[data-filename], [data-file-name], ' +
                '[data-testid*="attachment"], [data-testid*="file"]';
              const names = host ? [...host.querySelectorAll(selector)]
                .filter((element) => visible(element) &&
                  !element.parentElement?.closest(selector))
                .map((element) => String(
                  element.getAttribute('data-filename') ||
                  element.getAttribute('data-file-name') ||
                  element.getAttribute('aria-label') ||
                  element.textContent || ''
                ).trim())
                .filter(Boolean) : [];
              const observed = Object.create(null);
              for (const name of names) observed[name] = (observed[name] || 0) + 1;
              const expectedKeys = Object.keys(expectedAttachmentNames).sort();
              const observedKeys = Object.keys(observed).sort();
              if (JSON.stringify(expectedKeys) !== JSON.stringify(observedKeys) ||
                  expectedKeys.some((name) => observed[name] !== expectedAttachmentNames[name])) {
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
            {
                "prompt": prompt,
                "target_path": target_path,
                "attachment_names": dict(expected_names),
            },
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
