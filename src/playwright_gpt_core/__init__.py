"""Stable, fail-closed ChatGPT Web execution core."""

from .api import ChatGPTCore, CoreConfig
from .models import Result, TurnIdentity, TurnRecord, TurnState

__all__ = [
    "ChatGPTCore",
    "CoreConfig",
    "Result",
    "TurnIdentity",
    "TurnRecord",
    "TurnState",
]
__version__ = "0.1.0"
