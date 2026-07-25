# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "playwright>=1.52,<2",
# ]
# ///

"""
ssgpt.py — ChatGPT Web CDP runner with persistent conversation registry.

Transport:
    open/reuse ChatGPT conversation
    -> paste task
    -> click the real frontend Send button
    -> do not intercept, abort, modify, or replay the request
    -> read conversation_id from the real frontend response
    -> persist the conversation registry
    -> close the helper tab
    -> monitor backend status, tool calls, tool results, and final response

Examples:
    uv run ssgpt.py "Reply with exactly OK"

    # Reuse the most recently used conversation (default).
    uv run ssgpt.py "Continue the previous task"

    # Open a specific conversation ID or full ChatGPT URL.
    uv run ssgpt.py "Continue" --url 6a63835f-1f70-83e8-bf99-d8871222e702
    uv run ssgpt.py "Continue" --url https://chatgpt.com/c/6a63835f-1f70-83e8-bf99-d8871222e702

    # Select a saved entry by its JSON key.
    uv run ssgpt.py "Continue" --id 1
    uv run ssgpt.py "Continue" --id ilovegpt

    # Force a fresh conversation.
    uv run ssgpt.py "Start a fresh task" --new

    # Attach to the response currently running on a saved conversation.
    uv run ssgpt.py --id ilovegpt --watch

    # Wait for the existing response, then send the next task.
    uv run ssgpt.py "Next task" --id ilovegpt --wait-idle

    # Wait up to one hour per monitored turn.
    uv run ssgpt.py "Long task" --timeout 3600

Registry:
    ssgpt_conversations.json is created next to this script.

    New conversations receive numeric keys: "0", "1", "2", ...
    You may stop the script and rename those keys manually, for example:
        "1" -> "ilovegpt"

    The next run can then use:
        --id ilovegpt

Important:
    This uses undocumented ChatGPT Web endpoints and DOM selectors. They may
    change without notice. The Chromium process exposed through CDP remains
    owned by the user; this script never calls browser.close().
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    APIRequestContext,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


VERSION = "2026-07-25-active-turn-watch-v5"
DEFAULT_CDP = os.environ.get("SSGPT_CDP", "http://127.0.0.1:9222")
ORIGIN = "https://chatgpt.com"
STORE_FILENAME = "ssgpt_conversations.json"
CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class SSGPTError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_clock() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S")


def log(kind: str, message: str) -> None:
    print(f"[{local_clock()}] [{kind}] {message}", flush=True)


def sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "<max-depth>"

    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if any(
                marker in lowered
                for marker in (
                    "authorization",
                    "cookie",
                    "access_token",
                    "accesstoken",
                    "sentinel",
                    "turnstile",
                    "proof_token",
                    "password",
                    "secret",
                    "api_key",
                    "apikey",
                )
            ):
                output[key] = "<redacted>"
            else:
                output[key] = sanitize(raw_value, depth=depth + 1)
        return output

    if isinstance(value, list):
        return [sanitize(item, depth=depth + 1) for item in value[:100]]

    if isinstance(value, tuple):
        return [sanitize(item, depth=depth + 1) for item in value[:100]]

    if isinstance(value, str) and len(value) > 6_000:
        return value[:6_000] + f"... <truncated {len(value) - 6_000} chars>"

    return value


def log_json(kind: str, value: Any, *, compact: bool) -> None:
    if compact:
        return
    rendered = json.dumps(sanitize(value), ensure_ascii=False, indent=2)
    for line in rendered.splitlines():
        log(kind, line)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Send a task through ChatGPT Web, or attach to an already-running "
            "conversation turn, using an existing CDP browser session."
        )
    )
    parser.add_argument(
        "task",
        nargs="?",
        help=(
            "Task/prompt text. Required for send mode and --wait-idle; "
            "omit it with --watch."
        ),
    )

    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--url",
        metavar="CONVERSATION",
        help=(
            "Conversation ID or full https://chatgpt.com/c/<id> URL. "
            "Unknown conversations are added to the registry automatically."
        ),
    )
    selector.add_argument(
        "--id",
        metavar="REGISTRY_ID",
        help='Saved registry key, for example "1" or "ilovegpt".',
    )
    selector.add_argument(
        "--new",
        action="store_true",
        help="Force a fresh normal ChatGPT conversation.",
    )

    behavior = parser.add_mutually_exclusive_group()
    behavior.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Do not send a new task. Attach to the currently running turn "
            "for the selected conversation and print its progress until done."
        ),
    )
    behavior.add_argument(
        "--wait-idle",
        action="store_true",
        help=(
            "If the selected conversation is already responding, attach and "
            "wait for it to finish, then send the supplied task."
        ),
    )

    parser.add_argument(
        "--cdp",
        default=DEFAULT_CDP,
        help=f"CDP endpoint. Default: {DEFAULT_CDP}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1_800.0,
        help="Maximum backend wait in seconds per monitored turn. Default: 1800.",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=1.0,
        help="Backend polling interval in seconds. Default: 1.",
    )
    parser.add_argument(
        "--send-timeout",
        type=float,
        default=90.0,
        help="Maximum seconds to obtain the initial send response. Default: 90.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Hide expanded JSON payloads while retaining event summaries.",
    )
    parser.add_argument(
        "--keep-tab",
        action="store_true",
        help="Keep the helper tab open after backend handoff.",
    )
    return parser.parse_args()


def store_path() -> Path:
    return Path(__file__).resolve().with_name(STORE_FILENAME)


def empty_store() -> dict[str, Any]:
    return {
        "version": 2,
        "_note": (
            "Conversation keys are user-editable. New keys are numeric. "
            "You may rename a key such as '1' to 'ilovegpt', then use "
            "`--id ilovegpt`."
        ),
        "last_used_id": None,
        "conversations": {},
    }


def load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_store()

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SSGPTError(
            f"Conversation registry is invalid JSON: {path}: {exc}"
        ) from exc
    except OSError as exc:
        raise SSGPTError(f"Could not read conversation registry: {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise SSGPTError(f"Conversation registry root must be an object: {path}")

    conversations = value.get("conversations")
    if conversations is None:
        conversations = {}
        value["conversations"] = conversations
    if not isinstance(conversations, dict):
        raise SSGPTError(
            f'Conversation registry field "conversations" must be an object: {path}'
        )

    value.setdefault("version", 1)
    value.setdefault("last_used_id", None)
    value.setdefault(
        "_note",
        (
            "Conversation keys are user-editable. New keys are numeric. "
            "Rename a key manually and select it with --id."
        ),
    )
    return value


def save_store(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SSGPTError(f"Could not save conversation registry: {path}: {exc}") from exc


def conversation_url(conversation_id: str) -> str:
    return f"{ORIGIN}/c/{conversation_id}"


def normalize_conversation(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise SSGPTError("Conversation URL/ID is empty.")

    if "://" not in raw:
        conversation_id = raw.strip("/")
    else:
        parsed = urlparse(raw)
        hostname = (parsed.hostname or "").casefold()
        if hostname not in {"chatgpt.com", "www.chatgpt.com"}:
            raise SSGPTError(
                f"Conversation URL must use chatgpt.com, got: {raw!r}"
            )
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0] != "c":
            raise SSGPTError(
                "Conversation URL must have the form "
                "https://chatgpt.com/c/<conversation_id>."
            )
        conversation_id = parts[1]

    if not CONVERSATION_ID_PATTERN.fullmatch(conversation_id):
        raise SSGPTError(
            f"Invalid ChatGPT conversation ID: {conversation_id!r}"
        )

    return conversation_id, conversation_url(conversation_id)


def entry_conversation(
    registry_id: str,
    entry: Any,
) -> tuple[str, str]:
    if not isinstance(entry, dict):
        raise SSGPTError(
            f"Registry entry {registry_id!r} must be a JSON object."
        )

    raw_id = entry.get("conversation_id")
    raw_url = entry.get("url")

    if isinstance(raw_id, str) and raw_id.strip():
        return normalize_conversation(raw_id)
    if isinstance(raw_url, str) and raw_url.strip():
        return normalize_conversation(raw_url)

    raise SSGPTError(
        f"Registry entry {registry_id!r} has no conversation_id or URL."
    )


def find_registry_id_by_conversation(
    store: dict[str, Any],
    conversation_id: str,
) -> str | None:
    conversations = store["conversations"]
    for registry_id, entry in conversations.items():
        try:
            entry_id, _url = entry_conversation(str(registry_id), entry)
        except SSGPTError:
            continue
        if entry_id == conversation_id:
            return str(registry_id)
    return None


def next_numeric_registry_id(store: dict[str, Any]) -> str:
    numeric_ids: list[int] = []
    for raw_id in store["conversations"]:
        value = str(raw_id)
        if value.isdecimal():
            numeric_ids.append(int(value))
    return str(max(numeric_ids, default=-1) + 1)


def latest_registry_id(store: dict[str, Any]) -> str | None:
    conversations = store["conversations"]
    configured = store.get("last_used_id")
    if configured is not None and str(configured) in conversations:
        return str(configured)

    ranked: list[tuple[str, str]] = []
    for registry_id, entry in conversations.items():
        if not isinstance(entry, dict):
            continue
        timestamp = str(
            entry.get("last_used_at")
            or entry.get("created_at")
            or ""
        )
        ranked.append((timestamp, str(registry_id)))

    if not ranked:
        return None
    ranked.sort()
    return ranked[-1][1]


@dataclass(frozen=True)
class Target:
    mode: str
    url: str
    expected_conversation_id: str | None
    registry_id: str | None


def resolve_target(args: argparse.Namespace, store: dict[str, Any]) -> Target:
    if args.new:
        return Target(
            mode="new",
            url=f"{ORIGIN}/",
            expected_conversation_id=None,
            registry_id=None,
        )

    if args.url:
        conversation_id, url = normalize_conversation(args.url)
        return Target(
            mode="url",
            url=url,
            expected_conversation_id=conversation_id,
            registry_id=find_registry_id_by_conversation(
                store,
                conversation_id,
            ),
        )

    if args.id is not None:
        registry_id = str(args.id)
        entry = store["conversations"].get(registry_id)
        if entry is None:
            raise SSGPTError(
                f"Registry ID {registry_id!r} was not found in {store_path()}."
            )
        conversation_id, url = entry_conversation(registry_id, entry)
        return Target(
            mode="id",
            url=url,
            expected_conversation_id=conversation_id,
            registry_id=registry_id,
        )

    registry_id = latest_registry_id(store)
    if registry_id is None:
        return Target(
            mode="new-default",
            url=f"{ORIGIN}/",
            expected_conversation_id=None,
            registry_id=None,
        )

    conversation_id, url = entry_conversation(
        registry_id,
        store["conversations"][registry_id],
    )
    return Target(
        mode="recent",
        url=url,
        expected_conversation_id=conversation_id,
        registry_id=registry_id,
    )


def register_conversation(
    store: dict[str, Any],
    *,
    conversation_id: str,
    preferred_registry_id: str | None,
    model: str | None,
    thinking_effort: str | None,
    turn_exchange_id: str | None,
    status: str,
    error: str | None = None,
) -> str:
    conversations = store["conversations"]

    registry_id = preferred_registry_id
    if registry_id is None:
        registry_id = find_registry_id_by_conversation(
            store,
            conversation_id,
        )
    if registry_id is None:
        registry_id = next_numeric_registry_id(store)

    existing = conversations.get(registry_id)
    entry = dict(existing) if isinstance(existing, dict) else {}
    timestamp = utc_now()

    entry.setdefault("created_at", timestamp)
    entry.update(
        {
            "conversation_id": conversation_id,
            "url": conversation_url(conversation_id),
            "last_used_at": timestamp,
            "last_status": status,
            "last_error": error,
            "last_model": model,
            "last_thinking_effort": thinking_effort,
            "last_turn_exchange_id": turn_exchange_id,
        }
    )
    conversations[registry_id] = entry
    store["last_used_id"] = registry_id
    return registry_id


TERMINAL_STREAM_STATUSES = frozenset(
    {
        "COMPLETE",
        "COMPLETED",
        "FAILED",
        "ERROR",
        "CANCELLED",
        "CANCELED",
        "IDLE",
        "NOT_FOUND",
    }
)


def stream_status_value(body: dict[str, Any] | None) -> str:
    if not isinstance(body, dict):
        return ""
    return str(body.get("status") or "").strip().upper()


def stream_is_active(http_status: int, status: str) -> bool:
    return (
        http_status == 200
        and bool(status)
        and status not in TERMINAL_STREAM_STATUSES
    )


def current_branch_messages(
    conversation: dict[str, Any],
) -> list[dict[str, Any]]:
    mapping = conversation.get("mapping")
    current_node = conversation.get("current_node")
    if not isinstance(mapping, dict) or not isinstance(current_node, str):
        return ordered_messages(conversation)

    chain: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    node_id: str | None = current_node

    while node_id and node_id not in seen_nodes:
        seen_nodes.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            break

        message = node.get("message")
        if isinstance(message, dict):
            copy = dict(message)
            copy["_node_id"] = node_id
            chain.append(copy)

        parent = node.get("parent")
        node_id = str(parent) if isinstance(parent, str) and parent else None

    chain.reverse()
    return chain or ordered_messages(conversation)


def latest_turn_identity(
    conversation: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(conversation, dict):
        return {}

    for message in reversed(current_branch_messages(conversation)):
        metadata_raw = message.get("metadata")
        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
        turn_exchange_id = metadata.get("turn_exchange_id")
        working_turn_id = metadata.get("working_turn_id")
        request_id = metadata.get("request_id")
        model = (
            metadata.get("model_slug")
            or metadata.get("resolved_model_slug")
        )
        thinking_effort = metadata.get("thinking_effort")

        if any(
            value is not None
            for value in (
                turn_exchange_id,
                working_turn_id,
                request_id,
                model,
                thinking_effort,
            )
        ):
            return {
                "turn_exchange_id": (
                    str(turn_exchange_id)
                    if turn_exchange_id is not None
                    else None
                ),
                "working_turn_id": (
                    str(working_turn_id)
                    if working_turn_id is not None
                    else None
                ),
                "request_id": (
                    str(request_id)
                    if request_id is not None
                    else None
                ),
                "model": str(model) if model is not None else None,
                "thinking_effort": (
                    str(thinking_effort)
                    if thinking_effort is not None
                    else None
                ),
                "message_id": str(
                    message.get("id")
                    or message.get("_node_id")
                    or ""
                )
                or None,
            }

    return {}


async def inspect_backend_conversation(
    backend: Backend,
    conversation_id: str,
) -> tuple[
    int,
    str,
    dict[str, Any] | None,
    int,
    dict[str, Any] | None,
]:
    status_http, status_body, status_raw = await get_json(
        backend,
        f"/backend-api/conversation/{conversation_id}/stream_status",
    )
    status = stream_status_value(status_body)

    graph_http, graph, graph_raw = await get_json(
        backend,
        f"/backend-api/conversation/{conversation_id}",
    )

    log(
        "PREFLIGHT",
        (
            f"conversation={conversation_id} "
            f"stream_http={status_http} "
            f"stream_status={status or '<unknown>'} "
            f"graph_http={graph_http}"
        ),
    )

    if status_body:
        log_json("PREFLIGHT_STATUS", status_body, compact=True)
    elif status_http not in {200, 404}:
        log("PREFLIGHT_STATUS_BODY", status_raw[:2_000])

    if graph is None and graph_http not in {200, 404}:
        log("PREFLIGHT_GRAPH_BODY", graph_raw[:2_000])

    return status_http, status, status_body, graph_http, graph


def stored_active_turn(
    store: dict[str, Any],
    registry_id: str | None,
) -> dict[str, Any]:
    if registry_id is None:
        return {}
    entry = store.get("conversations", {}).get(registry_id)
    if not isinstance(entry, dict):
        return {}
    active = entry.get("active_turn")
    return dict(active) if isinstance(active, dict) else {}


def save_active_turn(
    store: dict[str, Any],
    *,
    conversation_id: str,
    preferred_registry_id: str | None,
    turn_exchange_id: str | None,
    stream_topic_id: str | None,
    request_id: str | None,
    working_turn_id: str | None,
    user_message_id: str | None,
    model: str | None,
    thinking_effort: str | None,
    source: str,
    status: str = "RUNNING",
    accepted_at: str | None = None,
    error: str | None = None,
) -> str:
    registry_id = register_conversation(
        store,
        conversation_id=conversation_id,
        preferred_registry_id=preferred_registry_id,
        model=model,
        thinking_effort=thinking_effort,
        turn_exchange_id=turn_exchange_id,
        status=status,
        error=error,
    )
    entry = store["conversations"][registry_id]
    previous = (
        entry.get("active_turn")
        if isinstance(entry.get("active_turn"), dict)
        else {}
    )
    entry["active_turn"] = {
        "turn_exchange_id": turn_exchange_id,
        "stream_topic_id": stream_topic_id,
        "request_id": request_id,
        "working_turn_id": working_turn_id,
        "user_message_id": user_message_id,
        "status": status,
        "source": source,
        "accepted_at": (
            accepted_at
            or previous.get("accepted_at")
            or utc_now()
        ),
        "last_observed_at": utc_now(),
        "last_error": error,
    }
    return registry_id


def mark_turn_complete(
    store: dict[str, Any],
    *,
    conversation_id: str,
    registry_id: str,
    model: str | None,
    thinking_effort: str | None,
    turn_exchange_id: str | None,
) -> None:
    entry = store["conversations"].get(registry_id)
    if not isinstance(entry, dict):
        entry = {}
        store["conversations"][registry_id] = entry

    active = (
        dict(entry.get("active_turn"))
        if isinstance(entry.get("active_turn"), dict)
        else {}
    )
    active.update(
        {
            "turn_exchange_id": (
                turn_exchange_id
                or active.get("turn_exchange_id")
            ),
            "status": "COMPLETE",
            "completed_at": utc_now(),
            "last_error": None,
        }
    )
    entry["last_completed_turn"] = active
    entry["active_turn"] = None

    register_conversation(
        store,
        conversation_id=conversation_id,
        preferred_registry_id=registry_id,
        model=model,
        thinking_effort=thinking_effort,
        turn_exchange_id=turn_exchange_id,
        status="COMPLETE",
        error=None,
    )


def mark_turn_error(
    store: dict[str, Any],
    *,
    conversation_id: str,
    registry_id: str,
    model: str | None,
    thinking_effort: str | None,
    turn_exchange_id: str | None,
    error: str,
) -> None:
    entry = store["conversations"].get(registry_id)
    if isinstance(entry, dict) and isinstance(entry.get("active_turn"), dict):
        entry["active_turn"]["status"] = "ERROR"
        entry["active_turn"]["last_error"] = error
        entry["active_turn"]["last_observed_at"] = utc_now()

    register_conversation(
        store,
        conversation_id=conversation_id,
        preferred_registry_id=registry_id,
        model=model,
        thinking_effort=thinking_effort,
        turn_exchange_id=turn_exchange_id,
        status="ERROR",
        error=error,
    )



async def first_context(browser: Browser) -> BrowserContext:
    if not browser.contexts:
        raise SSGPTError("The CDP browser has no persistent browser context.")
    return browser.contexts[0]


async def find_composer(page: Page):
    selectors = (
        'div[role="textbox"][contenteditable="true"]',
        '[contenteditable="true"][data-lexical-editor="true"]',
        'textarea[data-testid="prompt-textarea"]',
    )
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=5_000)
            return locator
        except PlaywrightTimeoutError:
            continue
    raise SSGPTError("Could not find the ChatGPT composer.")


async def find_send_button(page: Page):
    selectors = (
        'button[data-testid="send-button"]:visible',
        'button[aria-label="Send prompt"]:visible',
        'button[aria-label^="Send"]:visible',
    )
    for selector in selectors:
        locator = page.locator(selector).first
        if not await locator.count():
            continue
        try:
            await locator.wait_for(state="visible", timeout=3_000)
            return locator
        except PlaywrightTimeoutError:
            continue
    raise SSGPTError("Could not find the ChatGPT Send button.")


async def verify_browser_session(page: Page) -> None:
    result = await page.evaluate(
        """async () => {
          const response = await fetch("/api/auth/session", {
            credentials: "include",
          });

          let session = null;
          try {
            session = await response.json();
          } catch {}

          return {
            status: response.status,
            ok: response.ok,
            hasToken: Boolean(
              session?.accessToken ?? session?.access_token
            ),
            expires: session?.expires ?? null,
          };
        }"""
    )
    if not result.get("ok") or not result.get("hasToken"):
        raise SSGPTError(
            "ChatGPT browser session is unavailable "
            f"(HTTP {result.get('status')}, "
            f"accessToken={result.get('hasToken')})."
        )
    log(
        "SESSION",
        f"authenticated; expires={result.get('expires') or '-'}",
    )


async def visible_message_ids(page: Page) -> set[str]:
    try:
        values = await page.locator("[data-message-id]").evaluate_all(
            """elements => elements
              .map(element => element.getAttribute("data-message-id"))
              .filter(Boolean)"""
        )
    except Exception:
        return set()
    return {str(value) for value in values if value}


def is_real_conversation_response(response: Any) -> bool:
    try:
        parsed = urlparse(response.url)
        return (
            response.request.method == "POST"
            and (parsed.hostname or "").casefold()
            in {"chatgpt.com", "www.chatgpt.com"}
            and parsed.path == "/backend-api/f/conversation"
        )
    except Exception:
        return False


def request_payload(request: Any) -> dict[str, Any]:
    try:
        value = json.loads(request.post_data or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def safe_api_headers(request: Any) -> dict[str, str]:
    allowed = {
        "chatgpt-account-id",
        "oai-client-build-number",
        "oai-client-version",
        "oai-device-id",
        "oai-language",
        "oai-session-id",
    }
    return {
        key.lower(): value
        for key, value in request.headers.items()
        if key.lower() in allowed
    }


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue

        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue

        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue

        if isinstance(value, dict):
            events.append(value)

    return events


def parse_handoff(
    text: str,
) -> tuple[str | None, str | None, str | None, list[dict[str, Any]]]:
    conversation_id = None
    turn_exchange_id = None
    topic_id = None
    events = parse_sse_events(text)

    for event in events:
        if isinstance(event.get("conversation_id"), str):
            conversation_id = event["conversation_id"]
        if isinstance(event.get("turn_exchange_id"), str):
            turn_exchange_id = event["turn_exchange_id"]

        options = event.get("options")
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue
                candidate = option.get("topic_id")
                if isinstance(candidate, str) and candidate:
                    topic_id = candidate

    if conversation_id is None:
        match = re.search(
            r'"conversation_id"\s*:\s*"([A-Za-z0-9_-]{8,128})"',
            text,
        )
        if match:
            conversation_id = match.group(1)

    return conversation_id, turn_exchange_id, topic_id, events


async def obtain_access_token(request: APIRequestContext) -> str:
    response = await request.get(
        f"{ORIGIN}/api/auth/session",
        headers={"Accept": "application/json"},
    )
    try:
        body = await response.json()
    except Exception as exc:
        raw = await response.text()
        raise SSGPTError(
            "/api/auth/session returned non-JSON "
            f"HTTP {response.status}: {raw[:500]}"
        ) from exc

    token = None
    if isinstance(body, dict):
        token = body.get("accessToken") or body.get("access_token")

    if response.status != 200 or not isinstance(token, str) or not token:
        raise SSGPTError(
            f"Could not obtain accessToken; HTTP {response.status}."
        )
    return token


class Backend:
    def __init__(
        self,
        request: APIRequestContext,
        headers: dict[str, str],
    ) -> None:
        self.request = request
        self.headers = dict(headers)
        self.token: str | None = None

    async def get(self, path: str) -> tuple[int, str]:
        if self.token is None:
            self.token = await obtain_access_token(self.request)
            log(
                "SESSION",
                f"access token refreshed in memory only; length={len(self.token)}",
            )

        headers = {
            **self.headers,
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        response = await self.request.get(
            f"{ORIGIN}{path}",
            headers=headers,
        )

        if response.status == 401:
            log("AUTH", f"HTTP 401 for {path}; refreshing session")
            self.token = await obtain_access_token(self.request)
            headers["Authorization"] = f"Bearer {self.token}"
            response = await self.request.get(
                f"{ORIGIN}{path}",
                headers=headers,
            )

        return response.status, await response.text()


async def get_json(
    backend: Backend,
    path: str,
) -> tuple[int, dict[str, Any] | None, str]:
    status, text = await backend.get(path)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    return status, value if isinstance(value, dict) else None, text


def content_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""

    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""

    output: list[str] = []
    for part in parts:
        if isinstance(part, str):
            output.append(part)
        elif isinstance(part, dict):
            for key in ("text", "content", "value"):
                candidate = part.get(key)
                if isinstance(candidate, str):
                    output.append(candidate)
                    break

    return "\n".join(output).strip()


def ordered_messages(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        return []

    output: list[dict[str, Any]] = []
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue

        copy = dict(message)
        copy["_node_id"] = str(node_id)
        output.append(copy)

    def sort_key(message: dict[str, Any]) -> tuple[float, str]:
        try:
            created = float(message.get("create_time") or 0)
        except (TypeError, ValueError):
            created = 0.0
        identity = str(
            message.get("id")
            or message.get("_node_id")
            or ""
        )
        return created, identity

    output.sort(key=sort_key)
    return output


def tool_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "invoked_plugin",
        "invoked_resource",
        "connector_tool_payload",
        "tool_name",
        "command",
        "arguments",
        "request_id",
        "turn_exchange_id",
        "working_turn_id",
    )
    return {
        key: metadata[key]
        for key in keys
        if key in metadata
    }


def belongs_to_turn(
    metadata: dict[str, Any],
    turn_exchange_id: str | None,
) -> bool:
    if not turn_exchange_id:
        return True

    message_turn = metadata.get("turn_exchange_id")
    if message_turn is None:
        return True
    return str(message_turn) == turn_exchange_id


def log_new_messages(
    conversation: dict[str, Any],
    seen_message_ids: set[str],
    *,
    turn_exchange_id: str | None,
    compact: bool,
) -> str | None:
    final_text: str | None = None

    for message in ordered_messages(conversation):
        message_id = str(
            message.get("id")
            or message.get("_node_id")
            or ""
        )
        if not message_id or message_id in seen_message_ids:
            continue

        metadata_raw = message.get("metadata")
        metadata = (
            metadata_raw
            if isinstance(metadata_raw, dict)
            else {}
        )
        if not belongs_to_turn(metadata, turn_exchange_id):
            continue

        seen_message_ids.add(message_id)

        author_raw = message.get("author")
        author = (
            str(author_raw.get("role") or "")
            if isinstance(author_raw, dict)
            else ""
        )
        recipient = str(message.get("recipient") or "all")
        content = message.get("content")
        content_type = (
            str(content.get("content_type") or "")
            if isinstance(content, dict)
            else ""
        )
        text = content_text(content)
        status = message.get("status")
        model = (
            metadata.get("model_slug")
            or metadata.get("resolved_model_slug")
        )
        effort = metadata.get("thinking_effort")

        if author == "user":
            log(
                "USER",
                f"id={message_id} status={status or '-'}",
            )
            if text:
                log("USER_TEXT", text[:2_000])
            continue

        if author == "assistant" and recipient not in {"", "all"}:
            log(
                "TOOL_CALL",
                (
                    f"id={message_id} recipient={recipient} "
                    f"model={model or '-'} effort={effort or '-'}"
                ),
            )
            details = tool_metadata(metadata)
            if details:
                log_json(
                    "TOOL_CALL_DATA",
                    details,
                    compact=compact,
                )
            if text:
                log("TOOL_CALL_TEXT", text[:4_000])
            elif isinstance(content, dict) and not compact:
                log_json(
                    "TOOL_CALL_CONTENT",
                    content,
                    compact=False,
                )
            continue

        if author == "tool":
            log(
                "TOOL_RESULT",
                f"id={message_id} status={status or '-'}",
            )
            details = tool_metadata(metadata)
            if details:
                log_json(
                    "TOOL_RESULT_DATA",
                    details,
                    compact=compact,
                )
            if text:
                log("TOOL_RESULT_TEXT", text[:8_000])
            elif isinstance(content, dict) and not compact:
                log_json(
                    "TOOL_RESULT_CONTENT",
                    content,
                    compact=False,
                )
            else:
                log(
                    "TOOL_RESULT",
                    f"content_type={content_type or '-'}",
                )
            continue

        if author == "assistant" and content_type == "thoughts":
            log(
                "REASONING",
                (
                    f"id={message_id} "
                    f"status={metadata.get('reasoning_status') or status or '-'} "
                    f"model={model or '-'} effort={effort or '-'}"
                ),
            )
            # Hidden chain-of-thought content is intentionally not printed.
            continue

        if author == "assistant" and content_type == "reasoning_recap":
            log(
                "REASONING_RECAP",
                f"id={message_id} status={status or '-'}",
            )
            if text:
                log("REASONING_RECAP_TEXT", text[:4_000])
            continue

        if (
            author == "assistant"
            and recipient in {"", "all"}
            and text
        ):
            log(
                "ASSISTANT",
                (
                    f"id={message_id} type={content_type or '-'} "
                    f"status={status or '-'} model={model or '-'} "
                    f"effort={effort or '-'}"
                ),
            )
            log("ASSISTANT_TEXT", text[:12_000])
            if content_type in {"text", "multimodal_text"}:
                final_text = text
            continue

        # System/developer/internal messages are not printed.

    return final_text


async def monitor_conversation(
    backend: Backend,
    conversation_id: str,
    *,
    turn_exchange_id: str | None,
    baseline_message_ids: set[str],
    timeout_seconds: float,
    poll_seconds: float,
    compact: bool,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    seen_message_ids = set(baseline_message_ids)
    last_stream_status: str | None = None
    last_graph_http: int | None = None
    final_text: str | None = None

    log(
        "MONITOR",
        (
            f"conversation={conversation_id} "
            f"poll={poll_seconds:g}s timeout={timeout_seconds:g}s"
        ),
    )

    while time.monotonic() < deadline:
        status_http, status_json, status_raw = await get_json(
            backend,
            f"/backend-api/conversation/{conversation_id}/stream_status",
        )
        stream_status = (
            str(status_json.get("status") or "").upper()
            if status_json
            else ""
        )

        if stream_status != last_stream_status:
            log(
                "STREAM_STATUS",
                (
                    f"http={status_http} "
                    f"status={stream_status or '<unknown>'}"
                ),
            )
            if status_json:
                log_json(
                    "STREAM_STATUS_DATA",
                    status_json,
                    compact=compact,
                )
            elif status_http not in {200, 404}:
                log("STREAM_STATUS_BODY", status_raw[:2_000])
            last_stream_status = stream_status

        graph_http, conversation, graph_raw = await get_json(
            backend,
            f"/backend-api/conversation/{conversation_id}",
        )
        if graph_http != last_graph_http:
            log("CONVERSATION_API", f"http={graph_http}")
            last_graph_http = graph_http

        if conversation:
            candidate = log_new_messages(
                conversation,
                seen_message_ids,
                turn_exchange_id=turn_exchange_id,
                compact=compact,
            )
            if candidate:
                final_text = candidate
        elif graph_http not in {200, 404}:
            log("CONVERSATION_BODY", graph_raw[:2_000])

        if stream_status == "COMPLETE":
            await asyncio.sleep(0.35)
            final_http, final_graph, final_raw = await get_json(
                backend,
                f"/backend-api/conversation/{conversation_id}",
            )
            log("FINAL_GRAPH", f"http={final_http}")

            if final_graph:
                candidate = log_new_messages(
                    final_graph,
                    seen_message_ids,
                    turn_exchange_id=turn_exchange_id,
                    compact=compact,
                )
                if candidate:
                    final_text = candidate
            elif final_http != 200:
                log("FINAL_GRAPH_BODY", final_raw[:2_000])

            if not final_text:
                raise SSGPTError(
                    "The turn is COMPLETE, but no final visible assistant "
                    "text was found for the current turn."
                )
            return final_text

        if stream_status in {
            "FAILED",
            "ERROR",
            "CANCELLED",
            "CANCELED",
        }:
            raise SSGPTError(
                f"Backend turn ended with status {stream_status}."
            )

        await asyncio.sleep(poll_seconds)

    raise SSGPTError(
        f"Timed out after {timeout_seconds:g}s waiting for "
        f"conversation {conversation_id}."
    )


async def run(args: argparse.Namespace) -> None:
    if args.timeout <= 0:
        raise SSGPTError("--timeout must be positive.")
    if args.poll <= 0:
        raise SSGPTError("--poll must be positive.")
    if args.send_timeout <= 0:
        raise SSGPTError("--send-timeout must be positive.")

    task = str(args.task or "").strip()

    if args.watch:
        if task:
            raise SSGPTError(
                "--watch does not send a task. Omit the positional task."
            )
        if args.new:
            raise SSGPTError("--watch cannot be combined with --new.")
    elif not task:
        raise SSGPTError(
            "Task is required unless --watch is used."
        )

    registry_path = store_path()
    registry = load_store(registry_path)
    target = resolve_target(args, registry)

    if args.watch and target.expected_conversation_id is None:
        raise SSGPTError(
            "--watch requires an existing conversation selected by --url, "
            "--id, or the most recently used registry entry."
        )

    log("VERSION", VERSION)
    log("TRANSPORT", "REAL_FRONTEND_SEND_NO_INTERCEPT")
    log("TRANSPORT", "request intercepts=0; aborts=0; replays=0")
    log(
        "BEHAVIOR",
        (
            "watch"
            if args.watch
            else "wait-idle-then-send"
            if args.wait_idle
            else "send"
        ),
    )
    log("CDP", args.cdp)
    log("REGISTRY", str(registry_path))
    log(
        "TARGET",
        (
            f"mode={target.mode} "
            f"id={target.registry_id or '-'} "
            f"url={target.url}"
        ),
    )

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.connect_over_cdp(
                args.cdp
            )
        except Exception as exc:
            raise SSGPTError(
                f"Could not connect to CDP endpoint {args.cdp}: {exc}"
            ) from exc

        context = await first_context(browser)
        log(
            "CDP",
            (
                f"connected; contexts={len(browser.contexts)} "
                f"existing_pages={len(context.pages)}"
            ),
        )

        backend = Backend(context.request, {})
        preferred_registry_id = target.registry_id

        # Existing conversations are checked before any helper tab is opened.
        if target.expected_conversation_id:
            conversation_id = target.expected_conversation_id
            (
                status_http,
                stream_status,
                _status_body,
                _graph_http,
                graph,
            ) = await inspect_backend_conversation(
                backend,
                conversation_id,
            )
            active = stream_is_active(status_http, stream_status)

            inferred = latest_turn_identity(graph)
            stored = stored_active_turn(
                registry,
                preferred_registry_id,
            )
            active_turn_id = (
                inferred.get("turn_exchange_id")
                or stored.get("turn_exchange_id")
            )
            active_request_id = (
                inferred.get("request_id")
                or stored.get("request_id")
            )
            active_working_turn_id = (
                inferred.get("working_turn_id")
                or stored.get("working_turn_id")
            )
            active_model = (
                inferred.get("model")
                or (
                    registry["conversations"]
                    .get(preferred_registry_id, {})
                    .get("last_model")
                    if preferred_registry_id
                    else None
                )
            )
            active_effort = (
                inferred.get("thinking_effort")
                or (
                    registry["conversations"]
                    .get(preferred_registry_id, {})
                    .get("last_thinking_effort")
                    if preferred_registry_id
                    else None
                )
            )

            if active:
                log(
                    "ACTIVE_TURN",
                    (
                        f"detected status={stream_status}; "
                        f"turn_exchange_id={active_turn_id or '<unknown>'}"
                    ),
                )

                preferred_registry_id = save_active_turn(
                    registry,
                    conversation_id=conversation_id,
                    preferred_registry_id=preferred_registry_id,
                    turn_exchange_id=active_turn_id,
                    stream_topic_id=stored.get("stream_topic_id"),
                    request_id=active_request_id,
                    working_turn_id=active_working_turn_id,
                    user_message_id=stored.get("user_message_id"),
                    model=active_model,
                    thinking_effort=active_effort,
                    source=(
                        str(stored.get("source") or "")
                        or "attached"
                    ),
                    status="RUNNING",
                    accepted_at=stored.get("accepted_at"),
                )
                save_store(registry_path, registry)
                log(
                    "REGISTRY",
                    (
                        f"active turn saved under id="
                        f"{preferred_registry_id}"
                    ),
                )

                if not args.watch and not args.wait_idle:
                    raise SSGPTError(
                        "The selected conversation is already responding. "
                        "Use --watch to only follow that response, or "
                        "--wait-idle to wait for it and then send the new task."
                    )

                try:
                    active_final = await monitor_conversation(
                        backend,
                        conversation_id,
                        turn_exchange_id=active_turn_id,
                        baseline_message_ids=set(),
                        timeout_seconds=args.timeout,
                        poll_seconds=args.poll,
                        compact=args.compact,
                    )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    mark_turn_error(
                        registry,
                        conversation_id=conversation_id,
                        registry_id=preferred_registry_id,
                        model=active_model,
                        thinking_effort=active_effort,
                        turn_exchange_id=active_turn_id,
                        error=error,
                    )
                    save_store(registry_path, registry)
                    raise

                mark_turn_complete(
                    registry,
                    conversation_id=conversation_id,
                    registry_id=preferred_registry_id,
                    model=active_model,
                    thinking_effort=active_effort,
                    turn_exchange_id=active_turn_id,
                )
                save_store(registry_path, registry)

                log(
                    "WATCH_COMPLETE",
                    (
                        f"id={preferred_registry_id} "
                        f"conversation_id={conversation_id}"
                    ),
                )
                print(
                    "\n===== WATCHED RESPONSE =====",
                    flush=True,
                )
                print(active_final, flush=True)

                if args.watch:
                    return

                log(
                    "WAIT_IDLE",
                    "previous turn completed; sending the supplied task now",
                )

            elif args.watch:
                raise SSGPTError(
                    "The selected conversation has no active response to watch "
                    f"(stream status: {stream_status or '<unknown>'})."
                )

        page: Page | None = await context.new_page()
        helper_closed = False
        saved_registry_id: str | None = preferred_registry_id
        actual_conversation_id: str | None = None
        model: str | None = None
        thinking_effort: str | None = None
        turn_exchange_id: str | None = None

        try:
            log("TAB", f"opening helper tab: {target.url}")
            await page.goto(
                target.url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await verify_browser_session(page)

            if target.expected_conversation_id:
                actual_page = urlparse(str(page.url))
                actual_parts = [
                    part
                    for part in actual_page.path.split("/")
                    if part
                ]
                actual_page_id = (
                    actual_parts[1]
                    if len(actual_parts) == 2
                    and actual_parts[0] == "c"
                    else None
                )
                if actual_page_id != target.expected_conversation_id:
                    raise SSGPTError(
                        "ChatGPT did not open the requested conversation. "
                        f"Expected {target.expected_conversation_id}, "
                        f"landed on {page.url!r}."
                    )

            baseline_message_ids = await visible_message_ids(page)
            log(
                "BASELINE",
                f"visible_message_ids={len(baseline_message_ids)}",
            )

            composer = await find_composer(page)
            tag_name = str(
                await composer.evaluate(
                    "(element) => element.tagName"
                )
            ).upper()

            if tag_name == "TEXTAREA":
                await composer.fill(task)
            else:
                await composer.click()
                await composer.fill(task)

            log(
                "COMPOSER",
                f"task inserted; characters={len(task)}",
            )

            send_button = await find_send_button(page)
            log(
                "SEND",
                "clicking the real frontend Send button",
            )

            try:
                async with page.expect_response(
                    is_real_conversation_response,
                    timeout=args.send_timeout * 1_000,
                ) as response_info:
                    await send_button.click()
                response = await response_info.value
            except PlaywrightTimeoutError as exc:
                raise SSGPTError(
                    "The frontend did not return "
                    "POST /backend-api/f/conversation within "
                    f"{args.send_timeout:g}s."
                ) from exc

            request = response.request
            payload = request_payload(request)
            model_value = payload.get("model")
            effort_value = payload.get("thinking_effort")
            model = (
                str(model_value)
                if model_value is not None
                else None
            )
            thinking_effort = (
                str(effort_value)
                if effort_value is not None
                else None
            )

            log(
                "REQUEST",
                (
                    "frontend request sent unchanged; "
                    f"model={model or '-'} "
                    f"thinking_effort={thinking_effort or '-'}"
                ),
            )
            log("REQUEST", f"url={request.url}")

            response_text = await response.text()
            log(
                "HANDOFF",
                (
                    f"http={response.status} "
                    f"content-type="
                    f"{response.headers.get('content-type') or '-'} "
                    f"bytes={len(response_text.encode('utf-8'))}"
                ),
            )

            if not response.ok:
                raise SSGPTError(
                    "The real frontend Send request failed "
                    f"HTTP {response.status}: {response_text[:3_000]}"
                )

            (
                actual_conversation_id,
                turn_exchange_id,
                topic_id,
                handoff_events,
            ) = parse_handoff(response_text)

            for event in handoff_events:
                log(
                    "HANDOFF_EVENT",
                    str(event.get("type") or "event"),
                )
                log_json(
                    "HANDOFF_EVENT_DATA",
                    event,
                    compact=args.compact,
                )

            if not actual_conversation_id:
                raise SSGPTError(
                    "The Send request succeeded, but no conversation_id "
                    f"was found. Response prefix: {response_text[:2_500]}"
                )

            actual_url = conversation_url(actual_conversation_id)
            log("CONVERSATION", f"id={actual_conversation_id}")
            log("CONVERSATION", f"url={actual_url}")
            if turn_exchange_id:
                log(
                    "CONVERSATION",
                    f"turn_exchange_id={turn_exchange_id}",
                )
            if topic_id:
                log(
                    "CONVERSATION",
                    f"stream_topic_id={topic_id}",
                )

            request_id = None
            metadata_value = payload.get("metadata")
            if isinstance(metadata_value, dict):
                request_value = metadata_value.get("request_id")
                if request_value is not None:
                    request_id = str(request_value)

            saved_registry_id = save_active_turn(
                registry,
                conversation_id=actual_conversation_id,
                preferred_registry_id=saved_registry_id,
                turn_exchange_id=turn_exchange_id,
                stream_topic_id=topic_id,
                request_id=request_id,
                working_turn_id=None,
                user_message_id=None,
                model=model,
                thinking_effort=thinking_effort,
                source="ssgpt-send",
                status="RUNNING",
            )
            save_store(registry_path, registry)
            log(
                "REGISTRY",
                (
                    f"saved active turn id={saved_registry_id} "
                    f"url={actual_url}"
                ),
            )

            backend = Backend(
                context.request,
                safe_api_headers(request),
            )

            if not args.keep_tab:
                await page.close()
                page = None
                helper_closed = True
                log(
                    "TAB",
                    "helper tab closed after durable backend handoff",
                )
            else:
                log(
                    "TAB",
                    "helper tab remains open because --keep-tab was used",
                )

            try:
                final_text = await monitor_conversation(
                    backend,
                    actual_conversation_id,
                    turn_exchange_id=turn_exchange_id,
                    baseline_message_ids=baseline_message_ids,
                    timeout_seconds=args.timeout,
                    poll_seconds=args.poll,
                    compact=args.compact,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                mark_turn_error(
                    registry,
                    conversation_id=actual_conversation_id,
                    registry_id=saved_registry_id,
                    model=model,
                    thinking_effort=thinking_effort,
                    turn_exchange_id=turn_exchange_id,
                    error=error,
                )
                save_store(registry_path, registry)
                raise

            mark_turn_complete(
                registry,
                conversation_id=actual_conversation_id,
                registry_id=saved_registry_id,
                model=model,
                thinking_effort=thinking_effort,
                turn_exchange_id=turn_exchange_id,
            )
            save_store(registry_path, registry)

            log(
                "COMPLETE",
                (
                    f"id={saved_registry_id} "
                    f"conversation_id={actual_conversation_id}"
                ),
            )
            print("\n===== FINAL RESPONSE =====", flush=True)
            print(final_text, flush=True)

        finally:
            if page is not None and not helper_closed:
                try:
                    await page.close()
                    log("TAB", "helper tab closed during cleanup")
                except Exception:
                    pass

            # Never call browser.close(). The persistent CDP browser and all
            # unrelated tabs belong to the user.


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print(
            "\nssgpt: interrupted",
            file=sys.stderr,
            flush=True,
        )
        return 130
    except Exception as exc:
        print(
            f"\nssgpt: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())