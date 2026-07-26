from __future__ import annotations

import json

import pytest

from playwright_gpt_core.redaction import redact, safe_json_dumps, sanitize_diagnostic


def test_recursive_secret_redaction() -> None:
    value = {
        "authorization": "Bearer abc",
        "resume_conversation_token": "danger",
        "nested": {"sentinel": "proof", "ok": "value"},
        "items": [{"access_token": "hidden"}],
    }
    cleaned = redact(value)
    rendered = json.dumps(cleaned)
    assert "danger" not in rendered
    assert "proof" not in rendered
    assert "hidden" not in rendered
    assert cleaned["nested"]["ok"] == "value"


def test_jwt_and_bearer_values_are_redacted_even_under_safe_key() -> None:
    rendered = safe_json_dumps(
        {
            "message": "Bearer abc.def.ghi",
            "other": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
        }
    )
    assert "abc.def.ghi" not in rendered
    assert "eyJhbGci" not in rendered


@pytest.mark.parametrize(
    "diagnostic",
    [
        "access_token: access-secret-value",
        "resume_conversation_token=resume-secret-value",
        "sentinel=sentinel-secret-value",
        "proof_token=proof-secret-value",
        "Authorization: Basic basic-secret-value",
        "cookie=session-secret-value",
    ],
)
def test_free_form_credential_assignments_are_sanitized(diagnostic: str) -> None:
    rendered = sanitize_diagnostic(f"operation failed: {diagnostic}; retry later")

    assert "secret-value" not in rendered
    assert "operation failed" in rendered
    assert "retry later" in rendered
    assert "<redacted>" in rendered


def test_credential_bearing_url_is_sanitized_without_losing_origin_context() -> None:
    rendered = sanitize_diagnostic(
        "GET https://alice:password@example.test/api/token/opaque-secret"
        "?access_token=query-secret#fragment-secret failed"
    )

    assert "alice" not in rendered
    assert "password" not in rendered
    assert "opaque-secret" not in rendered
    assert "query-secret" not in rendered
    assert "fragment-secret" not in rendered
    assert "https://example.test/api/token/<redacted>" in rendered
    assert "failed" in rendered


def test_safe_json_dumps_sanitizes_nested_diagnostic_strings() -> None:
    rendered = safe_json_dumps(
        {"failure": {"message": "resume_conversation_token=do-not-print"}}
    )
    assert "do-not-print" not in rendered


