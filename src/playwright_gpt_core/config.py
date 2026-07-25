from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .errors import InvalidInputError


@dataclass(frozen=True, slots=True)
class CoreConfig:
    cdp_endpoint: str = "http://127.0.0.1:9222"
    state_dir: Path = Path(".playwright-gpt")
    timeout: float = 1800.0
    poll: float = 1.0
    send_timeout: float = 90.0
    identity_timeout: float = 30.0
    stable_samples: int = 2
    stable_seconds: float = 0.5
    keep_helper_tab: bool = False

    def validated(self) -> CoreConfig:
        parsed = urlparse(self.cdp_endpoint)
        if parsed.scheme not in {"http", "https"}:
            raise InvalidInputError("CDP endpoint must use http or https")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise InvalidInputError("CDP endpoint must be loopback")
        if self.timeout <= 0 or self.poll <= 0 or self.send_timeout <= 0:
            raise InvalidInputError("timeouts and poll interval must be positive")
        if self.identity_timeout <= 0:
            raise InvalidInputError("identity timeout must be positive")
        if self.stable_samples < 2 or self.stable_seconds < 0:
            raise InvalidInputError("graph convergence requires at least two samples")
        return self
