"""Installable, fail-closed ChatGPT Web SDK for persistent CDP Chromium."""

from importlib.metadata import PackageNotFoundError, version

from .attachments import AttachmentInput
from .config import ClientConfig
from .errors import Disposition, Failure, FailureCategory, FailureCode
from .models import Result, TurnIdentity, TurnState
from .projects import ProjectMemoryScope, ProjectRef
from .service import ChatGPTClient
from .sync import SyncChatGPTClient
from .targets import ChatTarget, TargetKind

__all__ = [
    "AttachmentInput",
    "ChatGPTClient",
    "ChatTarget",
    "ClientConfig",
    "Disposition",
    "Failure",
    "FailureCategory",
    "FailureCode",
    "ProjectMemoryScope",
    "ProjectRef",
    "Result",
    "SyncChatGPTClient",
    "TargetKind",
    "TurnIdentity",
    "TurnState",
]

try:
    __version__ = version("playwright-api")
except PackageNotFoundError:  # source checkout without installed metadata
    __version__ = "0+unknown"
