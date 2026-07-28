from __future__ import annotations

import json

import pytest

from playwright_api.cli import (
    EXIT_AMBIGUOUS,
    EXIT_CANCELLED,
    EXIT_OWNERSHIP,
    _error_result,
    _render,
    build_parser,
    exit_code,
    main,
)
from playwright_api.errors import Failure, FailureCategory
from playwright_api.models import Result, TurnIdentity, TurnRecord, TurnState
from playwright_api.storage import StateStore


def test_cli_acceptance_surface_requires_explicit_target() -> None:
    parser = build_parser()
    args = parser.parse_args(["send", "Reply OK", "--fresh", "--json"])
    assert args.command == "send"
    assert args.fresh is True
    args = parser.parse_args(["get", "req-1", "--json"])
    assert args.command == "get"
    args = parser.parse_args(["status", "req-1", "--json"])
    assert args.command == "status"
    args = parser.parse_args(
        [
            "send",
            "next",
            "--conversation",
            "conversation-1",
            "--coordination-dir",
            "/tmp/shared-coordination",
            "--deployment-id",
            "profile-9222",
        ]
    )
    assert not hasattr(args, "wait_idle")
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
                failure=Failure(FailureCategory.OWNERSHIP_TIMEOUT, "busy", True),
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


def test_get_json_waits_for_exact_result(monkeypatch, capsys) -> None:
    import playwright_api.cli as cli_module

    class Core:
        def __init__(self, _config) -> None:
            pass

        async def get(self, request_id: str) -> Result:
            return Result(1, request_id, TurnState.COMPLETE, response="EXACT_OK")

    monkeypatch.setattr(cli_module, "ChatGPTClient", Core)

    code = main(["get", "req-1", "--json"])
    output = capsys.readouterr().out.strip()
    assert code == 0
    value = json.loads(output)
    assert value["request_id"] == "req-1"
    assert value["response"] == "EXACT_OK"


@pytest.mark.parametrize("command", ["watch", "recover"])
def test_retired_command_aliases_are_rejected(command, capsys) -> None:
    with pytest.raises(SystemExit) as captured:
        main([command, "req-alias", "--json"])

    assert captured.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["send", "prompt", "--fresh", "--request-id", ""],
        ["get", "../escape"],
    ],
)
def test_invalid_request_ids_return_public_invalid_input(argv, tmp_path, capsys) -> None:
    code = main([*argv, "--state-dir", str(tmp_path / "state"), "--json"])
    value = json.loads(capsys.readouterr().out)

    assert code == 2
    assert value["disposition"] == "invalid_input"
    assert value["failure"]["category"] == "invalid_input"
    assert not (tmp_path / "state").exists()


def test_missing_get_returns_invalid_exit_and_json_failure(tmp_path, capsys) -> None:
    code = main(["get", "missing", "--state-dir", str(tmp_path), "--json"])
    value = json.loads(capsys.readouterr().out)
    assert code == 2
    assert value["failure"]["category"] == "invalid_input"


