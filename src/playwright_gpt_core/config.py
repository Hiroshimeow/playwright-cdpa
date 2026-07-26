from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

from .errors import InvalidInputError

_DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _default_coordination_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            base = candidate
        else:
            home = Path.home()
            if not home.is_absolute():
                raise InvalidInputError("home directory must be absolute")
            base = home / ".local" / "state"
    else:
        home = Path.home()
        if not home.is_absolute():
            raise InvalidInputError("home directory must be absolute")
        base = home / ".local" / "state"
    return (base / "playwright-gpt-core" / "coordination").resolve()


def _absolute_coordination_dir(value: Path | None) -> Path:
    if value is None:
        return _default_coordination_dir()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise InvalidInputError("coordination_dir must be an absolute path")
    return candidate.resolve()


@dataclass(frozen=True, slots=True)
class CoreConfig:
    cdp_endpoint: str = "http://127.0.0.1:9222"
    state_dir: Path = Path(".playwright-gpt")
    coordination_dir: Path | None = None
    deployment_id: str | None = None
    timeout: float = 1800.0
    poll: float = 1.0
    send_timeout: float = 90.0
    identity_timeout: float = 30.0
    stable_samples: int = 2
    stable_seconds: float = 0.5
    keep_helper_tab: bool = False

    @property
    def normalized_cdp_endpoint(self) -> str:
        parsed = urlparse(self.cdp_endpoint)
        scheme = parsed.scheme.casefold()
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise InvalidInputError("CDP endpoint has an invalid port") from exc
        return f"{scheme}://loopback:{port}"

    @property
    def coordination_root(self) -> Path:
        base = _absolute_coordination_dir(self.coordination_dir)
        deployment = self.deployment_id
        if deployment is None:
            digest = hashlib.sha256(self.normalized_cdp_endpoint.encode("utf-8")).hexdigest()[
                :24
            ]
            deployment = f"cdp-{digest}"
        return base / deployment

    def validated(self) -> CoreConfig:
        parsed = urlparse(self.cdp_endpoint)
        if parsed.scheme not in {"http", "https"}:
            raise InvalidInputError("CDP endpoint must use http or https")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise InvalidInputError("CDP endpoint must be loopback")
        if parsed.username is not None or parsed.password is not None:
            raise InvalidInputError("CDP endpoint must not contain credentials")
        if parsed.query or parsed.fragment:
            raise InvalidInputError("CDP endpoint must not contain query or fragment data")
        if parsed.path not in {"", "/"}:
            raise InvalidInputError("CDP endpoint path must be empty")
        _ = self.normalized_cdp_endpoint
        if self.deployment_id is not None and not _DEPLOYMENT_ID.fullmatch(self.deployment_id):
            raise InvalidInputError("deployment_id must contain 1 to 80 path-safe characters")
        if self.timeout <= 0 or self.poll <= 0 or self.send_timeout <= 0:
            raise InvalidInputError("timeouts and poll interval must be positive")
        if self.identity_timeout <= 0:
            raise InvalidInputError("identity timeout must be positive")
        if self.stable_samples < 2 or self.stable_seconds < 0:
            raise InvalidInputError("graph convergence requires at least two samples")
        return replace(
            self,
            coordination_dir=_absolute_coordination_dir(self.coordination_dir),
        )
