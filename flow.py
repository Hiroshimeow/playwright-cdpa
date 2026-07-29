from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Callable

from playwright_api import ChatTarget, SyncChatGPTClient, TargetKind

STEP_SLEEP_SECONDS = 2.0
_ALLOWED_ROUTES = {
    "PLAN": {"REVIEW", "DONE"},
    "REVIEW": {"DEV"},
    "DEV": {"PLAN"},
}


def parse_handoff(response: str) -> tuple[str, str]:
    for line in reversed(response.splitlines()):
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and set(value) == {"route", "report_path"}:
            route = value["route"]
            report_path = value["report_path"]
            if (
                isinstance(route, str)
                and isinstance(report_path, str)
                and report_path.strip()
            ):
                return route.upper(), report_path.strip()
        break
    raise RuntimeError(
        'role response must end with {"route":"...","report_path":"..."}'
    )


def _role_prompt(
    role: str,
    task: str,
    turn: int,
    run_id: str,
    source_report: str | None,
) -> str:
    report_path = f".plan/three-agent/{role.lower()}_turn{turn}_{run_id}.md"
    role_instruction = {
        "PLAN": (
            "Inspect the target repository and the source report. Do not implement product code. "
            "If the goal is completely implemented and verified, route DONE. Otherwise produce "
            "the smallest actionable plan and route REVIEW."
        ),
        "REVIEW": (
            "Read the PLAN report and independently inspect the target repository. Review the "
            "plan for correctness, missing risks, and unnecessary scope. Do not implement. "
            "Write exact guidance for DEV and route DEV."
        ),
        "DEV": (
            "Read the REVIEW report. Implement the smallest complete change in the target "
            "repository, run relevant checks, record exact evidence, and route PLAN."
        ),
    }[role]
    source = source_report or "none; this is the first PLAN turn"
    return f"""You are the {role} role in a thin PLAN -> REVIEW -> DEV workflow.

GOAL
{task}

Source report: {source}

{role_instruction}

Report contract:
- Work in the repository required by GOAL.
- Write the complete Markdown report inside that repository at exactly:
  {report_path}
- Create the .plan/three-agent directory when missing.
- Include facts, actions or findings, verification evidence, and what the next role must do.
- Do not place the report in another workspace.

Response contract:
- End with exactly one compact JSON object on one line and nothing after it.
- Use the exact report path above.
- Allowed route for this role: {", ".join(sorted(_ALLOWED_ROUTES[role]))}
- Format: {{"route":"ROUTE","report_path":"{report_path}"}}
"""


def _initial_target(url_id: str | None) -> ChatTarget:
    if not url_id:
        return ChatTarget.fresh()
    value = url_id.strip()
    if "://" in value or value.startswith("/"):
        return ChatTarget.parse(value)
    return ChatTarget.conversation(value)


def _continue_target(target: ChatTarget, conversation_id: str) -> ChatTarget:
    if target.kind is TargetKind.FRESH:
        return ChatTarget.conversation(conversation_id)
    if target.kind is TargetKind.PROJECT:
        assert target.project_id is not None
        return ChatTarget.project_conversation(target.project_id, conversation_id)
    return target


def _complete_request(client, prompt: str, request_id: str, target: ChatTarget, sleeper):
    result = client.send(prompt, request_id=request_id, target=target)
    while not result.success and result.disposition == "get_required":
        sleeper(STEP_SLEEP_SECONDS)
        result = client.get(request_id)
    if not result.success:
        raise RuntimeError(f"request {request_id} failed: {result.failure}")
    if result.response is None or result.identity is None:
        raise RuntimeError(f"request {request_id} completed without durable response identity")
    if not result.identity.conversation_id:
        raise RuntimeError(f"request {request_id} has no conversation_id")
    return result


def run_flow(
    task: str,
    url_id: str | None = None,
    project_name: str | None = None,
    *,
    client=None,
    sleeper: Callable[[float], None] = time.sleep,
    run_id: str | None = None,
    output: Callable[[str], None] = print,
) -> str:
    if not task.strip():
        raise ValueError("--task must not be empty")

    client = client or SyncChatGPTClient()
    run_id = run_id or (
        datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )

    fresh_target = ChatTarget.fresh()
    if project_name:
        project_key = "three-agent:" + hashlib.sha256(project_name.encode()).hexdigest()[:24]
        project = client.ensure_project(key=project_key, name=project_name)
        fresh_target = client.open_project(project)
        output(f"Project: {project.canonical_url}")
        sleeper(STEP_SLEEP_SECONDS)

    targets = {
        "PLAN": _initial_target(url_id) if url_id else fresh_target,
        "REVIEW": fresh_target,
        "DEV": fresh_target,
    }
    turns = {"PLAN": 0, "REVIEW": 0, "DEV": 0}
    role = "PLAN"
    source_report: str | None = None

    while True:
        turns[role] += 1
        turn = turns[role]
        request_id = f"{run_id}-{role.lower()}-{turn:03d}"
        prompt = _role_prompt(role, task, turn, run_id, source_report)
        output(f"\n[{role} turn {turn}] {targets[role].canonical_url}")
        result = _complete_request(client, prompt, request_id, targets[role], sleeper)
        output(result.response)

        targets[role] = _continue_target(
            targets[role], result.identity.conversation_id
        )
        route, report_path = parse_handoff(result.response)
        if route not in _ALLOWED_ROUTES[role]:
            raise RuntimeError(f"{role} cannot route to {route}")
        if route == "DONE":
            output(f"\nDONE: {report_path}")
            return report_path

        source_report = report_path
        role = route
        sleeper(STEP_SLEEP_SECONDS)
