from __future__ import annotations

import pytest

from playwright_api.backend import AuthenticatedBackend


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


class UnavailableRequest(Request):
    async def get(self, url: str, headers: dict[str, str]):
        self.calls.append((url, headers))
        if url.endswith("/api/auth/session"):
            return Response(200, {"accessToken": "token-secret"})
        return Response(503, {"error": "temporary"})


class MaterializingRequest(Request):
    async def get(self, url: str, headers: dict[str, str]):
        self.calls.append((url, headers))
        if url.endswith("/api/auth/session"):
            return Response(200, {"accessToken": "token-secret"})
        if url.endswith("/stream_status"):
            return Response(200, {"status": "COMPLETE"})
        return Response(404, {"detail": "not materialized"})


@pytest.mark.asyncio
async def test_snapshot_treats_not_yet_materialized_graph_as_absent() -> None:
    context = Context()
    context.request = MaterializingRequest()
    backend = AuthenticatedBackend(context)  # type: ignore[arg-type]

    snapshot = await backend.snapshot("conv-1")

    assert snapshot.stream_status == "COMPLETE"
    assert snapshot.graph is None


@pytest.mark.asyncio
async def test_backend_5xx_is_external_and_retryable() -> None:
    from playwright_api.errors import BackendUnavailableError

    context = Context()
    context.request = UnavailableRequest()
    backend = AuthenticatedBackend(context)  # type: ignore[arg-type]
    with pytest.raises(BackendUnavailableError) as caught:
        await backend.get_json("/backend-api/conversation/test")
    failure = caught.value.as_failure()
    assert failure.external is True
    assert failure.retryable is True
