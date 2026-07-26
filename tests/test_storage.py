from __future__ import annotations

import json

import pytest

from playwright_gpt_core.errors import CorruptStateError, Failure, FailureCategory
from playwright_gpt_core.models import TurnIdentity, TurnRecord, TurnState
from playwright_gpt_core.storage import StateStore


def test_atomic_round_trip_and_revision(tmp_path) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id="req-a", prompt="hello")
    saved = store.save(record)
    assert saved.revision == 1
    loaded = store.load("req-a")
    assert loaded.request_id == "req-a"
    assert loaded.revision == 1
    saved2 = store.save(loaded.transition(TurnState.PREPARING), expected_revision=1)
    assert saved2.revision == 2


def test_failure_diagnostic_is_sanitized_before_state_write(tmp_path) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id="failure-secret", prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(
            FailureCategory.INVARIANT,
            "access_token: do-not-print",
        ),
    )

    store.save(record)
    raw = store.turn_path(record.request_id).read_text(encoding="utf-8")

    assert "do-not-print" not in raw
    assert "<redacted>" in raw


def test_corrupt_state_is_preserved_and_rejected(tmp_path) -> None:
    store = StateStore(tmp_path)
    path = store.turn_path("req-b")
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(CorruptStateError):
        store.load("req-b")
    assert path.read_text(encoding="utf-8") == "{broken"


def test_state_file_contains_no_prompt_or_secret(tmp_path) -> None:
    store = StateStore(tmp_path)
    store.save(TurnRecord.new(request_id="req-c", prompt="Bearer top.secret.value"))
    raw = store.turn_path("req-c").read_text(encoding="utf-8")
    assert "Bearer" not in raw
    assert "top.secret.value" not in raw
    json.loads(raw)


def test_create_rejects_duplicate_request_identity(tmp_path) -> None:
    from playwright_gpt_core.errors import OwnershipConflictError

    store = StateStore(tmp_path)
    store.create(TurnRecord.new(request_id="same-request", prompt="first"))
    with pytest.raises(OwnershipConflictError):
        store.create(TurnRecord.new(request_id="same-request", prompt="second"))
    persisted = store.load("same-request")
    assert (
        persisted.prompt_sha256
        == TurnRecord.new(request_id="same-request", prompt="first").prompt_sha256
    )


def _write_schema_four_helper_variant(store: StateStore, request_id: str, **changes) -> str:
    record = TurnRecord.new(request_id=request_id, prompt="prompt").to_dict()
    record.update(changes)
    path = store.turn_path(request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(record, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")
    return raw


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"helper_page_keep": "false"}, "helper_page_keep"),
        ({"helper_page_keep": []}, "helper_page_keep"),
        ({"helper_page_keep": {}}, "helper_page_keep"),
        ({"helper_page_keep": None}, "helper_page_keep"),
        ({"helper_page_keep": 0}, "helper_page_keep"),
        ({"helper_page_target_id": 123}, "helper_page_target_id"),
        ({"helper_page_target_id": []}, "helper_page_target_id"),
        ({"helper_page_target_id": ""}, "helper_page_target_id"),
        ({"helper_page_target_id": "bad\ncontrol"}, "helper_page_target_id"),
        ({"helper_page_target_id": "x" * 257}, "helper_page_target_id"),
        ({"helper_page_closed_at": 123}, "helper_page_closed_at"),
        ({"helper_page_closed_at": []}, "helper_page_closed_at"),
        ({"helper_page_closed_at": "not-a-timestamp"}, "helper_page_closed_at"),
        ({"helper_page_closed_at": "2026-07-25T08:00:00"}, "helper_page_closed_at"),
        (
            {
                "helper_page_target_id": None,
                "helper_page_closed_at": "2026-07-25T08:00:00+00:00",
            },
            "requires helper_page_target_id",
        ),
        (
            {"helper_page_target_id": None, "helper_page_keep": True},
            "requires helper_page_target_id",
        ),
    ],
)
def test_schema_four_rejects_malformed_helper_ownership(tmp_path, changes, message) -> None:
    store = StateStore(tmp_path)
    raw = _write_schema_four_helper_variant(store, "bad-helper", **changes)

    with pytest.raises(CorruptStateError, match="corrupt state preserved"):
        store.load("bad-helper")

    assert store.turn_path("bad-helper").read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    "missing_field",
    ["helper_page_target_id", "helper_page_keep", "helper_page_closed_at"],
)
def test_schema_four_requires_all_helper_ownership_fields(tmp_path, missing_field) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="missing-helper", prompt="prompt").to_dict()
    value.pop(missing_field)
    path = store.turn_path("missing-helper")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(CorruptStateError, match="corrupt state preserved"):
        store.load("missing-helper")