@pytest.mark.parametrize(
    ("diagnostic", "secret"),
    [
        (
            "GET https://example.test/api/accessToken/URL-SECRET failed",
            "URL-SECRET",
        ),
        (
            "GET https://example.test/api/session_token/URL-SECRET failed",
            "URL-SECRET",
        ),
        (
            "GET https://example.test/api/api_key/URL-SECRET failed",
            "URL-SECRET",
        ),
        ("proof_material=PROOF-SECRET", "PROOF-SECRET"),
    ],
)
def test_normalized_credential_labels_are_sanitized_in_urls_and_text(
    diagnostic: str, secret: str
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert secret not in rendered
    assert "<redacted>" in rendered


@pytest.mark.parametrize(
    ("label", "secret"),
    [
        ("client_secret", "CLIENT-SECRET"),
        ("client-secret", "CLIENT-SECRET"),
        ("clientSecret", "CLIENT-SECRET"),
        ("clientsecret", "CLIENT-SECRET"),
        ("db_password", "DB-SECRET"),
        ("db-password", "DB-SECRET"),
        ("dbPassword", "DB-SECRET"),
        ("dbpassword", "DB-SECRET"),
    ],
)
def test_generic_normalized_secret_assignments_reach_shared_secret_grammar(
    label: str, secret: str
) -> None:
    rendered = sanitize_diagnostic(f"operation failed: {label}={secret}; retry later")

    assert secret not in rendered
    assert "<redacted>" in rendered
    assert "operation failed" in rendered
    assert "retry later" in rendered


@pytest.mark.parametrize(
    "segment",
    [
        "token-URL-SECRET",
        "accessToken-URL-SECRET",
        "session_token-URL-SECRET",
        "api_key-URL-SECRET",
        "client_secret-URL-SECRET",
        "dbPasswordURLSECRET",
    ],
)
def test_compound_credential_path_segments_are_redacted(segment: str) -> None:
    rendered = sanitize_diagnostic(f"GET https://example.test/api/{segment}/tail failed")

    assert "URL-SECRET" not in rendered
    assert "URLSECRET" not in rendered
    assert "<redacted>" in rendered
    assert "tail" not in rendered


@pytest.mark.parametrize(
    "segment",
    ["tokenization-guide", "accessTokens-resource", "passwordless-login"],
)
def test_ordinary_near_match_path_segments_remain_diagnostic_context(segment: str) -> None:
    rendered = sanitize_diagnostic(f"GET https://example.test/api/{segment} failed")

    assert segment in rendered
    assert "<redacted>" not in rendered


def test_failure_object_sanitizes_generic_assignments_and_compound_path() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    failure = Failure(
        FailureCategory.INVARIANT,
        "client_secret=CLIENT-SECRET; GET https://example.test/api/token-URL-SECRET failed",
    )

    assert "CLIENT-SECRET" not in failure.message
    assert "URL-SECRET" not in failure.message
    assert "<redacted>" in failure.message


@pytest.mark.parametrize(
    ("label", "secret"),
    [
        ("credential", "CREDENTIAL-VALUE"),
        ("credentials", "CREDENTIALS-VALUE"),
        ("client_credentials", "CLIENT-CREDENTIALS-VALUE"),
        ("client-credentials", "CLIENT-CREDENTIALS-VALUE"),
        ("clientCredential", "CLIENT-CREDENTIAL-VALUE"),
        ("clientcredentials", "CLIENT-CREDENTIALS-VALUE"),
        ("private_key", "PRIVATE-KEY-VALUE"),
        ("private-key", "PRIVATE-KEY-VALUE"),
        ("privateKey", "PRIVATE-KEY-VALUE"),
        ("privatekey", "PRIVATE-KEY-VALUE"),
        ("signing_key", "SIGNING-KEY-VALUE"),
        ("signing-key", "SIGNING-KEY-VALUE"),
        ("signingKey", "SIGNING-KEY-VALUE"),
        ("signingkey", "SIGNING-KEY-VALUE"),
    ],
)
def test_common_credential_and_private_key_assignments_are_sanitized(
    label: str, secret: str
) -> None:
    rendered = sanitize_diagnostic(f"operation failed: {label}={secret}; retry later")

    assert secret not in rendered
    assert "<redacted>" in rendered
    assert "operation failed" in rendered
    assert "retry later" in rendered


@pytest.mark.parametrize(
    ("label", "secret"),
    [
        ("client_credentials", "STRUCTURED-CREDENTIALS-VALUE"),
        ("clientCredential", "STRUCTURED-CREDENTIAL-VALUE"),
        ("private_key", "STRUCTURED-PRIVATE-KEY-VALUE"),
        ("signingKey", "STRUCTURED-SIGNING-KEY-VALUE"),
    ],
)
def test_structured_common_credential_and_private_key_values_are_sanitized(
    label: str, secret: str
) -> None:
    rendered = safe_json_dumps({"outer": {label: secret}})

    assert secret not in rendered
    assert "<redacted>" in rendered


@pytest.mark.parametrize(
    "segment",
    [
        "clientCredentials-CREDENTIAL-PATH-VALUE",
        "clientCredential-CREDENTIAL-PATH-VALUE",
        "private_key-PRIVATE-KEY-PATH-VALUE",
        "privateKeyPRIVATEKEYPATHVALUE",
        "signing_key-SIGNING-KEY-PATH-VALUE",
        "signingKeySIGNINGKEYPATHVALUE",
    ],
)
def test_compound_common_credential_and_private_key_paths_are_sanitized(
    segment: str,
) -> None:
    rendered = sanitize_diagnostic(f"GET https://example.test/api/{segment}/tail failed")

    for secret in (
        "CREDENTIAL-PATH-VALUE",
        "PRIVATE-KEY-PATH-VALUE",
        "PRIVATEKEYPATHVALUE",
        "SIGNING-KEY-PATH-VALUE",
        "SIGNINGKEYPATHVALUE",
    ):
        assert secret not in rendered
    assert "<redacted>" in rendered
    assert "tail" not in rendered


@pytest.mark.parametrize(
    "segment",
    [
        "credentials-guide",
        "credentialed-user",
        "private-key-format",
        "signing-key-docs",
        "public-key-resource",
    ],
)
def test_common_credential_and_key_near_match_paths_remain_visible(segment: str) -> None:
    rendered = sanitize_diagnostic(f"GET https://example.test/api/{segment} failed")

    assert segment in rendered
    assert "<redacted>" not in rendered


def test_failure_dict_sanitizes_common_credentials_and_private_keys() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    failure = Failure(
        FailureCategory.INVARIANT,
        "client_credentials=FAILURE-CREDENTIALS-VALUE; "
        "private_key=FAILURE-PRIVATE-KEY-VALUE; "
        "GET https://example.test/api/signingKey-SIGNING-PATH-VALUE/tail failed",
    )
    rendered = json.dumps(failure.to_dict(), sort_keys=True)

    assert "FAILURE-CREDENTIALS-VALUE" not in rendered
    assert "FAILURE-PRIVATE-KEY-VALUE" not in rendered
    assert "SIGNING-PATH-VALUE" not in rendered
    assert "<redacted>" in rendered


def test_common_credentials_and_private_keys_are_sanitized_in_url_query() -> None:
    rendered = sanitize_diagnostic(
        "GET https://example.test/callback?"
        "clientCredentials=QUERY-CREDENTIALS-VALUE&"
        "private_key=QUERY-PRIVATE-KEY-VALUE&"
        "signingKey=QUERY-SIGNING-KEY-VALUE&mode=inspect failed"
    )

    assert "QUERY-CREDENTIALS-VALUE" not in rendered
    assert "QUERY-PRIVATE-KEY-VALUE" not in rendered
    assert "QUERY-SIGNING-KEY-VALUE" not in rendered
    assert "mode=inspect" in rendered
    assert rendered.count("<redacted>") >= 3


def test_nested_diagnostic_sanitizes_common_credentials_and_private_keys() -> None:
    rendered = safe_json_dumps(
        {
            "failure": {
                "message": (
                    "client_credentials=NESTED-CREDENTIALS-VALUE; "
                    "privateKey=NESTED-PRIVATE-KEY-VALUE"
                )
            }
        }
    )

    assert "NESTED-CREDENTIALS-VALUE" not in rendered
    assert "NESTED-PRIVATE-KEY-VALUE" not in rendered
    assert "<redacted>" in rendered
