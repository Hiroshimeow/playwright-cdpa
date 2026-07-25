from __future__ import annotations

import json

from playwright_gpt_core.cli import (
    EXIT_AMBIGUOUS,
    EXIT_CANCELLED,
    EXIT_OWNERSHIP,
    build_parser,
    exit_code,
    main,
)
from playwright_gpt_core.errors import Failure, FailureCategory
from playwright_gpt_core.models import Result, TurnRecord, TurnState
from playwright_gpt_core.storage import StateStore


def test_cli_acceptance_surface_requires_explicit_target() -> None:
    parser = build_parser()
    args = parser.parse_args(["send", "Reply OK", "--fresh", "--json"])
    assert args.command == "send"
    assert args.fresh is True
    args = parser.parse_args(["watch", "req-1", "--json"])
    assert args.command == "watch"
    args = parser.parse_args(
        ["send", "next", "--conversation", "conversation-1", "--wait-idle"]
    )
    assert args.wait_idle is True


def test_stable_exit_codes() -> None:
    assert exit_code(Result(1, "r", TurnState.CANCELLED), command="cancel") == EXIT_CANCELLED
    assert exit_code(Result(1, "r", TurnState.COMPLETE), command="cancel") == 0
    assert (
        exit_code(
            Result(
                1,
                "r",
                TurnState.FAILED,
                failure=Failure(FailureCategory.OWNERSHIP, "busy", True),
            ),
            command="send",
        )
        == EXIT_OWNERSHIP
    )
    assert (
        exit_code(
            Result(
                1,
                "r",
                TurnState.UNKNOWN,
                failure=Failure(FailureCategory.AMBIGUOUS_OUTCOME, "unknown", True),
            ),
            command="send",
        )
        == EXIT_AMBIGUOUS
    )


def test_get_json_outputs_one_machine_readable_object(tmp_path, capsys) -> None:
    store = StateStore(tmp_path)
    store.save(TurnRecord.new(request_id="req-1", prompt="secret prompt"))
    code = main(["get", "req-1", "--state-dir", str(tmp_path), "--json"])
    output = capsys.readouterr().out.strip()
    assert code == 0
    value = json.loads(output)
    assert value["request_id"] == "req-1"
    assert "secret prompt" not in output


def test_missing_get_returns_invalid_exit_and_json_failure(tmp_path, capsys) -> None:
    code = main(["get", "missing", "--state-dir", str(tmp_path), "--json"])
    value = json.loads(capsys.readouterr().out)
    assert code == 2
    assert value["failure"]["category"] == "invalid_input"


def test_recoverable_backend_failure_maps_to_exit_ten() -> None:
    result = Result(
        1,
        "r",
        TurnState.UNKNOWN,
        failure=Failure(
            FailureCategory.BACKEND,
            "temporary backend outage",
            retryable=True,
            external=True,
        ),
    )
    assert exit_code(result, command="watch") == 10
