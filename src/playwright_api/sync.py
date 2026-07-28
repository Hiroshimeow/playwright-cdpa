from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .attachments import AttachmentInput
from .config import ClientConfig
from .models import Result
from .projects import ProjectMemoryScope, ProjectRef
from .service import ChatGPTClient
from .targets import ChatTarget


class SyncChatGPTClient:
    """Synchronous facade over one authoritative async client."""

    def __init__(self, config: ClientConfig | None = None) -> None:
        self._client = ChatGPTClient(config)

    def send(
        self,
        prompt: str,
        *,
        request_id: str | None = None,
        target: ChatTarget | None = None,
        attachments: Sequence[AttachmentInput] = (),
    ) -> Result:
        return self._run(
            self._client.send(
                prompt,
                request_id=request_id,
                target=target,
                attachments=attachments,
            )
        )

    def get(self, request_id: str) -> Result:
        return self._run(self._client.get(request_id))

    def status(self, request_id: str) -> Result:
        self._ensure_no_running_loop()
        return self._client.status(request_id)

    def cancel(self, request_id: str) -> Result:
        return self._run(self._client.cancel(request_id))

    def find_project(
        self, *, project_id: str | None = None, name: str | None = None
    ) -> ProjectRef | None:
        return self._run(self._client.find_project(project_id=project_id, name=name))

    def ensure_project(
        self,
        key: str,
        name: str,
        memory_scope: ProjectMemoryScope = ProjectMemoryScope.DEFAULT,
    ) -> ProjectRef:
        return self._run(self._client.ensure_project(key, name, memory_scope))

    def open_project(self, project: ProjectRef) -> ChatTarget:
        return self._run(self._client.open_project(project))

    @staticmethod
    def _ensure_no_running_loop() -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError(
            "SyncChatGPTClient cannot be used from a running event loop; use ChatGPTClient"
        )

    @classmethod
    def _run(cls, awaitable):
        cls._ensure_no_running_loop()
        return asyncio.run(awaitable)
