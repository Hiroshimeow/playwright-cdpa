from __future__ import annotations

from typing import Any

from playwright.async_api import APIRequestContext, BrowserContext

from .errors import (
    AuthenticationRequiredError,
    BackendError,
    BackendUnavailableError,
    NetworkError,
    SchemaDriftError,
)
from .monitor import MonitorSnapshot, SnapshotSource

ORIGIN = "https://chatgpt.com"


class AuthenticatedBackend(SnapshotSource):
    def __init__(self, context: BrowserContext) -> None:
        self.request: APIRequestContext = context.request
        self._token: str | None = None

    async def snapshot(self, conversation_id: str) -> MonitorSnapshot:
        status_value = await self.get_json(
            f"/backend-api/conversation/{conversation_id}/stream_status",
            allow_not_found=True,
        )
        graph = await self.get_json(f"/backend-api/conversation/{conversation_id}")
        status = str(status_value.get("status") or "").upper() if status_value else ""
        if not status:
            raise SchemaDriftError("stream_status response has no status")
        return MonitorSnapshot(stream_status=status, graph=graph)

    async def get_json(
        self, path: str, *, allow_not_found: bool = False
    ) -> dict[str, Any] | None:
        token = await self._access_token()
        response = await self._get(path, token)
        if response.status == 401:
            self._token = None
            token = await self._access_token()
            response = await self._get(path, token)
        if allow_not_found and response.status == 404:
            return {"status": "NOT_FOUND"}
        if response.status == 401:
            raise AuthenticationRequiredError("backend rejected authenticated browser session")
        if response.status >= 500 or response.status == 429:
            raise BackendUnavailableError(
                f"backend GET failed with HTTP {response.status}"
            )
        if response.status != 200:
            raise BackendError(f"backend GET returned HTTP {response.status}")
        try:
            value = await response.json()
        except Exception as exc:
            raise SchemaDriftError("backend GET returned non-JSON data") from exc
        if not isinstance(value, dict):
            raise SchemaDriftError("backend GET JSON root is not an object")
        return value

    async def _access_token(self) -> str:
        if self._token is not None:
            return self._token
        try:
            response = await self.request.get(
                f"{ORIGIN}/api/auth/session", headers={"Accept": "application/json"}
            )
        except Exception as exc:
            raise NetworkError("could not read authenticated browser session") from exc
        if response.status != 200:
            raise AuthenticationRequiredError(
                f"browser session endpoint returned HTTP {response.status}"
            )
        try:
            body = await response.json()
        except Exception as exc:
            raise SchemaDriftError("browser session endpoint returned non-JSON data") from exc
        token = None
        if isinstance(body, dict):
            token = body.get("accessToken") or body.get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthenticationRequiredError("browser session has no access token")
        self._token = token
        return token

    async def _get(self, path: str, token: str) -> Any:
        try:
            return await self.request.get(
                f"{ORIGIN}{path}",
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            )
        except Exception as exc:
            raise NetworkError("backend GET failed") from exc
