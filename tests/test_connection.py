from __future__ import annotations

import pytest

from playwright_api.connection import close_page_by_target_id, page_target_id


class FakePage:
    def __init__(self, target_id: str) -> None:
        self.target_id = target_id
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


class FakeCDPSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.detached = False

    async def send(self, method: str):
        assert method == "Target.getTargetInfo"
        return {"targetInfo": {"targetId": self.page.target_id, "type": "page"}}

    async def detach(self) -> None:
        self.detached = True


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages
        self.sessions: list[FakeCDPSession] = []

    async def new_cdp_session(self, page: FakePage) -> FakeCDPSession:
        session = FakeCDPSession(page)
        self.sessions.append(session)
        return session


@pytest.mark.asyncio
async def test_page_target_id_is_exact_and_content_free() -> None:
    page = FakePage("target-exact")
    context = FakeContext([page])
    assert await page_target_id(context, page) == "target-exact"  # type: ignore[arg-type]
    assert context.sessions[0].detached is True


@pytest.mark.asyncio
async def test_close_page_by_target_id_never_closes_url_peers() -> None:
    owned = FakePage("owned-target")
    unrelated = FakePage("unrelated-target")
    context = FakeContext([unrelated, owned])

    closed = await close_page_by_target_id(  # type: ignore[arg-type]
        context, "owned-target"
    )

    assert closed is True
    assert owned.closed is True
    assert unrelated.closed is False