def test_corrupt_state_diagnostic_does_not_echo_parser_secret(tmp_path) -> None:
    store = StateStore(tmp_path)
    path = store.turn_path("secret-parser")
    path.parent.mkdir(parents=True, exist_ok=True)
    value = TurnRecord.new(request_id="secret-parser", prompt="prompt").to_dict()
    value["identity"] = {"conversation_id": {"access_token": "do-not-print"}}
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(CorruptStateError) as captured:
        store.load("secret-parser")

    assert "do-not-print" not in str(captured.value)
    assert "do-not-print" not in repr(captured.value)


def test_schema_three_ignores_untrusted_helper_fields(tmp_path) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="schema-three", prompt="prompt").to_dict()
    value["schema_version"] = 3
    value["helper_page_target_id"] = "injected-target"
    value["helper_page_keep"] = True
    value["helper_page_closed_at"] = "2026-07-25T08:00:00+00:00"
    path = store.turn_path("schema-three")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")

    loaded = store.load("schema-three")

    assert loaded.schema_version == 4
    assert loaded.helper_page_target_id is None
    assert loaded.helper_page_keep is False
    assert loaded.helper_page_closed_at is None


def _write_turn_payload(store: StateStore, request_id: str, payload: dict) -> str:
    path = store.turn_path(request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")
    return raw


def _failed_payload(request_id: str = "strict-failure") -> dict:
    return (
        TurnRecord.new(request_id=request_id, prompt="prompt")
        .transition(
            TurnState.FAILED,
            failure=Failure(
                FailureCategory.TIMEOUT,
                "temporary timeout",
                retryable=False,
                external=False,
            ),
        )
        .to_dict()
    )


@pytest.mark.parametrize("field", ["retryable", "external"])
def test_persisted_failure_flags_require_exact_booleans(tmp_path, field) -> None:
    store = StateStore(tmp_path)
    value = _failed_payload()
    value["failure"][field] = "false"
    raw = _write_turn_payload(store, "strict-failure", value)

    with pytest.raises(CorruptStateError):
        store.load("strict-failure")

    assert store.turn_path("strict-failure").read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("schema_version", 4.0),
        ("state", 1),
        ("send_provenance", False),
        ("prompt_sha256", "not-a-sha256"),
        ("prompt_length", "6"),
        ("prompt_length", True),
        ("prompt_length", -1),
        ("target_kind", 1),
        ("target_kind", "latest"),
        ("revision", "1"),
        ("revision", True),
        ("revision", -1),
        ("created_at", "2026-07-26T00:00:00"),
        ("updated_at", 123),
        ("response_sha256", 123),
        ("response_length", "1"),
        ("response_length", -1),
        ("cancellation_requested_at", 123),
    ],
)
def test_turn_record_rejects_coerced_or_malformed_scalars(tmp_path, field, malformed) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="strict-scalars", prompt="prompt").to_dict()
    value[field] = malformed
    _write_turn_payload(store, "strict-scalars", value)

    with pytest.raises(CorruptStateError):
        store.load("strict-scalars")


def test_turn_record_rejects_non_string_baseline_fingerprint(tmp_path) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="strict-baseline", prompt="prompt").to_dict()
    value["baseline_node_fingerprints"] = {"node-1": 123}
    _write_turn_payload(store, "strict-baseline", value)

    with pytest.raises(CorruptStateError):
        store.load("strict-baseline")


