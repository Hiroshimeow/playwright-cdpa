from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from .errors import InvalidInputError

ORIGIN = "https://chatgpt.com"
_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{8,160}$")
_PROJECT_ID = re.compile(r"^g-p-[A-Za-z0-9_-]{8,160}$")
_REAL_PROJECT_ROUTE = re.compile(
    r"^(g-p-[0-9a-f]{32})(?:-[A-Za-z0-9][A-Za-z0-9_-]{0,127})?$",
    re.IGNORECASE,
)


class TargetKind(str, Enum):
    FRESH = "fresh"
    CONVERSATION = "conversation"
    PROJECT = "project"
    PROJECT_CONVERSATION = "project_conversation"


@dataclass(frozen=True, slots=True)
class ChatTarget:
    kind: TargetKind
    project_id: str | None
    conversation_id: str | None
    canonical_url: str

    @classmethod
    def parse(cls, value: str) -> ChatTarget:
        if type(value) is not str:
            raise InvalidInputError("target must be exactly a string")
        raw = value.strip()
        if not raw:
            raise InvalidInputError("target is empty")
        if "://" in raw:
            parsed = urlparse(raw)
            try:
                port = parsed.port
            except ValueError as exc:
                raise InvalidInputError("target URL has an invalid port") from exc
            if (
                parsed.scheme != "https"
                or (parsed.hostname or "").casefold() not in {"chatgpt.com", "www.chatgpt.com"}
                or parsed.username is not None
                or parsed.password is not None
                or port not in {None, 443}
            ):
                raise InvalidInputError("target URL must use exact HTTPS ChatGPT origin")
            if parsed.params or parsed.query or parsed.fragment:
                raise InvalidInputError("target URL must not contain params, query, or fragment")
            path = parsed.path
        else:
            path = raw
        if path == "/":
            return cls(TargetKind.FRESH, None, None, f"{ORIGIN}/")
        match = re.fullmatch(r"/c/([A-Za-z0-9_-]{8,160})", path)
        if match:
            return cls.conversation(match.group(1))
        match = re.fullmatch(r"/g/(g-p-[A-Za-z0-9_-]{8,160})/project", path)
        if match:
            return cls.project(match.group(1))
        match = re.fullmatch(
            r"/g/(g-p-[A-Za-z0-9_-]{8,160})/c/([A-Za-z0-9_-]{8,160})",
            path,
        )
        if match:
            return cls.project_conversation(match.group(1), match.group(2))
        raise InvalidInputError("unsupported or malformed ChatGPT target")

    @classmethod
    def fresh(cls) -> ChatTarget:
        return cls(TargetKind.FRESH, None, None, f"{ORIGIN}/")

    @classmethod
    def conversation(cls, conversation_id: str) -> ChatTarget:
        conversation_id = _validate_conversation_id(conversation_id)
        return cls(
            TargetKind.CONVERSATION,
            None,
            conversation_id,
            f"{ORIGIN}/c/{conversation_id}",
        )

    @classmethod
    def project(cls, project_id: str) -> ChatTarget:
        project_id = _validate_project_id(project_id)
        return cls(
            TargetKind.PROJECT,
            project_id,
            None,
            f"{ORIGIN}/g/{project_id}/project",
        )

    @classmethod
    def project_conversation(
        cls, project_id: str, conversation_id: str
    ) -> ChatTarget:
        project_id = _validate_project_id(project_id)
        conversation_id = _validate_conversation_id(conversation_id)
        return cls(
            TargetKind.PROJECT_CONVERSATION,
            project_id,
            conversation_id,
            f"{ORIGIN}/g/{project_id}/c/{conversation_id}",
        )

    @property
    def coordination_key(self) -> str:
        if self.kind is TargetKind.FRESH:
            return "fresh"
        if self.kind is TargetKind.CONVERSATION:
            return f"conversation:{self.conversation_id}"
        if self.kind is TargetKind.PROJECT:
            return f"project:{self.project_id}:root"
        return f"project:{self.project_id}:conversation:{self.conversation_id}"


def _validate_conversation_id(value: str) -> str:
    if type(value) is not str or not _CONVERSATION_ID.fullmatch(value):
        raise InvalidInputError("invalid conversation ID")
    return value


def _validate_project_id(value: str) -> str:
    if type(value) is not str or not _PROJECT_ID.fullmatch(value):
        raise InvalidInputError("invalid project ID")
    real_route = _REAL_PROJECT_ROUTE.fullmatch(value)
    if real_route is not None:
        return real_route.group(1).lower()
    return value


def normalize_conversation(value: str) -> str:
    """Internal compatibility helper for ordinary conversations."""
    if type(value) is str and "://" not in value and not value.startswith("/"):
        return _validate_conversation_id(value.strip("/"))
    target = ChatTarget.parse(value)
    if target.kind is not TargetKind.CONVERSATION or target.conversation_id is None:
        raise InvalidInputError("target is not an ordinary conversation")
    return target.conversation_id


def conversation_url(conversation_id: str) -> str:
    return ChatTarget.conversation(conversation_id).canonical_url