def test_relative_coordination_directory_returns_invalid_without_creating_state(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(
        [
            "get",
            "missing",
            "--state-dir",
            "local-state",
            "--coordination-dir",
            "shared-coordination",
            "--deployment-id",
            "same-browser",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    value = json.loads(captured.out)

    assert code == 2
    assert value["failure"]["category"] == "invalid_input"
    assert "absolute path" in value["failure"]["message"]
    assert captured.err == ""
    assert not (tmp_path / "local-state").exists()
    assert not (tmp_path / "shared-coordination").exists()


def test_relative_default_home_returns_sanitized_invalid_without_creating_state(
    tmp_path, monkeypatch, capsys
) -> None:
    home_canary = "client_secret=HOME-CANARY"
    xdg_canary = "proof_token=XDG-CANARY"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", home_canary)
    monkeypatch.setenv("XDG_STATE_HOME", xdg_canary)

    code = main(
        [
            "get",
            "missing",
            "--state-dir",
            "local-state",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    value = json.loads(captured.out)

    assert code == 2
    assert value["failure"]["category"] == "invalid_input"
    assert "home directory must be absolute" in value["failure"]["message"]
    assert "HOME-CANARY" not in captured.out
    assert "XDG-CANARY" not in captured.out
    assert captured.err == ""
    assert not (tmp_path / "local-state").exists()
    assert not (tmp_path / home_canary).exists()
    assert not (tmp_path / xdg_canary).exists()


def test_unexpected_exception_diagnostic_does_not_echo_credentials() -> None:
    result = _error_result(RuntimeError("resume_conversation_token=do-not-print"))

    assert result.failure is not None
    assert "do-not-print" not in result.failure.message
    assert "RuntimeError" in result.failure.message


def test_unexpected_exception_json_and_stderr_do_not_echo_credentials(
    monkeypatch, capsys
) -> None:
    import playwright_api.cli as cli_module

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
    assert exit_code(result, command="get") == 10


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
    import playwright_api.cli as cli_module

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
    import playwright_api.cli as cli_module

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


def test_get_json_does_not_print_multiword_passphrase_tail(tmp_path, capsys) -> None:
    store = StateStore(tmp_path)
    message = "qualifiedPassphrase=correct horse battery staple; retry later"
    record = TurnRecord.new(request_id="cli-multiword-passphrase", prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", record.request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    for secret_word in ("correct", "horse", "battery", "staple"):
        assert secret_word not in captured.out
        assert secret_word not in captured.err
    assert "<redacted>" in captured.out
    assert "retry later" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-quoted-key-secret",
            '{"aws_secret_access_key":"CLI-QUOTED-SECRET", "mode":"inspect"}',
            ("CLI-QUOTED-SECRET",),
        ),
        (
            "cli-set-cookie-secret",
            "Set-Cookie: arbitrary_name=CLI-COOKIE-SECRET; HttpOnly",
            ("CLI-COOKIE-SECRET",),
        ),
    ],
)
def test_get_json_does_not_print_quoted_keys_or_explicit_cookie_headers(
    tmp_path, capsys, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", record.request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    for secret in forbidden:
        assert secret not in captured.out
        assert secret not in captured.err
    assert "<redacted>" in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-escaped-quoted-secret",
            json.dumps(
                {"passphrase": 'CLI "ESCAPED" SECRET TAIL', "mode": "inspect"},
                separators=(",", ":"),
            ),
            ("CLI", "ESCAPED", "SECRET", "TAIL"),
        ),
        (
            "cli-token-cookie-name-secret",
            "Set-Cookie: prefix+suffix=CLI-COOKIE-PUNCT-SECRET; HttpOnly",
            ("CLI-COOKIE-PUNCT-SECRET",),
        ),
        (
            "cli-unterminated-quoted-secret",
            'passphrase="CLI-UNTERMINATED-SECRET-TAIL',
            ("CLI-UNTERMINATED-SECRET-TAIL",),
        ),
    ],
)
def test_get_json_does_not_print_escaped_values_or_token_cookie_names(
    tmp_path, capsys, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", record.request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    for secret in forbidden:
        assert secret not in captured.out
        assert secret not in captured.err
    assert "<redacted>" in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-authorization-header-secret",
            (
                "Authorization: AWS4-HMAC-SHA256 Credential=CLI-AUTH-CREDENTIAL, "
                "SignedHeaders=host, Signature=CLI-AUTH-SIGNATURE"
            ),
            ("CLI-AUTH-CREDENTIAL", "CLI-AUTH-SIGNATURE"),
        ),
        (
            "cli-proxy-authorization-secret",
            "Proxy-Authorization: Digest response=CLI-PROXY-AUTH-RESPONSE",
            ("CLI-PROXY-AUTH-RESPONSE",),
        ),
        (
            "cli-proof-shapes-secret",
            (
                "GET https://example.test/object?X-Goog-Signature=CLI-SIGNED-URL&"
                "codeVerifier=CLI-CODE-VERIFIER&clientAssertion=CLI-ASSERTION"
            ),
            ("CLI-SIGNED-URL", "CLI-CODE-VERIFIER", "CLI-ASSERTION"),
        ),
        (
            "cli-dotted-quoted-key-secret",
            '{"aws.secret_access_key":"CLI-DOTTED-CREDENTIAL","mode":"inspect"}',
            ("CLI-DOTTED-CREDENTIAL",),
        ),
    ],
)
def test_get_json_does_not_print_authorization_proof_or_separator_key_values(
    tmp_path, capsys, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", record.request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    for secret in forbidden:
        assert secret not in captured.out
        assert secret not in captured.err
    assert "<redacted>" in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-structured-proxy-authorization-digest",
            json.dumps(
                {
                    "Proxy-Authorization": (
                        "Digest nonce=CLI-PROXY-NONCE, response=CLI-PROXY-RESPONSE"
                    ),
                    "mode": "inspect",
                },
                separators=(",", ":"),
            ),
            ("CLI-PROXY-NONCE", "CLI-PROXY-RESPONSE"),
        ),
        (
            "cli-quoted-proxy-authorization-aws",
            (
                "{'proxy_authorization':'AWS4-HMAC-SHA256 "
                "Credential=CLI-PROXY-CREDENTIAL, "
                "Signature=CLI-PROXY-SIGNATURE','mode':'inspect'}"
            ),
            ("CLI-PROXY-CREDENTIAL", "CLI-PROXY-SIGNATURE"),
        ),
        (
            "cli-quoted-proxy-authorization-custom",
            '{"proxyAuthorization":"CustomScheme CLI-PROXY-ARBITRARY","mode":"inspect"}',
            ("CLI-PROXY-ARBITRARY",),
        ),
        (
            "cli-quoted-proxy-authorization-compact",
            "{'proxyauthorization':'Digest response=CLI-PROXY-COMPACT','mode':'inspect'}",
            ("CLI-PROXY-COMPACT",),
        ),
    ],
)
def test_get_json_does_not_print_structured_or_quoted_proxy_authorization_values(
    tmp_path, capsys, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", record.request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    for secret in forbidden:
        assert secret not in captured.out
        assert secret not in captured.err
    assert "<redacted>" in captured.out
    assert "inspect" in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


_AUTHORIZATION_SUFFIX_WORDS_TURN13 = (
    "retry",
    "failed",
    "failure",
    "error",
    "status",
    "reason",
    "request",
    "operation",
)


@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization"])
@pytest.mark.parametrize("suffix_word", _AUTHORIZATION_SUFFIX_WORDS_TURN13)
def test_get_json_does_not_print_authorization_suffix_parameters(
    tmp_path, capsys, header: str, suffix_word: str
) -> None:
    canary = f"CLI-AUTH-{suffix_word.upper()}-TAIL"
    message = (
        f"{header}: CustomScheme CLI-AUTH-PRIMARY; {suffix_word}={canary}\nnext diagnostic line"
    )
    store = StateStore(tmp_path)
    request_id = f"cli-auth-suffix-{header.lower()}-{suffix_word}"
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    assert "CLI-AUTH-PRIMARY" not in captured.out
    assert canary not in captured.out
    assert canary not in captured.err
    assert "next diagnostic line" in captured.out
    assert "<redacted>" in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-set-cookie-json",
            '{"Set-Cookie":"session=CLI-SET-COOKIE; HttpOnly","mode":"inspect"}',
            ("CLI-SET-COOKIE",),
        ),
        (
            "cli-set-cookie-python",
            "{'setCookie':'session=CLI-SET-COOKIE-CAMEL; Secure','mode':'inspect'}",
            ("CLI-SET-COOKIE-CAMEL",),
        ),
        (
            "cli-qualified-set-cookie",
            '{"headers.set_cookie":"session=CLI-QUALIFIED-COOKIE; Path=/","mode":"inspect"}',
            ("CLI-QUALIFIED-COOKIE",),
        ),
        (
            "cli-qualified-authorization",
            '{"headers.authorization":"Digest response=CLI-QUALIFIED-AUTH","mode":"inspect"}',
            ("CLI-QUALIFIED-AUTH",),
        ),
        (
            "cli-qualified-proxy-authorization",
            "{'headers.proxyAuthorization':'Custom CLI-QUALIFIED-PROXY-AUTH','mode':'inspect'}",
            ("CLI-QUALIFIED-PROXY-AUTH",),
        ),
        (
            "cli-compact-authorization",
            '{"headersauthorization":"Basic CLI-COMPACT-AUTH","mode":"inspect"}',
            ("CLI-COMPACT-AUTH",),
        ),
        (
            "cli-compact-set-cookie",
            "{'headerssetcookie':'session=CLI-COMPACT-COOKIE; Secure','mode':'inspect'}",
            ("CLI-COMPACT-COOKIE",),
        ),
    ],
)
def test_get_json_does_not_print_cookie_or_qualified_authorization_values(
    tmp_path, capsys, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    for secret in forbidden:
        assert secret not in captured.out
        assert secret not in captured.err
    assert "<redacted>" in captured.out
    assert "inspect" in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-compact-multi-level-authorization",
            '{"requestheadersauthorization":"Digest response=CLI-COMPACT-MULTI-AUTH",'
            '"mode":"inspect"}',
            ("CLI-COMPACT-MULTI-AUTH",),
        ),
        (
            "cli-compact-multi-level-proxy-authorization",
            "{'requestheadersproxyauthorization':'Custom "
            "CLI-COMPACT-MULTI-PROXY-AUTH','mode':'inspect'}",
            ("CLI-COMPACT-MULTI-PROXY-AUTH",),
        ),
        (
            "cli-compact-multi-level-set-cookie",
            '{"networkrequestheaderssetcookie":"session=CLI-COMPACT-MULTI-COOKIE; '
            'Secure","mode":"inspect"}',
            ("CLI-COMPACT-MULTI-COOKIE",),
        ),
    ],
)
def test_get_json_does_not_print_lowercase_compact_multi_level_header_values(
    tmp_path, capsys, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    for secret in forbidden:
        assert secret not in captured.out
        assert secret not in captured.err
    assert "<redacted>" in captured.out
    assert "inspect" in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden", "preserved"),
    [
        (
            "cli-folded-authorization-crlf",
            "Authorization: Digest nonce=CLI-FOLDED-PRIMARY\r\n"
            " response=CLI-FOLDED-CONTINUATION\r\n"
            "X-Status: visible",
            ("CLI-FOLDED-PRIMARY", "CLI-FOLDED-CONTINUATION"),
            "X-Status: visible",
        ),
        (
            "cli-folded-proxy-lf",
            "Proxy-Authorization: Custom CLI-FOLDED-PROXY\n"
            "\trealm=CLI-FOLDED-REALM\n"
            "next diagnostic line",
            ("CLI-FOLDED-PROXY", "CLI-FOLDED-REALM"),
            "next diagnostic line",
        ),
    ],
)
def test_get_json_does_not_print_folded_authorization_continuations(
    tmp_path,
    capsys,
    request_id: str,
    message: str,
    forbidden: tuple[str, ...],
    preserved: str,
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    for secret in forbidden:
        assert secret not in captured.out
        assert secret not in captured.err
    assert preserved in captured.out
    assert "<redacted>" in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-canonical-bracket-key",
            '{"headers[Authorization]":"Digest response=CLI-BRACKET-AUTH","mode":"inspect"}',
            ("CLI-BRACKET-AUTH",),
        ),
        (
            "cli-canonical-escaped-slash-key",
            '{"headers\\/authorization":"Digest response=CLI-ESCAPED-SLASH-AUTH",'
            '"mode":"inspect"}',
            ("CLI-ESCAPED-SLASH-AUTH",),
        ),
        (
            "cli-canonical-unicode-key",
            '{"Authoriz\\u0061tion":"Digest response=CLI-UNICODE-AUTH","mode":"inspect"}',
            ("CLI-UNICODE-AUTH",),
        ),
        (
            "cli-canonical-padded-key",
            '{" request.authorization ":"Digest response=CLI-PADDED-AUTH","mode":"inspect"}',
            ("CLI-PADDED-AUTH",),
        ),
        (
            "cli-canonical-malformed-key",
            r'{"Authoriz\qtion":"CLI-MALFORMED-AUTH","mode":"inspect"}',
            ("CLI-MALFORMED-AUTH",),
        ),
    ],
)
def test_get_json_does_not_print_canonical_or_malformed_quoted_header_values(
    tmp_path, capsys, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert code == 20
    for secret in forbidden:
        assert secret not in captured.out
        assert secret not in captured.err
    assert "<redacted>" in captured.out
    assert captured.err == ""
    assert json.loads(captured.out)["failure"]["category"] == "invariant"


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-missing-close-unicode-auth",
            r'{"Authoriz\u0061tion: CLI-MISSING-CLOSE-AUTH',
            "CLI-MISSING-CLOSE-AUTH",
        ),
        (
            "cli-missing-close-unicode-proxy",
            r'{"Proxy-Authoriz\u0061tion: CLI-MISSING-CLOSE-PROXY',
            "CLI-MISSING-CLOSE-PROXY",
        ),
        (
            "cli-missing-close-unicode-cookie",
            r'{"Set-Cook\u0069e: CLI-MISSING-CLOSE-COOKIE',
            "CLI-MISSING-CLOSE-COOKIE",
        ),
        (
            "cli-missing-close-bracket-auth",
            r'{"headers[Authoriz\u0061tion]: CLI-MISSING-CLOSE-BRACKET',
            "CLI-MISSING-CLOSE-BRACKET",
        ),
        (
            "cli-missing-close-escaped-bracket-auth",
            r'{"headers\u005bAuthorization\u005d: CLI-MISSING-CLOSE-ESCAPED-BRACKET',
            "CLI-MISSING-CLOSE-ESCAPED-BRACKET",
        ),
        (
            "cli-missing-close-dotted-auth",
            r'{"request\u002eheaders\u002eauthorization: CLI-MISSING-CLOSE-DOTTED',
            "CLI-MISSING-CLOSE-DOTTED",
        ),
        (
            "cli-missing-close-single-auth",
            r"{'Authoriz\u0061tion: CLI-MISSING-CLOSE-SINGLE",
            "CLI-MISSING-CLOSE-SINGLE",
        ),
    ],
)
def test_get_json_does_not_print_missing_close_canonical_secret_values(
    tmp_path, capsys, request_id: str, message: str, forbidden: str
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert code == 20
    assert forbidden not in captured.out
    assert forbidden not in captured.err
    assert parsed["failure"]["message"] == "<redacted>"
    assert captured.err == ""


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-internal-colon-auth",
            r'{"headers:Authoriz\u0061tion: CLI-INTERNAL-COLON-AUTH',
            "CLI-INTERNAL-COLON-AUTH",
        ),
        (
            "cli-internal-nested-auth",
            r'{"request:headers:Authoriz\u0061tion: CLI-INTERNAL-NESTED-AUTH',
            "CLI-INTERNAL-NESTED-AUTH",
        ),
        (
            "cli-internal-equals-auth",
            r'{"headers=Authoriz\u0061tion: CLI-INTERNAL-EQUALS-AUTH',
            "CLI-INTERNAL-EQUALS-AUTH",
        ),
        (
            "cli-internal-proxy",
            r"{'headers:Proxy-Authoriz\u0061tion: CLI-INTERNAL-PROXY",
            "CLI-INTERNAL-PROXY",
        ),
        (
            "cli-internal-cookie",
            r'{"response:Set-Cook\u0069e: CLI-INTERNAL-COOKIE',
            "CLI-INTERNAL-COOKIE",
        ),
    ],
)
def test_get_json_does_not_print_internal_delimiter_canonical_secret_values(
    tmp_path, capsys, request_id: str, message: str, forbidden: str
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert code == 20
    assert forbidden not in captured.out
    assert forbidden not in captured.err
    assert parsed["failure"]["message"] == "<redacted>"
    assert captured.err == ""


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-json-value-auth-colon",
            r'{"message":"Authorization\u003a Custom CLI-JSON-VALUE-AUTH","mode":"inspect"}',
            "CLI-JSON-VALUE-AUTH",
        ),
        (
            "cli-json-value-auth-equals",
            r'{"message":"Authoriz\u0061tion\u003d Custom '
            r'CLI-JSON-VALUE-UNICODE-AUTH","mode":"inspect"}',
            "CLI-JSON-VALUE-UNICODE-AUTH",
        ),
        (
            "cli-json-value-proxy",
            r'{"message":"Proxy-Authoriz\u0061tion\u003a Digest '
            r'CLI-JSON-VALUE-PROXY","mode":"inspect"}',
            "CLI-JSON-VALUE-PROXY",
        ),
        (
            "cli-json-value-cookie",
            r'{"message":"Cookie\u003a session=CLI-JSON-VALUE-COOKIE; Secure",'
            r'"mode":"inspect"}',
            "CLI-JSON-VALUE-COOKIE",
        ),
        (
            "cli-json-value-set-cookie",
            r'{"message":"Set-Cook\u0069e\u003d '
            r'session=CLI-JSON-VALUE-SET-COOKIE; Secure","mode":"inspect"}',
            "CLI-JSON-VALUE-SET-COOKIE",
        ),
        (
            "cli-fully-escaped-malformed",
            r'{"headers\u003aAuthoriz\u0061tion\u003a CLI-FULLY-ESCAPED-MALFORMED',
            "CLI-FULLY-ESCAPED-MALFORMED",
        ),
    ],
)
def test_get_json_does_not_print_escaped_diagnostic_value_secrets(
    tmp_path, capsys, request_id: str, message: str, forbidden: str
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert code == 20
    assert forbidden not in captured.out
    assert forbidden not in captured.err
    assert "<redacted>" in parsed["failure"]["message"]
    assert captured.err == ""


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-recursive-inner-auth",
            json.dumps(
                {
                    "message": json.dumps(
                        {
                            "message": r"Authorization\u003a Custom CLI-RECURSIVE-AUTH",
                            "mode": "inner",
                        },
                        separators=(",", ":"),
                    ),
                    "mode": "outer",
                },
                separators=(",", ":"),
            ),
            "CLI-RECURSIVE-AUTH",
        ),
        (
            "cli-recursive-inner-proxy",
            json.dumps(
                {
                    "message": json.dumps(
                        {
                            "message": (
                                r"Proxy-Authoriz\u0061tion\u003a Digest "
                                r"CLI-RECURSIVE-PROXY"
                            ),
                            "mode": "inner",
                        },
                        separators=(",", ":"),
                    ),
                    "mode": "outer",
                },
                separators=(",", ":"),
            ),
            "CLI-RECURSIVE-PROXY",
        ),
        (
            "cli-recursive-inner-cookie",
            json.dumps(
                {
                    "message": json.dumps(
                        {
                            "message": (
                                r"Set-Cook\u0069e\u003a "
                                r"session=CLI-RECURSIVE-COOKIE; Secure"
                            ),
                            "mode": "inner",
                        },
                        separators=(",", ":"),
                    ),
                    "mode": "outer",
                },
                separators=(",", ":"),
            ),
            "CLI-RECURSIVE-COOKIE",
        ),
        (
            "cli-double-escaped-auth",
            json.dumps(
                {
                    "message": r"Authorization\u003a Custom CLI-DOUBLE-AUTH",
                    "mode": "outer",
                },
                separators=(",", ":"),
            ),
            "CLI-DOUBLE-AUTH",
        ),
        (
            "cli-double-escaped-proxy",
            json.dumps(
                {
                    "message": r"Proxy-Authorization\u003a Digest CLI-DOUBLE-PROXY",
                    "mode": "outer",
                },
                separators=(",", ":"),
            ),
            "CLI-DOUBLE-PROXY",
        ),
        (
            "cli-double-escaped-cookie",
            json.dumps(
                {
                    "message": (r"Set-Cookie\u003a session=CLI-DOUBLE-COOKIE; Secure"),
                    "mode": "outer",
                },
                separators=(",", ":"),
            ),
            "CLI-DOUBLE-COOKIE",
        ),
    ],
)
def test_get_json_does_not_print_recursive_structured_diagnostic_secrets(
    tmp_path, capsys, request_id: str, message: str, forbidden: str
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert code == 20
    assert forbidden not in captured.out
    assert forbidden not in captured.err
    assert "<redacted>" in parsed["failure"]["message"]
    assert "outer" in parsed["failure"]["message"]
    assert captured.err == ""


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "cli-root-immediate-auth",
            r'"Authorization\u003a Custom CLI-ROOT-IMMEDIATE-AUTH"',
            "CLI-ROOT-IMMEDIATE-AUTH",
        ),
        (
            "cli-root-remaining-auth",
            json.dumps(r"Authorization\u003a Custom CLI-ROOT-REMAINING-AUTH"),
            "CLI-ROOT-REMAINING-AUTH",
        ),
        (
            "cli-root-immediate-proxy",
            r'"Proxy-Authorization\u003a Digest CLI-ROOT-IMMEDIATE-PROXY"',
            "CLI-ROOT-IMMEDIATE-PROXY",
        ),
        (
            "cli-root-remaining-proxy",
            json.dumps(r"Proxy-Authorization\u003a Digest CLI-ROOT-REMAINING-PROXY"),
            "CLI-ROOT-REMAINING-PROXY",
        ),
        (
            "cli-root-immediate-cookie",
            r'"Set-Cookie\u003a session=CLI-ROOT-IMMEDIATE-COOKIE; Secure"',
            "CLI-ROOT-IMMEDIATE-COOKIE",
        ),
        (
            "cli-root-remaining-cookie",
            json.dumps(r"Set-Cookie\u003a session=CLI-ROOT-REMAINING-COOKIE; Secure"),
            "CLI-ROOT-REMAINING-COOKIE",
        ),
        (
            "cli-key-auth",
            json.dumps({r"Authoriz\u0061tion": "CLI-KEY-AUTH", "mode": "inspect"}),
            "CLI-KEY-AUTH",
        ),
        (
            "cli-key-proxy",
            json.dumps({r"Proxy-Authoriz\u0061tion": "CLI-KEY-PROXY", "mode": "inspect"}),
            "CLI-KEY-PROXY",
        ),
        (
            "cli-key-cookie",
            json.dumps({r"Cook\u0069e": "CLI-KEY-COOKIE", "mode": "inspect"}),
            "CLI-KEY-COOKIE",
        ),
        (
            "cli-key-set-cookie",
            json.dumps({r"Set-Cook\u0069e": "CLI-KEY-SET-COOKIE", "mode": "inspect"}),
            "CLI-KEY-SET-COOKIE",
        ),
    ],
)
def test_get_json_does_not_print_json_string_root_or_recursive_key_secrets(
    tmp_path, capsys, request_id: str, message: str, forbidden: str
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    diagnostic = parsed["failure"]["message"]

    assert code == 20
    assert forbidden not in captured.out
    assert forbidden not in captured.err
    assert "<redacted>" in diagnostic
    assert isinstance(json.loads(diagnostic), (dict, str))
    assert captured.err == ""


@pytest.mark.parametrize(
    ("request_id", "key", "canary"),
    [
        (
            "cli-secret-key-token-auth",
            r"Authorization\u003a Bearer CLI-KEY-TOKEN-AUTH",
            "CLI-KEY-TOKEN-AUTH",
        ),
        (
            "cli-secret-key-token-basic",
            r"Authorization\u003a Basic CLI-KEY-TOKEN-BASIC",
            "CLI-KEY-TOKEN-BASIC",
        ),
        (
            "cli-secret-key-token-proxy",
            r"Proxy-Authorization\u003a Digest response=CLI-KEY-TOKEN-PROXY",
            "CLI-KEY-TOKEN-PROXY",
        ),
        (
            "cli-secret-key-token-cookie",
            r"Cookie\u003a session=CLI-KEY-TOKEN-COOKIE",
            "CLI-KEY-TOKEN-COOKIE",
        ),
        (
            "cli-secret-key-token-set-cookie",
            r"Set-Cookie\u003a session=CLI-KEY-TOKEN-SET-COOKIE",
            "CLI-KEY-TOKEN-SET-COOKIE",
        ),
    ],
)
def test_get_json_does_not_print_secret_material_from_json_key_tokens(
    tmp_path, capsys, request_id: str, key: str, canary: str
) -> None:
    diagnostic = json.dumps({key: "ordinary", "mode": "inspect"}, separators=(",", ":"))
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, diagnostic),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert code == 20
    assert canary not in captured.out
    assert canary not in captured.err
    assert parsed["failure"]["message"] == "<redacted>"
    assert captured.err == ""