@pytest.mark.parametrize(
    "changes",
    [
        {"target_kind": "conversation", "target_conversation_id": None},
        {"target_kind": "fresh", "target_conversation_id": "conversation-1"},
        {"response_sha256": "a" * 64, "response_length": None},
        {"response_sha256": None, "response_length": 1},
        {"state": "NEW", "send_provenance": "DURABLE_HANDOFF"},
    ],
)
def test_turn_record_rejects_invalid_cross_field_combinations(tmp_path, changes) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="strict-cross-field", prompt="prompt").to_dict()
    value.update(changes)
    _write_turn_payload(store, "strict-cross-field", value)

    with pytest.raises(CorruptStateError):
        store.load("strict-cross-field")


def test_state_write_sanitizes_normalized_url_and_proof_labels(tmp_path) -> None:
    store = StateStore(tmp_path)
    message = (
        "GET https://example.test/api/accessToken/URL-SECRET failed; "
        "proof_material=PROOF-SECRET"
    )
    record = TurnRecord.new(request_id="normalized-secret", prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )

    store.save(record)
    raw = store.turn_path(record.request_id).read_text(encoding="utf-8")

    assert "URL-SECRET" not in raw
    assert "PROOF-SECRET" not in raw
    assert "<redacted>" in raw


def test_turn_file_identity_must_match_embedded_request_id(tmp_path) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="different-id", prompt="prompt").to_dict()
    raw = _write_turn_payload(store, "requested-id", value)

    with pytest.raises(CorruptStateError, match="identity mismatch"):
        store.load("requested-id")

    assert store.turn_path("requested-id").read_text(encoding="utf-8") == raw
    assert not store.turn_path("different-id").exists()


def test_find_by_conversation_rejects_mismatched_turn_file_identity(tmp_path) -> None:
    from playwright_gpt_core.models import TurnIdentity

    store = StateStore(tmp_path)
    value = (
        TurnRecord.new(
            request_id="different-id",
            prompt="prompt",
            target_kind="conversation",
            target_conversation_id="conversation-1",
        )
        .with_identity(TurnIdentity(conversation_id="conversation-1"))
        .to_dict()
    )
    _write_turn_payload(store, "requested-id", value)

    with pytest.raises(CorruptStateError, match="identity mismatch"):
        store.find_by_conversation("conversation-1")


def test_state_write_sanitizes_generic_secret_assignments_and_compound_url_paths(
    tmp_path,
) -> None:
    store = StateStore(tmp_path)
    message = (
        "client_secret=CLIENT-SECRET; db_password=DB-SECRET; "
        "GET https://example.test/api/accessToken-URL-SECRET failed"
    )
    record = TurnRecord.new(request_id="generic-secret", prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )

    store.save(record)
    raw = store.turn_path(record.request_id).read_text(encoding="utf-8")

    assert "CLIENT-SECRET" not in raw
    assert "DB-SECRET" not in raw
    assert "URL-SECRET" not in raw
    assert "<redacted>" in raw


def test_state_write_sanitizes_common_credentials_and_private_keys(tmp_path) -> None:
    store = StateStore(tmp_path)
    message = (
        "client_credentials=STATE-CREDENTIALS-VALUE; "
        "private_key=STATE-PRIVATE-KEY-VALUE; "
        "GET https://example.test/api/signingKey-STATE-SIGNING-PATH-VALUE/tail failed"
    )
    record = TurnRecord.new(request_id="credential-key-secret", prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )

    store.save(record)
    raw = store.turn_path(record.request_id).read_text(encoding="utf-8")

    assert "STATE-CREDENTIALS-VALUE" not in raw
    assert "STATE-PRIVATE-KEY-VALUE" not in raw
    assert "STATE-SIGNING-PATH-VALUE" not in raw
    assert "<redacted>" in raw
    assert isinstance(json.loads(raw), dict)


