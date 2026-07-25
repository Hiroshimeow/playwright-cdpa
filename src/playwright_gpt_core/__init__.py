"""Stable, fail-closed ChatGPT Web execution core."""

from .models import Result, TurnIdentity, TurnRecord, TurnState

__all__ = ["Result", "TurnIdentity", "TurnRecord", "TurnState"]
__version__ = "0.1.0"
