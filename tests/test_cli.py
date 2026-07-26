from __future__ import annotations

import json

from playwright_gpt_core.cli import (
    EXIT_AMBIGUOUS,
    EXIT_CANCELLED,
    EXIT_OWNERSHIP,
    _error_result,
    build_parser,
    exit_code,
    main,
)
from playwright_gpt_core.errors import Failure, FailureCategory
from playwright_gpt_core.models import Result, TurnIdentity, TurnRecord, TurnState
from playwright_gpt_core.storage import StateStore


def test_cli_acceptance_surface_requires_explicit_target() -> None:
    parser = build_parser()
    args = parser.parse_args(["send", "Reply OK", "--fresh", "--json"])
    assert args.command == "send"
    assert args.fresh is True
    args = parser.parse_args(["watch", "req-1", "--json"])
    assert args.command == "watch"
    args = parser.parse_args(
        [
            "send",
            "next",
            "--conversation",
            "conversation-1",
            "--wait-idle",
            "--coordination-dir",
            "/tmp/shared-coordination",
            "--deployment-id",
            "profile-9222",
        ]
    )
    assert args.wait_idle is True
    assert args.coordination_dir == "/tmp/shared-coordination"
    assert args.deployment_id == "profile-9222"


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


def test_unexpected_exception_diagnostic_does_not_echo_credentials() -> None:
    result = _error_result(RuntimeError("resume_conversation_token=do-not-print"))

    assert result.failure is not None
    assert "do-not-print" not in result.failure.message
    assert "RuntimeError" in result.failure.message


def test_unexpected_exception_json_and_stderr_do_not_echo_credentials(
    monkeypatch, capsys
) -> None:
    import playwright_gpt_core.cli as cli_module

    async def fail(_args):
        raise RuntimeError("Authorization: Basic do-not-print")

    monkeypatch.setattr(cli_module, "_run", fail)

    code = main(["get", "req-1", "--json"])
    captured = capsys.readouterr()

    assert code == 20
    assert "do-not-print" not in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


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


def test_get_rejects_string_failure_flags_as_corrupt_state(tmp_path, capsys) -> None:
    store = StateStore(tmp_path)
    value = (
        TurnRecord.new(request_id="corrupt-failure", prompt="prompt")
        .transition(
            TurnState.FAILED,
            failure=Failure(
                FailureCategory.TIMEOUT,
                "timeout",
                retryable=False,
                external=False,
            ),
        )
        .to_dict()
    )
    value["failure"]["retryable"] = "false"
    path = store.turn_path("corrupt-failure")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")

    code = main(["get", "corrupt-failure", "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert code == 20
    assert result["failure"]["category"] == "corrupt_state"
    assert captured.err == ""


def test_unexpected_exception_url_and_proof_values_are_not_printed(monkeypatch, capsys) -> None:
    import playwright_gpt_core.cli as cli_module

    async def fail(_args):
        raise RuntimeError(
            "GET https://example.test/api/session_token/URL-SECRET failed; "
            "proof_material=PROOF-SECRET"
        )

    monkeypatch.setattr(cli_module, "_run", fail)

    code = main(["get", "req-1", "--json"])
    captured = capsys.readouterr()

    assert code == 20
    assert "URL-SECRET" not in captured.out
    assert "PROOF-SECRET" not in captured.out
    assert captured.err == ""


def test_get_rejects_turn_file_payload_identity_mismatch(tmp_path, capsys) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="different-id", prompt="prompt").to_dict()
    path = store.turn_path("requested-id")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")

    code = main(["get", "requested-id", "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert code == 20
    assert result["failure"]["category"] == "corrupt_state"
    assert result["request_id"] == "-"
    assert path.read_text(encoding="utf-8") == raw
    assert captured.err == ""


def test_unexpected_exception_generic_secret_assignments_are_not_printed(
    monkeypatch, capsys
) -> None:
    import playwright_gpt_core.cli as cli_module

    async def fail(_args):
        raise RuntimeError(
            "client_secret=CLIENT-SECRET; db_password=DB-SECRET; "
            "GET https://example.test/api/token-URL-SECRET failed"
        )

    monkeypatch.setattr(cli_module, "_run", fail)

    code = main(["get", "req-1", "--json"])
    captured = capsys.readouterr()

    assert code == 20
    assert "CLIENT-SECRET" not in captured.out
    assert "DB-SECRET" not in captured.out
    assert "URL-SECRET" not in captured.out
    assert captured.err == ""


def test_get_json_does_not_print_common_credentials_or_private_keys(tmp_path, capsys) -> None:
    store = StateStore(tmp_path)
    message = (
        "client_credentials=CLI-CREDENTIALS-VALUE; "
        "private_key=CLI-PRIVATE-KEY-VALUE; "
        "GET https://example.test/api/signingKey-CLI-SIGNING-PATH-VALUE/tail failed"
    )
    record = TurnRecord.new(request_id="cli-credential-key", prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", record.request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    assert "CLI-CREDENTIALS-VALUE" not in captured.out
    assert "CLI-PRIVATE-KEY-VALUE" not in captured.out
    assert "CLI-SIGNING-PATH-VALUE" not in captured.out
    assert "<redacted>" in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


def test_get_json_does_not_print_cloud_or_encryption_secrets(tmp_path, capsys) -> None:
    store = StateStore(tmp_path)
    message = (
        "aws_secret_access_key=CLI-AWS-SECRET; "
        "encryption_key=CLI-ENCRYPTION-SECRET; passphrase=CLI-PASSPHRASE-SECRET"
    )
    record = TurnRecord.new(request_id="cli-cloud-secret", prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", record.request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    assert "CLI-AWS-SECRET" not in captured.out
    assert "CLI-ENCRYPTION-SECRET" not in captured.out
    assert "CLI-PASSPHRASE-SECRET" not in captured.out
    assert "<redacted>" in captured.out
    assert captured.err == ""


def test_get_rejects_unknown_nested_identity_field_without_rewriting(tmp_path, capsys) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="cli-unknown-identity", prompt="prompt").to_dict()
    value["identity"] = TurnIdentity(conversation_id="conversation-1").to_dict()
    value["identity"]["unexpected_runtime_pointer"] = "foreign-id"
    path = store.turn_path("cli-unknown-identity")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")

    code = main(["get", "cli-unknown-identity", "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert code == 20
    assert result["failure"]["category"] == "corrupt_state"
    assert path.read_text(encoding="utf-8") == raw
    assert captured.err == ""