@pytest.mark.parametrize("position", ["start", "middle", "end"])
def test_get_json_does_not_print_over_budget_structured_diagnostic_canary(
    tmp_path, capsys, position: str
) -> None:
    canary = f"CLI-LARGE-{position.upper()}-AUTH"
    secret = rf"Authorization\u003a Custom {canary}"
    padding = "x" * 40000
    if position == "start":
        payload = {"message": secret, "padding": padding, "mode": "inspect"}
    elif position == "middle":
        payload = {"before": padding, "message": secret, "after": padding}
    else:
        payload = {"padding": padding, "mode": "inspect", "message": secret}
    diagnostic = json.dumps(payload, separators=(",", ":"))
    request_id = f"cli-large-{position}"
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, diagnostic),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert code == 20
    assert canary not in captured.out
    assert canary not in captured.err
    assert parsed["failure"]["message"] == "<redacted>"
    assert captured.err == ""


@pytest.mark.parametrize(
    ("request_id", "key", "canary"),
    [
        (
            "cli-secret-key-token-access",
            r"access_token\u003dCLI-KEY-TOKEN-ACCESS",
            "CLI-KEY-TOKEN-ACCESS",
        ),
        (
            "cli-secret-key-token-proof",
            r"proof_token\u003dCLI-KEY-TOKEN-PROOF",
            "CLI-KEY-TOKEN-PROOF",
        ),
    ],
)
def test_get_json_does_not_print_secret_assignments_from_json_key_tokens(
    tmp_path, capsys, request_id: str, key: str, canary: str
) -> None:
    diagnostic = json.dumps({key: "ordinary", "mode": "inspect"}, separators=(",", ":"))
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, diagnostic),
    )
    store.save(record)

    code = main(["get", request_id, "--state-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert code == 20
    assert canary not in captured.out
    assert canary not in captured.err
    assert parsed["failure"]["message"] == "<redacted>"
    assert captured.err == ""


@pytest.mark.parametrize("kind", ["key-token", "over-budget"])
def test_direct_json_renderer_never_emits_structured_diagnostic_canary(
    capsys, kind: str
) -> None:
    canary = f"DIRECT-RENDER-{kind.upper()}-CANARY"
    if kind == "key-token":
        key = rf"Authorization\u003a Bearer {canary}"
        diagnostic = json.dumps({key: "ordinary", "mode": "inspect"}, separators=(",", ":"))
    else:
        diagnostic = json.dumps(
            {
                "padding": "x" * 40000,
                "message": rf"proof_token\u003d{canary}",
            },
            separators=(",", ":"),
        )
    result = Result(
        1,
        "direct-render",
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, diagnostic),
    )

    _render(result, json_mode=True)
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert canary not in captured.out
    assert captured.err == ""
    assert parsed["failure"]["message"] == "<redacted>"
