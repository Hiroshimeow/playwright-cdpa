from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .attachments import AttachmentInput, validate_attachments_unchanged
from .errors import (
    AuthenticationRequiredError,
    CancellationUnprovenError,
    ConflictingIdentityError,
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


@dataclass(frozen=True, slots=True)
class AttachmentState:
    names: tuple[str, ...]
    pending: bool


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
              const fileTiles = host ? [...host.querySelectorAll('[role="group"][aria-label]')]
                .filter((element) => visible(element) &&
                  element.querySelector('button[aria-label^="Remove file "]')) : [];
              const legacyAttachments = host ? [...host.querySelectorAll(
                '[data-filename], [data-file-name]'
              )].filter((element) => visible(element) &&
                !element.parentElement?.closest('[data-filename], [data-file-name]')) : [];
              const attachments = fileTiles.length ? fileTiles : legacyAttachments;
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


async def find_project_frontend(
    page: Page,
    *,
    project_id: str | None = None,
    name: str | None = None,
) -> ProjectRef | None:
    if project_id is None and name is None:
        raise ValueError("project_id or name is required")
    expected_target = ChatTarget.project(project_id) if project_id is not None else None
    try:
        if expected_target is not None:
            await page.goto(
                expected_target.canonical_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            try:
                observed_target = ChatTarget.parse(page.url)
            except Exception:
                return None
            if observed_target != expected_target:
                return None
        else:
            assert name is not None
            await page.goto(f"{ORIGIN}/projects", wait_until="domcontentloaded", timeout=60_000)
            grid = page.locator('[role="grid"][aria-label="Projects"]').first
            await grid.wait_for(state="visible", timeout=10_000)
            readiness_deadline = time.monotonic() + 5.0
            while True:
                selection = await page.evaluate(
                    """expectedName => {
                      const visible = (element) => Boolean(element && element.getClientRects().length &&
                        getComputedStyle(element).visibility !== 'hidden');
                      const grid = [...document.querySelectorAll('[role="grid"][aria-label="Projects"]')]
                        .find(visible);
                      if (!grid) return {count: null};
                      const rows = [...grid.querySelectorAll(
                        '[role="row"][data-page-table-selectable-row="true"]'
                      )].filter(visible);
                      const matches = rows.filter((row) => [...row.querySelectorAll('div,span')]
                        .filter((element) => visible(element) && element.children.length === 0)
                        .some((element) => String(element.textContent || '').trim() === expectedName));
                      if (matches.length === 1) matches[0].click();
                      return {count: matches.length};
                    }""",
                    name,
                )
                if (
                    not isinstance(selection, dict)
                    or type(selection.get("count")) is not int
                ):
                    raise FrontendNotReadyError(
                        "project row selection response is malformed"
                    )
                count = selection["count"]
                if count == 1:
                    break
                if count > 1:
                    raise ConflictingIdentityError("multiple exact projects matched")
                if time.monotonic() >= readiness_deadline:
                    return None
                await page.wait_for_timeout(250)
            await page.wait_for_url("**/g/g-p-*/project", timeout=60_000)
            try:
                observed_target = ChatTarget.parse(page.url)
            except Exception as exc:
                raise FrontendNotReadyError(
                    "selected project did not resolve to an exact project URL"
                ) from exc
            if observed_target.kind is not TargetKind.PROJECT:
                raise FrontendNotReadyError(
                    "selected project did not resolve to an exact project root"
                )

        title = await _find_visible(
            page,
            ('button[name="project-title"]:visible',),
            "project title",
            5_000,
        )
        observed_name = str(await title.inner_text()).strip()
        if not observed_name:
            raise FrontendNotReadyError("project title is empty")
        if name is not None and observed_name != name:
            return None

        details = await _find_visible(
            page,
            ('button[aria-label="Show project details"]:visible',),
            "project details control",
            5_000,
        )
        await details.evaluate("element => element.click()")
        settings = page.get_by_text("Project settings", exact=True).first
        await settings.wait_for(state="visible", timeout=5_000)
        await settings.evaluate("element => element.click()")
        form = page.locator('form[aria-label="Project settings"]').first
        await form.wait_for(state="visible", timeout=5_000)
        metadata = await form.evaluate(
            """form => {
              const projectName = String(
                form.querySelector('#project-name')?.value ||
                form.querySelector('input[aria-label="Project name"]')?.value || ''
              ).trim();
              const memorySection = [...form.querySelectorAll('section')].find((section) =>
                [...section.querySelectorAll('label')].some((label) =>
                  String(label.textContent || '').trim() === 'Memory'
                )
              );
              const memoryValue = String(
                memorySection?.querySelector('span')?.textContent || ''
              ).trim();
              let memoryScope = null;
              if (memoryValue === 'Project-only') memoryScope = 'project_only';
              else if (memoryValue === 'Default') memoryScope = 'default';
              return {name: projectName, memory_scope: memoryScope};
            }"""
        )
    except PlaywrightTimeoutError as exc:
        raise FrontendNotReadyError("project identity controls did not become available") from exc
    except PlaywrightError as exc:
        raise NetworkError("project identity frontend flow failed") from exc

    if not isinstance(metadata, dict):
        raise FrontendNotReadyError("project metadata response is malformed")
    metadata_name = metadata.get("name")
    metadata_scope = metadata.get("memory_scope")
    if type(metadata_name) is not str or metadata_name != observed_name:
        raise FrontendNotReadyError("project settings title does not match project page")
    try:
        memory_scope = ProjectMemoryScope(metadata_scope)
    except (TypeError, ValueError) as exc:
        raise FrontendNotReadyError("project memory scope could not be proven") from exc
    assert observed_target.project_id is not None
    return ProjectRef(
        project_id=observed_target.project_id,
        canonical_url=observed_target.canonical_url,
        name=observed_name,
        memory_scope=memory_scope,
    )


async def create_project_frontend(
    page: Page,
    *,
    name: str,
    memory_scope: ProjectMemoryScope,
    on_create_boundary: Callable[[], None],
) -> ProjectRef:
    create_boundary_entered = False
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
        await create_control.evaluate("element => element.click()")
        name_input = await _find_visible(
            page,
            (
                'form[data-testid="create-new-project-form"] input[name="projectName"]',
                '#project-name',
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
                'form[data-testid="create-new-project-form"] button[type="submit"]',
                '[role="dialog"] button[data-testid="create-project-button"]',
                '[role="dialog"] button[type="submit"]',
                '[role="dialog"] button[aria-label="Create project"]',
            ),
            "project creation submit control",
            5_000,
        )
        on_create_boundary()
        create_boundary_entered = True
        await submit.evaluate("element => element.click()")
        await page.wait_for_url("**/g/g-p-*/project", timeout=60_000)
    except PlaywrightTimeoutError as exc:
        if create_boundary_entered:
            raise OperationTimeoutError("project creation outcome is unknown") from exc
        raise FrontendNotReadyError("project creation preflight timed out before Create") from exc
    except PlaywrightError as exc:
        if create_boundary_entered:
            raise OperationTimeoutError("project creation outcome is unknown") from exc
        raise NetworkError("project creation frontend preflight failed") from exc
    try:
        target = ChatTarget.parse(page.url)
    except Exception as exc:
        raise FrontendNotReadyError("created project URL could not be proven") from exc
    if target.kind is not TargetKind.PROJECT or target.project_id is None:
        raise FrontendNotReadyError("created project did not resolve to an exact project root")
    observed = await find_project_frontend(
        page,
        project_id=target.project_id,
        name=name,
    )
    if observed is None:
        raise ConflictingIdentityError("created project name could not be proven")
    if observed.memory_scope is not memory_scope:
        raise ConflictingIdentityError(
            "created project memory scope differs from requested metadata"
        )
    return observed


async def attachment_state(page: Page) -> AttachmentState:
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
              if (!host) return {names: [], pending: false};
              const fileTiles = [...host.querySelectorAll('[role="group"][aria-label]')]
                .filter((element) => visible(element) &&
                  element.querySelector('button[aria-label^="Remove file "]'));
              const legacyAttachments = [...host.querySelectorAll(
                '[data-filename], [data-file-name]'
              )].filter((element) => visible(element) &&
                !element.parentElement?.closest('[data-filename], [data-file-name]'));
              const attachments = fileTiles.length ? fileTiles : legacyAttachments;
              const names = attachments.map((element) => String(
                element.getAttribute('data-filename') ||
                element.getAttribute('data-file-name') ||
                element.getAttribute('aria-label') || ''
              ).trim()).filter(Boolean);
              const pending = attachments.some((element) =>
                element.matches('[aria-busy="true"]') ||
                Boolean(element.querySelector('[aria-busy="true"], .cursor-wait'))
              );
              return {names, pending};
            }"""
        )
    except Exception as exc:
        raise FrontendNotReadyError("could not inspect attachment identity") from exc
    if not isinstance(raw, dict):
        raise FrontendNotReadyError("attachment identity response is malformed")
    names = raw.get("names")
    pending = raw.get("pending")
    if (
        not isinstance(names, list)
        or any(type(item) is not str or not item for item in names)
        or type(pending) is not bool
    ):
        raise FrontendNotReadyError("attachment identity response is malformed")
    return AttachmentState(tuple(names), pending)


async def attachment_names(page: Page) -> tuple[str, ...]:
    return (await attachment_state(page)).names


async def upload_attachments(
    page: Page,
    attachments: Sequence[AttachmentInput],
    *,
    timeout: float = 60.0,
    on_upload_boundary: Callable[[], None] | None = None,
) -> None:
    expected = tuple(attachments)
    if not expected:
        return
    validate_attachments_unchanged(expected)
    inputs = page.locator('input[type="file"]#upload-files')
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
        inputs = page.locator('input[type="file"]#upload-files')
        count = await inputs.count()
    if count != 1:
        raise FrontendNotReadyError("could not identify one exact ChatGPT file input")
    if on_upload_boundary is not None:
        on_upload_boundary()
    try:
        await inputs.first.set_input_files([str(item.path) for item in expected])
    except PlaywrightTimeoutError as exc:
        raise OperationTimeoutError("attachment upload invocation timed out") from exc
    except PlaywrightError as exc:
        raise NetworkError("attachment upload invocation failed") from exc

    expected_names = Counter(item.path.name for item in expected)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await attachment_state(page)
        observed = Counter(state.names)
        if observed == expected_names and not state.pending:
            return
        if observed and any(observed[name] > expected_names[name] for name in observed):
            raise FrontendNotReadyError("unexpected or duplicate attachment identity appeared")
        await page.wait_for_timeout(200)
    raise FrontendNotReadyError("exact attachment identity was not proven after upload")


def _atomic_target_path_pattern(target: ChatTarget) -> str:
    if target.kind not in {TargetKind.PROJECT, TargetKind.PROJECT_CONVERSATION}:
        return f"^{re.escape(target.canonical_url.removeprefix(ORIGIN))}$"
    assert target.project_id is not None
    project_segment = re.escape(target.project_id)
    if re.fullmatch(r"g-p-[0-9a-f]{32}", target.project_id, re.IGNORECASE):
        project_segment += r"(?:-[A-Za-z0-9][A-Za-z0-9_-]{0,127})?"
    suffix = (
        "/project"
        if target.kind is TargetKind.PROJECT
        else f"/c/{re.escape(target.conversation_id or '')}"
    )
    return f"^/g/{project_segment}{suffix}$"


async def click_send_atomic(
    page: Page,
    prompt: str,
    *,
    target: ChatTarget,
    expected_attachment_names: Counter[str] | dict[str, int] | None = None,
) -> None:
    """Revalidate the exact target, composer, attachments, then click Send once."""
    expected_names = Counter(expected_attachment_names or {})
    target_path_pattern = _atomic_target_path_pattern(target)
    try:
        result = await page.evaluate(
            """({prompt: expectedPrompt, target_path_pattern: expectedPathPattern,
                    attachment_names: expectedAttachmentNames}) => {
              const current = new URL(window.location.href);
              const exactOrigin = current.protocol === 'https:' &&
                ['chatgpt.com', 'www.chatgpt.com'].includes(current.hostname.toLowerCase()) &&
                !current.username && !current.password &&
                (current.port === '' || current.port === '443');
              const exactPath = new RegExp(expectedPathPattern).test(current.pathname);
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
              const fileTiles = host ? [...host.querySelectorAll('[role="group"][aria-label]')]
                .filter((element) => visible(element) &&
                  element.querySelector('button[aria-label^="Remove file "]')) : [];
              const legacyAttachments = host ? [...host.querySelectorAll(
                '[data-filename], [data-file-name]'
              )].filter((element) => visible(element) &&
                !element.parentElement?.closest('[data-filename], [data-file-name]')) : [];
              const attachments = fileTiles.length ? fileTiles : legacyAttachments;
              const names = attachments.map((element) => String(
                element.getAttribute('data-filename') ||
                element.getAttribute('data-file-name') ||
                element.getAttribute('aria-label') || ''
              ).trim()).filter(Boolean);
              if (attachments.some((element) =>
                element.matches('[aria-busy="true"]') ||
                Boolean(element.querySelector('[aria-busy="true"], .cursor-wait'))
              )) {
                return {ok: false, reason: 'attachments_pending'};
              }
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
                "target_path_pattern": target_path_pattern,
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