@pytest.mark.parametrize("fingerprint", ["not-a-sha256", "A" * 64, "a" * 63])
def test_baseline_fingerprint_requires_exact_lowercase_sha256(
    tmp_path, fingerprint: str
) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="strict-baseline-sha", prompt="prompt").to_dict()
    value["baseline_node_fingerprints"] = {"root": fingerprint}
    raw = _write_turn_payload(store, "strict-baseline-sha", value)

    with pytest.raises(CorruptStateError):
        store.load("strict-baseline-sha")

    assert store.turn_path("strict-baseline-sha").read_text(encoding="utf-8") == raw


def test_unknown_nested_identity_field_is_rejected_and_preserved(tmp_path) -> None:
    store = StateStore(tmp_path)
    value = TurnRecord.new(request_id="unknown-identity-field", prompt="prompt").to_dict()
    value["identity"] = TurnIdentity(conversation_id="conversation-1").to_dict()
    value["identity"]["unexpected_runtime_pointer"] = "foreign-id"
    raw = _write_turn_payload(store, "unknown-identity-field", value)

    with pytest.raises(CorruptStateError):
        store.load("unknown-identity-field")

    assert store.turn_path("unknown-identity-field").read_text(encoding="utf-8") == raw


def test_state_write_sanitizes_cloud_and_encryption_secrets(tmp_path) -> None:
    store = StateStore(tmp_path)
    message = (
        "aws_secret_access_key=STATE-AWS-SECRET; "
        "encryption_key=STATE-ENCRYPTION-SECRET; passphrase=STATE-PASSPHRASE-SECRET"
    )
    record = TurnRecord.new(request_id="cloud-encryption-secret", prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )

    store.save(record)
    raw = store.turn_path(record.request_id).read_text(encoding="utf-8")

    assert "STATE-AWS-SECRET" not in raw
    assert "STATE-ENCRYPTION-SECRET" not in raw
    assert "STATE-PASSPHRASE-SECRET" not in raw
    assert "<redacted>" in raw
    assert isinstance(json.loads(raw), dict)


