from __future__ import annotations

from types import SimpleNamespace

from playwright_api import ChatTarget

from flow import parse_handoff, run_flow


def _result(response: str, conversation_id: str):
    return SimpleNamespace(
        success=True,
        disposition="complete",
        response=response,
        request_id="request",
        identity=SimpleNamespace(conversation_id=conversation_id),
    )


class FakeClient:
    def __init__(self) -> None:
        self.calls = []
        self.project_calls = []
        self.results = iter(
            [
                _result(
                    'Plan ready.\n{"route":"REVIEW","report_path":".plan/three-agent/plan.md"}',
                    "plan-conversation",
                ),
                _result(
                    'Reviewed.\n{"route":"DEV","report_path":".plan/three-agent/review.md"}',
                    "review-conversation",
                ),
                _result(
                    'Implemented.\n{"route":"PLAN","report_path":".plan/three-agent/dev.md"}',
                    "dev-conversation",
                ),
                _result(
                    'Verified.\n{"route":"DONE","report_path":".plan/three-agent/final.md"}',
                    "plan-conversation",
                ),
            ]
        )

    def ensure_project(self, *, key, name):
        self.project_calls.append((key, name))
        return SimpleNamespace(
            project_id="g-p-project123",
            canonical_url="https://chatgpt.com/g/g-p-project123/project",
        )

    def open_project(self, project):
        return ChatTarget.project(project.project_id)

    def send(self, prompt, *, request_id, target):
        self.calls.append((prompt, request_id, target))
        return next(self.results)

    def get(self, request_id):  # pragma: no cover - this scenario completes inline
        raise AssertionError(f"unexpected get: {request_id}")


def test_parse_handoff_reads_final_compact_json() -> None:
    assert parse_handoff(
        'summary\n{"route":"DEV","report_path":".plan/team/review.md"}'
    ) == ("DEV", ".plan/team/review.md")


def test_run_flow_reuses_each_role_conversation_and_stops_on_plan_done() -> None:
    client = FakeClient()

    final_report = run_flow(
        task="Implement the agreed change",
        url_id="6a6901ff-0178-83ee-9c73-9be41199b864",
        client=client,
        sleeper=lambda _seconds: None,
        run_id="test-run",
        output=lambda _text: None,
    )

    assert final_report == ".plan/three-agent/final.md"
    assert [target.canonical_url for _, _, target in client.calls] == [
        ChatTarget.conversation("6a6901ff-0178-83ee-9c73-9be41199b864").canonical_url,
        ChatTarget.fresh().canonical_url,
        ChatTarget.fresh().canonical_url,
        ChatTarget.conversation("6a6901ff-0178-83ee-9c73-9be41199b864").canonical_url,
    ]
    assert [request_id for _, request_id, _ in client.calls] == [
        "test-run-plan-001",
        "test-run-review-001",
        "test-run-dev-001",
        "test-run-plan-002",
    ]
    assert "Source report: .plan/three-agent/plan.md" in client.calls[1][0]
    assert "Source report: .plan/three-agent/review.md" in client.calls[2][0]
    assert "Source report: .plan/three-agent/dev.md" in client.calls[3][0]


def test_project_flag_creates_one_project_and_keeps_role_chats_inside_it() -> None:
    client = FakeClient()

    run_flow(
        task="Implement the agreed change",
        project_name="abc",
        client=client,
        sleeper=lambda _seconds: None,
        run_id="project-run",
        output=lambda _text: None,
    )

    assert len(client.project_calls) == 1
    assert client.project_calls[0][1] == "abc"
    assert [target.canonical_url for _, _, target in client.calls] == [
        "https://chatgpt.com/g/g-p-project123/project",
        "https://chatgpt.com/g/g-p-project123/project",
        "https://chatgpt.com/g/g-p-project123/project",
        "https://chatgpt.com/g/g-p-project123/c/plan-conversation",
    ]
