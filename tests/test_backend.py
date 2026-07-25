from __future__ import annotations

import pytest

from playwright_gpt_core.backend import AuthenticatedBackend


class Response:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.body = body

    async def json(self):
        return self.body


class Request:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, headers: dict[str, str]):
        self.calls.append((url, headers))
        if url.endswith("/api/auth/session"):
            return Response(200, {"accessToken": "token-secret"})
        if url.endswith("/stream_status"):
            return Response(200, {"status": "COMPLETE"})
        return Response(200, {"mapping": {}, "current_node": "x"})


class Context:
    def __init__(self) -> None:
        self.request = Request()


@pytest.mark.asyncio
async def test_backend_uses_token_in_memory_and_never_exposes_it() -> None:
    context = Context()
    backend = AuthenticatedBackend(context)  # type: ignore[arg-type]
    snapshot = await backend.snapshot("conv-1")
    assert snapshot.stream_status == "COMPLETE"
    assert "token-secret" not in repr(snapshot)
    assert backend.__dict__["_token"] == "token-secret"
    assert all("Cookie" not in headers for _url, headers in context.request.calls)