def test_state_write_removes_complete_multiword_passphrase(tmp_path) -> None:
    store = StateStore(tmp_path)
    message = "passphrase=correct horse battery staple; retry later"
    record = TurnRecord.new(request_id="multiword-passphrase", prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )

    store.save(record)
    raw = store.turn_path(record.request_id).read_text(encoding="utf-8")

    for secret_word in ("correct", "horse", "battery", "staple"):
        assert secret_word not in raw
    assert "<redacted>" in raw
    assert "retry later" in raw
    assert isinstance(json.loads(raw), dict)


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "quoted-key-secret",
            '{"passphrase": "STATE QUOTED SECRET", "mode": "inspect"}',
            ("STATE", "QUOTED", "SECRET"),
        ),
        (
            "set-cookie-secret",
            "Set-Cookie: arbitrary_name=STATE-COOKIE-SECRET; HttpOnly",
            ("STATE-COOKIE-SECRET",),
        ),
    ],
)
def test_state_write_sanitizes_quoted_keys_and_explicit_cookie_headers(
    tmp_path, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )

    store.save(record)
    raw = store.turn_path(record.request_id).read_text(encoding="utf-8")

    for secret in forbidden:
        assert secret not in raw
    assert "<redacted>" in raw
    assert isinstance(json.loads(raw), dict)


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "escaped-quoted-secret",
            json.dumps(
                {"passphrase": 'STATE "ESCAPED" SECRET TAIL', "mode": "inspect"},
                separators=(",", ":"),
            ),
            ("STATE", "ESCAPED", "SECRET", "TAIL"),
        ),
        (
            "token-cookie-name-secret",
            "Set-Cookie: prefix+suffix=STATE-COOKIE-PUNCT-SECRET; HttpOnly",
            ("STATE-COOKIE-PUNCT-SECRET",),
        ),
        (
            "unterminated-quoted-secret",
            'passphrase="STATE-UNTERMINATED-SECRET-TAIL',
            ("STATE-UNTERMINATED-SECRET-TAIL",),
        ),
    ],
)
def test_state_write_sanitizes_escaped_values_and_token_cookie_names(
    tmp_path, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )

    store.save(record)
    raw = store.turn_path(record.request_id).read_text(encoding="utf-8")

    for secret in forbidden:
        assert secret not in raw
    assert "<redacted>" in raw
    assert isinstance(json.loads(raw), dict)


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "authorization-header-secret",
            "Authorization: Digest nonce=STATE-AUTH-NONCE, response=STATE-AUTH-RESPONSE",
            ("STATE-AUTH-NONCE", "STATE-AUTH-RESPONSE"),
        ),
        (
            "proxy-authorization-secret",
            "Proxy-Authorization: Custom STATE-PROXY-AUTHORIZATION",
            ("STATE-PROXY-AUTHORIZATION",),
        ),
        (
            "proof-signature-secret",
            (
                "GET https://example.test/object?X-Amz-Signature=STATE-SIGNED-URL&"
                "code_verifier=STATE-CODE-VERIFIER&client_assertion=STATE-ASSERTION"
            ),
            ("STATE-SIGNED-URL", "STATE-CODE-VERIFIER", "STATE-ASSERTION"),
        ),
        (
            "dotted-quoted-key-secret",
            '{"aws.secret_access_key":"STATE-DOTTED-CREDENTIAL","mode":"inspect"}',
            ("STATE-DOTTED-CREDENTIAL",),
        ),
    ],
)
def test_state_write_sanitizes_authorization_proof_and_separator_key_shapes(
    tmp_path, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )

    store.save(record)
    raw = store.turn_path(record.request_id).read_text(encoding="utf-8")

    for secret in forbidden:
        assert secret not in raw
    assert "<redacted>" in raw
    assert isinstance(json.loads(raw), dict)


@pytest.mark.parametrize(
    ("request_id", "message", "forbidden"),
    [
        (
            "structured-proxy-authorization-digest",
            json.dumps(
                {
                    "Proxy-Authorization": (
                        "Digest nonce=STATE-PROXY-NONCE, response=STATE-PROXY-RESPONSE"
                    ),
                    "mode": "inspect",
                },
                separators=(",", ":"),
            ),
            ("STATE-PROXY-NONCE", "STATE-PROXY-RESPONSE"),
        ),
        (
            "quoted-proxy-authorization-aws",
            (
                "{'proxy_authorization':'AWS4-HMAC-SHA256 "
                "Credential=STATE-PROXY-CREDENTIAL, "
                "Signature=STATE-PROXY-SIGNATURE','mode':'inspect'}"
            ),
            ("STATE-PROXY-CREDENTIAL", "STATE-PROXY-SIGNATURE"),
        ),
        (
            "quoted-proxy-authorization-custom",
            '{"proxyAuthorization":"CustomScheme STATE-PROXY-ARBITRARY","mode":"inspect"}',
            ("STATE-PROXY-ARBITRARY",),
        ),
        (
            "quoted-proxy-authorization-compact",
            "{'proxyauthorization':'Digest response=STATE-PROXY-COMPACT','mode':'inspect'}",
            ("STATE-PROXY-COMPACT",),
        ),
    ],
)
def test_state_write_sanitizes_structured_and_quoted_proxy_authorization_values(
    tmp_path, request_id: str, message: str, forbidden: tuple[str, ...]
) -> None:
    store = StateStore(tmp_path)
    record = TurnRecord.new(request_id=request_id, prompt="prompt").transition(
        TurnState.FAILED,
        failure=Failure(FailureCategory.INVARIANT, message),
    )

    store.save(record)
    raw = store.turn_path(record.request_id).read_text(encoding="utf-8")

    for secret in forbidden:
        assert secret not in raw
    assert "<redacted>" in raw
    assert "inspect" in raw
    assert isinstance(json.loads(raw), dict)
