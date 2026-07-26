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
    if diagnostic.startswith("Authorization"):
        assert "retry later" not in rendered
    else:
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


@pytest.mark.parametrize(
    ("label", "secret"),
    [
        ("aws_secret_access_key", "AWS-SECRET-ACCESS-VALUE"),
        ("aws-secret-access-key", "AWS-SECRET-ACCESS-VALUE"),
        ("awsSecretAccessKey", "AWS-SECRET-ACCESS-VALUE"),
        ("awssecretaccesskey", "AWS-SECRET-ACCESS-VALUE"),
        ("secret_access_key", "SECRET-ACCESS-VALUE"),
        ("secretAccessKey", "SECRET-ACCESS-VALUE"),
        ("encryption_key", "ENCRYPTION-KEY-VALUE"),
        ("encryptionKey", "ENCRYPTION-KEY-VALUE"),
        ("passphrase", "PASSPHRASE-VALUE"),
        ("sshPassphrase", "PASSPHRASE-VALUE"),
    ],
)
def test_cloud_and_encryption_secret_assignments_are_sanitized(label: str, secret: str) -> None:
    rendered = sanitize_diagnostic(f"operation failed: {label}={secret}; retry later")

    assert secret not in rendered
    assert "<redacted>" in rendered
    assert "retry later" in rendered


@pytest.mark.parametrize(
    "label",
    ["aws_secret_access_key", "secretAccessKey", "encryption_key", "sshPassphrase"],
)
def test_cloud_and_encryption_structured_values_are_sanitized(label: str) -> None:
    rendered = safe_json_dumps({"outer": {label: "STRUCTURED-SECRET-VALUE"}})

    assert "STRUCTURED-SECRET-VALUE" not in rendered
    assert "<redacted>" in rendered


@pytest.mark.parametrize(
    "segment",
    [
        "aws_secret_access_key-PATH-SECRET-VALUE",
        "awsSecretAccessKeyPATHSECRETVALUE",
        "secret-access-key-PATH-SECRET-VALUE",
        "encryptionKeyPATHSECRETVALUE",
        "passphrase-PATH-SECRET-VALUE",
    ],
)
def test_cloud_and_encryption_secret_path_payloads_are_sanitized(segment: str) -> None:
    rendered = sanitize_diagnostic(f"GET https://example.test/api/{segment}/tail failed")

    assert "SECRET-VALUE" not in rendered
    assert "SECRETVALUE" not in rendered
    assert "tail" not in rendered
    assert "<redacted>" in rendered


@pytest.mark.parametrize(
    "segment",
    ["secret-access-key-guide", "encryption-key-format", "passphrase-help"],
)
def test_cloud_and_encryption_documentation_paths_remain_visible(segment: str) -> None:
    rendered = sanitize_diagnostic(f"GET https://example.test/api/{segment} failed")

    assert segment in rendered
    assert "<redacted>" not in rendered


def test_cloud_and_encryption_query_failure_and_nested_diagnostics_are_sanitized() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    query_diagnostic = (
        "GET https://example.test/callback?"
        "awsSecretAccessKey=QUERY-SECRET-VALUE&mode=inspect failed"
    )
    failure_diagnostic = "passphrase=PASS-SECRET-VALUE"
    rendered = safe_json_dumps({"failure": {"message": query_diagnostic}})
    failure = json.dumps(Failure(FailureCategory.INVARIANT, failure_diagnostic).to_dict())

    assert "QUERY-SECRET-VALUE" not in rendered
    assert "mode=inspect" in rendered
    assert "PASS-SECRET-VALUE" not in failure
    for output in (rendered, failure):
        assert "<redacted>" in output


@pytest.mark.parametrize(
    ("diagnostic", "preserved"),
    [
        ("passphrase=correct horse battery staple", None),
        ("passphrase: correct horse battery staple; retry later", "retry later"),
        ("qualifiedPassphrase=correct horse battery staple, mode inspect", "mode inspect"),
        ("ssh_passphrase=correct horse battery staple\nnext line", "next line"),
        ("passphrase=correct horse battery staple&mode=inspect", "mode=inspect"),
        ("passphrase=correct horse battery staple mode=inspect", "mode=inspect"),
        ('passphrase="correct horse battery staple"; retry later', "retry later"),
    ],
)
def test_multiword_passphrases_redact_to_safe_boundary(
    diagnostic: str, preserved: str | None
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    for secret_word in ("correct", "horse", "battery", "staple"):
        assert secret_word not in rendered
    assert "<redacted>" in rendered
    if preserved is not None:
        assert preserved in rendered


def test_multiword_passphrase_is_removed_from_nested_diagnostic_and_failure() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    diagnostic = "qualifiedPassphrase=correct horse battery staple; retry later"
    nested = safe_json_dumps({"failure": {"message": diagnostic}})
    failure = json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict())

    for output in (nested, failure):
        for secret_word in ("correct", "horse", "battery", "staple"):
            assert secret_word not in output
        assert "<redacted>" in output
        assert "retry later" in output


@pytest.mark.parametrize(
    "diagnostic",
    [
        "Cookie: arbitrary_name=COOKIE-SECRET-VALUE",
        "alpha=COOKIE-ONE; beta=COOKIE-TWO",
    ],
)
def test_cookie_headers_and_multi_pair_cookie_blobs_remain_fully_redacted(
    diagnostic: str,
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert rendered == "<redacted>"
    assert "COOKIE" not in rendered


@pytest.mark.parametrize(
    ("diagnostic", "secret_parts", "preserved"),
    [
        (
            '{"passphrase": "correct horse battery staple", "mode": "inspect"}',
            ("correct", "horse", "battery", "staple"),
            '"mode": "inspect"',
        ),
        (
            "{'passphrase': 'correct horse battery staple', 'mode': 'inspect'}",
            ("correct", "horse", "battery", "staple"),
            "'mode': 'inspect'",
        ),
        (
            '{"aws_secret_access_key":"QUOTED-CLOUD-SECRET", "status":"failed"}',
            ("QUOTED-CLOUD-SECRET",),
            '"status":"failed"',
        ),
        (
            "prefix 'qualifiedPassphrase' = 'correct horse battery staple'; retry later",
            ("correct", "horse", "battery", "staple"),
            "retry later",
        ),
    ],
)
def test_quoted_secret_keys_are_sanitized_with_bounded_context(
    diagnostic: str, secret_parts: tuple[str, ...], preserved: str
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    for secret in secret_parts:
        assert secret not in rendered
    assert "<redacted>" in rendered
    assert preserved in rendered


@pytest.mark.parametrize(
    "diagnostic",
    [
        "Set-Cookie: arbitrary_name=SET-COOKIE-SECRET; HttpOnly",
        "Cookie: arbitrary_name=COOKIE-HEADER-SECRET",
        "backend response Set-Cookie: arbitrary_name=EMBEDDED-COOKIE-SECRET; Secure",
    ],
)
def test_explicit_single_pair_cookie_headers_are_fully_redacted(diagnostic: str) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert rendered == "<redacted>"
    assert "SECRET" not in rendered


def test_single_raw_key_value_without_cookie_provenance_remains_diagnostic_context() -> None:
    diagnostic = "arbitrary_name=ordinary-value; HttpOnly flag observed"

    assert sanitize_diagnostic(diagnostic) == diagnostic


def test_quoted_keys_and_set_cookie_are_removed_from_nested_diagnostic_and_failure() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    diagnostics = [
        '{"passphrase": "NESTED QUOTED SECRET", "mode": "inspect"}',
        "Set-Cookie: arbitrary_name=NESTED-COOKIE-SECRET; HttpOnly",
    ]
    for diagnostic in diagnostics:
        nested = safe_json_dumps({"failure": {"message": diagnostic}})
        failure = json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict())
        for output in (nested, failure):
            assert "NESTED" not in output
            assert "SECRET" not in output
            assert "<redacted>" in output


@pytest.mark.parametrize("backslash_count", [0, 1, 2, 3, 4])
def test_json_quoted_secret_values_are_escape_aware(backslash_count: int) -> None:
    secret_value = "alpha " + ("\\" * backslash_count) + '"quoted" omega tail'
    diagnostic = json.dumps(
        {"passphrase": secret_value, "mode": "inspect"},
        separators=(",", ":"),
    )
    assert json.loads(diagnostic)["passphrase"] == secret_value

    rendered = sanitize_diagnostic(diagnostic)

    assert "alpha" not in rendered
    assert "quoted" not in rendered
    assert "omega" not in rendered
    assert "tail" not in rendered
    assert json.loads(rendered) == {"passphrase": "<redacted>", "mode": "inspect"}


def test_python_style_single_quoted_secret_value_is_escape_aware() -> None:
    diagnostic = r"{'passphrase': 'alpha \'quoted\' omega tail', 'mode': 'inspect'}"

    rendered = sanitize_diagnostic(diagnostic)

    for secret in ("alpha", "quoted", "omega", "tail"):
        assert secret not in rendered
    assert rendered == "{'passphrase': '<redacted>', 'mode': 'inspect'}"


@pytest.mark.parametrize("punctuation", ["+", "$", "!", "^", "|", "~"])
def test_explicit_cookie_headers_accept_http_token_punctuation(punctuation: str) -> None:
    from http.cookies import SimpleCookie

    cookie_name = f"prefix{punctuation}suffix"
    diagnostic = f"Set-Cookie: {cookie_name}=TOKEN-PUNCTUATION-SECRET; HttpOnly"
    parsed = SimpleCookie()
    parsed.load(f"{cookie_name}=value")
    assert cookie_name in parsed

    rendered = sanitize_diagnostic(diagnostic)

    assert rendered == "<redacted>"
    assert "TOKEN-PUNCTUATION-SECRET" not in rendered


def test_escaped_quotes_and_token_cookie_names_are_removed_from_nested_failure() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    diagnostics = [
        json.dumps(
            {"passphrase": 'NESTED "ESCAPED" SECRET TAIL', "mode": "inspect"},
            separators=(",", ":"),
        ),
        "Set-Cookie: prefix+suffix=NESTED-COOKIE-PUNCT-SECRET; HttpOnly",
    ]
    for diagnostic in diagnostics:
        nested = safe_json_dumps({"failure": {"message": diagnostic}})
        failure = json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict())
        for output in (nested, failure):
            for secret in ("NESTED", "ESCAPED", "SECRET", "TAIL", "COOKIE", "PUNCT"):
                assert secret not in output
            assert "<redacted>" in output


@pytest.mark.parametrize(
    "diagnostic",
    [
        'passphrase="UNTERMINATED SECRET TAIL',
        "passphrase='UNTERMINATED SECRET TAIL",
        'passphrase="UNTERMINATED SECRET TAIL\\',
    ],
)
def test_unterminated_quoted_secret_values_fail_closed(diagnostic: str) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert rendered == "passphrase=<redacted>"
    for secret in ("UNTERMINATED", "SECRET", "TAIL"):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("diagnostic", "forbidden"),
    [
        (
            (
                "operation failed Authorization: Digest username=alice, realm=chat, "
                "nonce=AUTH-DIGEST-NONCE, response=AUTH-DIGEST-RESPONSE, "
                "opaque=AUTH-DIGEST-OPAQUE\nretry later"
            ),
            ("AUTH-DIGEST-NONCE", "AUTH-DIGEST-RESPONSE", "AUTH-DIGEST-OPAQUE"),
        ),
        (
            (
                "Authorization = AWS4-HMAC-SHA256 Credential=AUTH-AWS-CREDENTIAL, "
                "SignedHeaders=host;x-amz-date, Signature=AUTH-AWS-SIGNATURE\nretry later"
            ),
            ("AUTH-AWS-CREDENTIAL", "AUTH-AWS-SIGNATURE"),
        ),
        (
            (
                "Proxy-Authorization: Digest username=proxy, "
                "response=PROXY-DIGEST-RESPONSE\nretry later"
            ),
            ("PROXY-DIGEST-RESPONSE",),
        ),
        (
            "authorization: CustomScheme ARBITRARY-AUTHORIZATION-VALUE\nretry later",
            ("ARBITRARY-AUTHORIZATION-VALUE",),
        ),
        (
            "Proxy-Authorization=Basic PROXY-BASIC-VALUE\nretry later",
            ("PROXY-BASIC-VALUE",),
        ),
    ],
)
def test_explicit_authorization_headers_redact_complete_value_for_any_scheme(
    diagnostic: str, forbidden: tuple[str, ...]
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    for secret in forbidden:
        assert secret not in rendered
    assert "<redacted>" in rendered
    assert "retry later" in rendered


@pytest.mark.parametrize(
    ("label", "secret"),
    [
        ("signature", "SIGNATURE-VALUE"),
        ("request_signature", "REQUEST-SIGNATURE-VALUE"),
        ("requestSignature", "REQUEST-SIGNATURE-VALUE"),
        ("requestsignature", "REQUEST-SIGNATURE-VALUE"),
        ("X-Amz-Signature", "AMZ-SIGNATURE-VALUE"),
        ("X_Goog_Signature", "GOOG-SIGNATURE-VALUE"),
        ("code_verifier", "CODE-VERIFIER-VALUE"),
        ("code-verifier", "CODE-VERIFIER-VALUE"),
        ("codeVerifier", "CODE-VERIFIER-VALUE"),
        ("codeverifier", "CODE-VERIFIER-VALUE"),
        ("client_assertion", "CLIENT-ASSERTION-VALUE"),
        ("client-assertion", "CLIENT-ASSERTION-VALUE"),
        ("clientAssertion", "CLIENT-ASSERTION-VALUE"),
        ("clientassertion", "CLIENT-ASSERTION-VALUE"),
    ],
)
def test_signature_verifier_and_assertion_assignments_use_shared_secret_grammar(
    label: str, secret: str
) -> None:
    rendered = sanitize_diagnostic(f"operation failed: {label}={secret}; retry later")

    assert secret not in rendered
    assert "<redacted>" in rendered
    assert "retry later" in rendered


@pytest.mark.parametrize(
    "label",
    [
        "signature",
        "X-Amz-Signature",
        "XGoogSignature",
        "code_verifier",
        "codeVerifier",
        "client_assertion",
        "clientAssertion",
    ],
)
def test_signature_verifier_and_assertion_structured_values_are_sanitized(label: str) -> None:
    rendered = safe_json_dumps({"outer": {label: "STRUCTURED-PROOF-VALUE"}})

    assert "STRUCTURED-PROOF-VALUE" not in rendered
    assert "<redacted>" in rendered


def test_signed_url_signature_and_oauth_proof_query_values_are_sanitized() -> None:
    rendered = sanitize_diagnostic(
        "GET https://example.test/object?"
        "X-Amz-Signature=SIGNED-URL-SIGNATURE&"
        "X-Amz-Credential=SIGNED-URL-CREDENTIAL&"
        "code_verifier=QUERY-CODE-VERIFIER&"
        "clientAssertion=QUERY-CLIENT-ASSERTION&mode=inspect failed"
    )

    for secret in (
        "SIGNED-URL-SIGNATURE",
        "SIGNED-URL-CREDENTIAL",
        "QUERY-CODE-VERIFIER",
        "QUERY-CLIENT-ASSERTION",
    ):
        assert secret not in rendered
    assert "mode=inspect" in rendered
    assert rendered.count("<redacted>") >= 4


@pytest.mark.parametrize(
    "segment",
    ["signature-guide", "code-verifier-docs", "client-assertion-schema"],
)
def test_proof_label_documentation_paths_remain_visible(segment: str) -> None:
    rendered = sanitize_diagnostic(f"GET https://example.test/api/{segment} failed")

    assert segment in rendered
    assert "<redacted>" not in rendered


@pytest.mark.parametrize(
    ("diagnostic", "secret", "preserved"),
    [
        (
            '{"aws.secret_access_key":"DOTTED-CREDENTIAL-VALUE","mode":"inspect"}',
            "DOTTED-CREDENTIAL-VALUE",
            '"mode":"inspect"',
        ),
        (
            "{'aws:secret_access_key':'COLON-CREDENTIAL-VALUE','mode':'inspect'}",
            "COLON-CREDENTIAL-VALUE",
            "'mode':'inspect'",
        ),
        (
            '{"aws/secret_access_key":"SLASH-CREDENTIAL-VALUE","mode":"inspect"}',
            "SLASH-CREDENTIAL-VALUE",
            '"mode":"inspect"',
        ),
    ],
)
def test_quoted_separator_bearing_secret_keys_are_sanitized(
    diagnostic: str, secret: str, preserved: str
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert secret not in rendered
    assert "<redacted>" in rendered
    assert preserved in rendered


def test_new_authorization_and_proof_shapes_are_removed_from_nested_failure() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    diagnostics = [
        "Authorization: Digest nonce=NESTED-AUTH-NONCE, response=NESTED-AUTH-RESPONSE",
        "GET https://example.test/object?X-Goog-Signature=NESTED-SIGNED-URL",
        "code_verifier=NESTED-CODE-VERIFIER; client_assertion=NESTED-ASSERTION",
        '{"aws.secret_access_key":"NESTED-DOTTED-CREDENTIAL","mode":"inspect"}',
    ]
    for diagnostic in diagnostics:
        nested = safe_json_dumps({"failure": {"message": diagnostic}})
        failure = json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict())
        for output in (nested, failure):
            for secret in (
                "NESTED-AUTH-NONCE",
                "NESTED-AUTH-RESPONSE",
                "NESTED-SIGNED-URL",
                "NESTED-CODE-VERIFIER",
                "NESTED-ASSERTION",
                "NESTED-DOTTED-CREDENTIAL",
            ):
                assert secret not in output
            assert "<redacted>" in output


@pytest.mark.parametrize(
    ("label", "value", "forbidden"),
    [
        (
            "Proxy-Authorization",
            "Digest nonce=STRUCTURED-PROXY-NONCE, response=STRUCTURED-PROXY-RESPONSE",
            ("STRUCTURED-PROXY-NONCE", "STRUCTURED-PROXY-RESPONSE"),
        ),
        (
            "proxy_authorization",
            (
                "AWS4-HMAC-SHA256 Credential=STRUCTURED-PROXY-CREDENTIAL, "
                "SignedHeaders=host, Signature=STRUCTURED-PROXY-SIGNATURE"
            ),
            ("STRUCTURED-PROXY-CREDENTIAL", "STRUCTURED-PROXY-SIGNATURE"),
        ),
        (
            "proxyAuthorization",
            "CustomScheme STRUCTURED-PROXY-ARBITRARY",
            ("STRUCTURED-PROXY-ARBITRARY",),
        ),
        (
            "proxyauthorization",
            "Basic STRUCTURED-PROXY-BASIC",
            ("STRUCTURED-PROXY-BASIC",),
        ),
    ],
)
def test_proxy_authorization_structured_mapping_values_use_shared_secret_grammar(
    label: str, value: str, forbidden: tuple[str, ...]
) -> None:
    rendered = safe_json_dumps({"headers": {label: value}, "mode": "inspect"})
    parsed = json.loads(rendered)

    for secret in forbidden:
        assert secret not in rendered
    assert parsed["headers"][label] == "<redacted>"
    assert parsed["mode"] == "inspect"


@pytest.mark.parametrize(
    ("diagnostic", "forbidden", "preserved"),
    [
        (
            json.dumps(
                {
                    "Proxy-Authorization": (
                        "Digest nonce=QUOTED-PROXY-NONCE, response=QUOTED-PROXY-RESPONSE"
                    ),
                    "mode": "inspect",
                },
                separators=(",", ":"),
            ),
            ("QUOTED-PROXY-NONCE", "QUOTED-PROXY-RESPONSE"),
            '"mode":"inspect"',
        ),
        (
            (
                "{'proxy_authorization':'AWS4-HMAC-SHA256 "
                "Credential=QUOTED-PROXY-CREDENTIAL, "
                "Signature=QUOTED-PROXY-SIGNATURE','mode':'inspect'}"
            ),
            ("QUOTED-PROXY-CREDENTIAL", "QUOTED-PROXY-SIGNATURE"),
            "'mode':'inspect'",
        ),
        (
            '{"proxyAuthorization":"CustomScheme QUOTED-PROXY-ARBITRARY","mode":"inspect"}',
            ("QUOTED-PROXY-ARBITRARY",),
            '"mode":"inspect"',
        ),
        (
            "{'proxyauthorization':'Digest response=QUOTED-PROXY-COMPACT','mode':'inspect'}",
            ("QUOTED-PROXY-COMPACT",),
            "'mode':'inspect'",
        ),
    ],
)
def test_quoted_proxy_authorization_assignments_redact_every_scheme(
    diagnostic: str, forbidden: tuple[str, ...], preserved: str
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    for secret in forbidden:
        assert secret not in rendered
    assert "<redacted>" in rendered
    assert preserved in rendered


def test_proxy_authorization_values_are_removed_from_nested_and_failure_surfaces() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    diagnostics = [
        json.dumps(
            {
                "Proxy-Authorization": (
                    "Digest nonce=NESTED-PROXY-NONCE, response=NESTED-PROXY-RESPONSE"
                ),
                "mode": "inspect",
            },
            separators=(",", ":"),
        ),
        (
            "{'proxyAuthorization':'AWS4-HMAC-SHA256 "
            "Credential=NESTED-PROXY-CREDENTIAL, "
            "Signature=NESTED-PROXY-SIGNATURE','mode':'inspect'}"
        ),
    ]
    forbidden = (
        "NESTED-PROXY-NONCE",
        "NESTED-PROXY-RESPONSE",
        "NESTED-PROXY-CREDENTIAL",
        "NESTED-PROXY-SIGNATURE",
    )

    for diagnostic in diagnostics:
        outputs = (
            safe_json_dumps({"failure": {"message": diagnostic}}),
            json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict()),
        )
        for output in outputs:
            for secret in forbidden:
                assert secret not in output
            assert "<redacted>" in output


_AUTHORIZATION_SUFFIX_WORDS = (
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
@pytest.mark.parametrize("suffix_word", _AUTHORIZATION_SUFFIX_WORDS)
def test_explicit_authorization_headers_redact_every_same_line_suffix_parameter(
    header: str, suffix_word: str
) -> None:
    diagnostic = (
        f"{header}: CustomScheme AUTH-PRIMARY; "
        f"{suffix_word}=AUTH-{suffix_word.upper()}-TAIL\nnext diagnostic line"
    )

    rendered = sanitize_diagnostic(diagnostic)

    assert "AUTH-PRIMARY" not in rendered
    assert f"AUTH-{suffix_word.upper()}-TAIL" not in rendered
    assert rendered == f"{header}: <redacted>\nnext diagnostic line"


@pytest.mark.parametrize("suffix_word", _AUTHORIZATION_SUFFIX_WORDS)
def test_authorization_suffix_parameters_are_removed_from_nested_and_failure_surfaces(
    suffix_word: str,
) -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    canary = f"NESTED-AUTH-{suffix_word.upper()}-TAIL"
    diagnostic = (
        f"Authorization: CustomScheme NESTED-AUTH-PRIMARY; {suffix_word}={canary}"
        "\nnext diagnostic line"
    )
    outputs = (
        safe_json_dumps({"failure": {"message": diagnostic}}),
        json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict()),
    )

    for output in outputs:
        assert "NESTED-AUTH-PRIMARY" not in output
        assert canary not in output
        assert "next diagnostic line" in output
        assert "<redacted>" in output


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("Set-Cookie", "session=COOKIE-SET-HEADER; HttpOnly"),
        ("set_cookie", "session=COOKIE-SET-SNAKE; Secure"),
        ("setCookie", "session=COOKIE-SET-CAMEL; SameSite=Lax"),
        ("setcookie", "session=COOKIE-SET-COMPACT; Path=/"),
        ("headers.Set-Cookie", "session=COOKIE-QUALIFIED-DOTTED; HttpOnly"),
        ("response/set_cookie", "session=COOKIE-QUALIFIED-SLASH; Secure"),
        ("headersSetCookie", "session=COOKIE-QUALIFIED-CAMEL; Path=/"),
        ("headerssetcookie", "session=COOKIE-QUALIFIED-COMPACT; Path=/"),
        ("headers.cookie", "session=COOKIE-QUALIFIED-COOKIE"),
        ("request.cookies", "session=COOKIE-QUALIFIED-COOKIES"),
    ],
)
def test_structured_cookie_header_keys_redact_complete_values(label: str, value: str) -> None:
    rendered = safe_json_dumps({"headers": {label: value}, "mode": "inspect"})
    parsed = json.loads(rendered)

    assert value not in rendered
    assert parsed["headers"][label] == "<redacted>"
    assert parsed["mode"] == "inspect"


@pytest.mark.parametrize(
    ("diagnostic", "canary", "preserved"),
    [
        (
            '{"Set-Cookie":"session=QUOTED-COOKIE-SET; HttpOnly","mode":"inspect"}',
            "QUOTED-COOKIE-SET",
            '"mode":"inspect"',
        ),
        (
            "{'set_cookie':'session=QUOTED-COOKIE-SNAKE; Secure','mode':'inspect'}",
            "QUOTED-COOKIE-SNAKE",
            "'mode':'inspect'",
        ),
        (
            '{"setCookie":"session=QUOTED-COOKIE-CAMEL; Path=/","mode":"inspect"}',
            "QUOTED-COOKIE-CAMEL",
            '"mode":"inspect"',
        ),
        (
            "{'setcookie':'session=QUOTED-COOKIE-COMPACT; Path=/','mode':'inspect'}",
            "QUOTED-COOKIE-COMPACT",
            "'mode':'inspect'",
        ),
        (
            '{"headers.Set-Cookie":"session=QUOTED-COOKIE-QUALIFIED; HttpOnly",'
            '"mode":"inspect"}',
            "QUOTED-COOKIE-QUALIFIED",
            '"mode":"inspect"',
        ),
        (
            "{'headersSetCookie':'session=QUOTED-COOKIE-QUALIFIED-CAMEL; Secure',"
            "'mode':'inspect'}",
            "QUOTED-COOKIE-QUALIFIED-CAMEL",
            "'mode':'inspect'",
        ),
        (
            '{"headerssetcookie":"session=QUOTED-COOKIE-QUALIFIED-COMPACT; Secure",'
            '"mode":"inspect"}',
            "QUOTED-COOKIE-QUALIFIED-COMPACT",
            '"mode":"inspect"',
        ),
    ],
)
def test_quoted_cookie_header_keys_redact_complete_values(
    diagnostic: str, canary: str, preserved: str
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert canary not in rendered
    assert "<redacted>" in rendered
    assert preserved in rendered


@pytest.mark.parametrize(
    "label",
    [
        "headers.authorization",
        "request/authorization",
        "response:authorization",
        "headers_authorization",
        "request-authorization",
        "headersAuthorization",
        "headersauthorization",
        "headers.proxy_authorization",
        "request/proxy-authorization",
        "response:proxyAuthorization",
        "headersProxyAuthorization",
        "headersproxyauthorization",
    ],
)
def test_qualified_authorization_mapping_keys_redact_complete_values(label: str) -> None:
    value = f"Digest response=QUALIFIED-{label.replace('/', '-').replace(':', '-')}"
    rendered = safe_json_dumps({label: value, "mode": "inspect"})
    parsed = json.loads(rendered)

    assert value not in rendered
    assert parsed[label] == "<redacted>"
    assert parsed["mode"] == "inspect"


@pytest.mark.parametrize(
    ("diagnostic", "canary", "preserved"),
    [
        (
            '{"headers.authorization":"Digest response=QUOTED-AUTH-DOTTED","mode":"inspect"}',
            "QUOTED-AUTH-DOTTED",
            '"mode":"inspect"',
        ),
        (
            "{'request/authorization':'Custom QUOTED-AUTH-SLASH','mode':'inspect'}",
            "QUOTED-AUTH-SLASH",
            "'mode':'inspect'",
        ),
        (
            '{"response:authorization":"Basic QUOTED-AUTH-COLON","mode":"inspect"}',
            "QUOTED-AUTH-COLON",
            '"mode":"inspect"',
        ),
        (
            "{'headersAuthorization':'Digest response=QUOTED-AUTH-CAMEL','mode':'inspect'}",
            "QUOTED-AUTH-CAMEL",
            "'mode':'inspect'",
        ),
        (
            '{"headersauthorization":"Custom QUOTED-AUTH-COMPACT","mode":"inspect"}',
            "QUOTED-AUTH-COMPACT",
            '"mode":"inspect"',
        ),
        (
            "{'headers.proxyAuthorization':'Custom QUOTED-PROXY-AUTH-DOTTED','mode':'inspect'}",
            "QUOTED-PROXY-AUTH-DOTTED",
            "'mode':'inspect'",
        ),
        (
            '{"headersProxyAuthorization":"Digest response=QUOTED-PROXY-AUTH-CAMEL",'
            '"mode":"inspect"}',
            "QUOTED-PROXY-AUTH-CAMEL",
            '"mode":"inspect"',
        ),
        (
            "{'headersproxyauthorization':'Custom QUOTED-PROXY-AUTH-COMPACT','mode':'inspect'}",
            "QUOTED-PROXY-AUTH-COMPACT",
            "'mode':'inspect'",
        ),
    ],
)
def test_quoted_qualified_authorization_keys_redact_complete_values(
    diagnostic: str, canary: str, preserved: str
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert canary not in rendered
    assert "<redacted>" in rendered
    assert preserved in rendered


@pytest.mark.parametrize(
    "label",
    [
        "authorization_status",
        "headers.authorization_status",
        "authorization-guide",
        "headers.authorization_schema",
        "reauthorization",
        "cookie_policy",
        "headers.set_cookie_policy",
        "set_cookie_docs",
        "cookies_count",
    ],
)
def test_authorization_and_cookie_near_match_keys_remain_visible(label: str) -> None:
    value = "PUBLIC-DOCUMENTATION-VALUE"
    rendered = safe_json_dumps({label: value})

    assert value in rendered
    assert "<redacted>" not in rendered


def test_cookie_and_qualified_authorization_values_are_removed_from_nested_failure() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    diagnostics = [
        '{"Set-Cookie":"session=NESTED-SET-COOKIE; HttpOnly","mode":"inspect"}',
        "{'headers.setCookie':'session=NESTED-QUALIFIED-COOKIE; Secure','mode':'inspect'}",
        '{"headers.authorization":"Digest response=NESTED-QUALIFIED-AUTH","mode":"inspect"}',
        "{'headers.proxyAuthorization':'Custom NESTED-QUALIFIED-PROXY-AUTH','mode':'inspect'}",
    ]
    forbidden = (
        "NESTED-SET-COOKIE",
        "NESTED-QUALIFIED-COOKIE",
        "NESTED-QUALIFIED-AUTH",
        "NESTED-QUALIFIED-PROXY-AUTH",
    )

    for diagnostic in diagnostics:
        outputs = (
            safe_json_dumps({"failure": {"message": diagnostic}}),
            json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict()),
        )
        for output in outputs:
            for secret in forbidden:
                assert secret not in output
            assert "<redacted>" in output
            assert "inspect" in output


_COMPACT_MULTI_LEVEL_PREFIXES = (
    "requestheaders",
    "responseheaders",
    "httprequestheaders",
    "httpresponseheaders",
    "networkrequestheaders",
    "upstreamrequestheaders",
    "downstreamresponseheaders",
)
_COMPACT_HEADER_SECRET_SUFFIXES = (
    "authorization",
    "proxyauthorization",
    "setcookie",
    "cookie",
    "cookies",
)


@pytest.mark.parametrize("prefix", _COMPACT_MULTI_LEVEL_PREFIXES)
@pytest.mark.parametrize("suffix", _COMPACT_HEADER_SECRET_SUFFIXES)
def test_lowercase_compact_multi_level_header_keys_redact_structured_values(
    prefix: str, suffix: str
) -> None:
    label = prefix + suffix
    canary = f"COMPACT-{prefix.upper()}-{suffix.upper()}"
    value = (
        f"Digest response={canary}"
        if "authorization" in suffix
        else f"session={canary}; Secure"
    )

    rendered = safe_json_dumps({label: value, "mode": "inspect"})
    parsed = json.loads(rendered)

    assert canary not in rendered
    assert parsed[label] == "<redacted>"
    assert parsed["mode"] == "inspect"


@pytest.mark.parametrize(
    ("label", "value", "canary"),
    [
        (
            "requestheadersauthorization",
            "Digest response=COMPACT-QUOTED-AUTH",
            "COMPACT-QUOTED-AUTH",
        ),
        (
            "requestheadersproxyauthorization",
            "Custom COMPACT-QUOTED-PROXY-AUTH",
            "COMPACT-QUOTED-PROXY-AUTH",
        ),
        (
            "requestheaderssetcookie",
            "session=COMPACT-QUOTED-COOKIE; HttpOnly",
            "COMPACT-QUOTED-COOKIE",
        ),
    ],
)
@pytest.mark.parametrize("style", ["json", "python"])
def test_lowercase_compact_multi_level_header_keys_redact_quoted_assignments(
    label: str, value: str, canary: str, style: str
) -> None:
    diagnostic = (
        json.dumps({label: value, "mode": "inspect"}, separators=(",", ":"))
        if style == "json"
        else repr({label: value, "mode": "inspect"})
    )

    rendered = sanitize_diagnostic(diagnostic)

    assert canary not in rendered
    assert "<redacted>" in rendered
    assert "inspect" in rendered


def test_lowercase_compact_multi_level_header_values_are_removed_from_nested_failure() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    diagnostics = (
        json.dumps(
            {
                "requestheadersauthorization": ("Digest response=COMPACT-NESTED-AUTH"),
                "mode": "inspect",
            },
            separators=(",", ":"),
        ),
        repr(
            {
                "requestheadersproxyauthorization": ("Custom COMPACT-NESTED-PROXY-AUTH"),
                "mode": "inspect",
            }
        ),
        json.dumps(
            {
                "networkrequestheaderssetcookie": ("session=COMPACT-NESTED-COOKIE; Secure"),
                "mode": "inspect",
            },
            separators=(",", ":"),
        ),
    )
    forbidden = (
        "COMPACT-NESTED-AUTH",
        "COMPACT-NESTED-PROXY-AUTH",
        "COMPACT-NESTED-COOKIE",
    )

    for diagnostic in diagnostics:
        outputs = (
            safe_json_dumps({"failure": {"message": diagnostic}}),
            json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict()),
        )
        for output in outputs:
            for secret in forbidden:
                assert secret not in output
            assert "<redacted>" in output
            assert "inspect" in output


@pytest.mark.parametrize(
    "label",
    [
        "reauthorization",
        "authorizationstatus",
        "marketingcookie",
        "setcookiedocs",
        "cookiescount",
        "marketingrequestheadersauthorization",
        "requestheadermetadataauthorization",
    ],
)
def test_lowercase_compact_header_near_matches_remain_visible(label: str) -> None:
    value = "PUBLIC-COMPACT-HEADER-VALUE"
    rendered = safe_json_dumps({label: value})

    assert value in rendered
    assert "<redacted>" not in rendered


@pytest.mark.parametrize(
    ("diagnostic", "forbidden", "expected"),
    [
        (
            "Authorization: Digest nonce=FOLDED-PRIMARY\r\n"
            " response=FOLDED-CONTINUATION\r\n"
            "X-Status: failed",
            ("FOLDED-PRIMARY", "FOLDED-CONTINUATION"),
            "Authorization: <redacted>\r\nX-Status: failed",
        ),
        (
            "Authorization: Custom FOLDED-LF-PRIMARY\n"
            "\trealm=FOLDED-LF-CONTINUATION\n"
            "next diagnostic line",
            ("FOLDED-LF-PRIMARY", "FOLDED-LF-CONTINUATION"),
            "Authorization: <redacted>\nnext diagnostic line",
        ),
        (
            "Proxy-Authorization: Digest FOLDED-MULTI-PRIMARY\r\n"
            " nonce=FOLDED-MULTI-NONCE\r\n"
            "\tresponse=FOLDED-MULTI-RESPONSE\r\n"
            "X-Trace: visible",
            (
                "FOLDED-MULTI-PRIMARY",
                "FOLDED-MULTI-NONCE",
                "FOLDED-MULTI-RESPONSE",
            ),
            "Proxy-Authorization: <redacted>\r\nX-Trace: visible",
        ),
        (
            "authorization = Custom FOLDED-END-PRIMARY\n arbitrary=FOLDED-END-CONTINUATION",
            ("FOLDED-END-PRIMARY", "FOLDED-END-CONTINUATION"),
            "authorization = <redacted>",
        ),
    ],
)
def test_folded_authorization_continuations_are_part_of_the_secret_value(
    diagnostic: str, forbidden: tuple[str, ...], expected: str
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    for secret in forbidden:
        assert secret not in rendered
    assert rendered == expected


def test_folded_authorization_continuations_are_removed_from_nested_failure() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    diagnostic = (
        "Authorization: Digest nonce=FOLDED-NESTED-PRIMARY\r\n"
        " response=FOLDED-NESTED-RESPONSE\r\n"
        " realm=FOLDED-NESTED-REALM\r\n"
        "X-Status: visible"
    )
    outputs = (
        safe_json_dumps({"failure": {"message": diagnostic}}),
        json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict()),
    )

    for output in outputs:
        for secret in (
            "FOLDED-NESTED-PRIMARY",
            "FOLDED-NESTED-RESPONSE",
            "FOLDED-NESTED-REALM",
        ):
            assert secret not in output
        assert "X-Status: visible" in output
        assert "<redacted>" in output


@pytest.mark.parametrize(
    ("diagnostic", "decoded_key", "canary"),
    [
        (
            '{"headers[Authorization]":"Digest response=QUOTED-BRACKET-AUTH","mode":"inspect"}',
            "headers[Authorization]",
            "QUOTED-BRACKET-AUTH",
        ),
        (
            '{"headers\\/authorization":"Digest response=QUOTED-ESCAPED-SLASH",'
            '"mode":"inspect"}',
            "headers/authorization",
            "QUOTED-ESCAPED-SLASH",
        ),
        (
            '{"Authoriz\\u0061tion":"Digest response=QUOTED-UNICODE-AUTH","mode":"inspect"}',
            "Authorization",
            "QUOTED-UNICODE-AUTH",
        ),
        (
            '{" request.authorization ":"Digest response=QUOTED-PADDED-AUTH","mode":"inspect"}',
            " request.authorization ",
            "QUOTED-PADDED-AUTH",
        ),
        (
            "{'headers[Proxy-Authorization]':'Custom QUOTED-PYTHON-BRACKET','mode':'inspect'}",
            "headers[Proxy-Authorization]",
            "QUOTED-PYTHON-BRACKET",
        ),
    ],
)
def test_canonical_quoted_header_keys_match_real_mapping_normalization(
    diagnostic: str, decoded_key: str, canary: str
) -> None:
    mapping_rendered = safe_json_dumps({decoded_key: f"Custom {canary}", "mode": "inspect"})
    diagnostic_rendered = sanitize_diagnostic(diagnostic)

    assert canary not in mapping_rendered
    assert canary not in diagnostic_rendered
    assert "<redacted>" in mapping_rendered
    assert "<redacted>" in diagnostic_rendered
    assert "inspect" in mapping_rendered
    assert "inspect" in diagnostic_rendered


def test_long_approved_quoted_header_key_remains_bounded_and_canonical() -> None:
    key = ("request." * 20) + "headers.authorization"
    assert 100 < len(key) <= 256
    diagnostic = json.dumps(
        {key: "Digest response=QUOTED-LONG-AUTH", "mode": "inspect"},
        separators=(",", ":"),
    )

    rendered = sanitize_diagnostic(diagnostic)
    parsed = json.loads(rendered)

    assert "QUOTED-LONG-AUTH" not in rendered
    assert parsed[key] == "<redacted>"
    assert parsed["mode"] == "inspect"


def test_maximum_bound_quoted_header_key_is_classified() -> None:
    base = "headers.authorization"
    key = (" " * (256 - len(base))) + base
    assert len(key) == 256
    diagnostic = json.dumps(
        {key: "Digest response=QUOTED-MAX-AUTH", "mode": "inspect"},
        separators=(",", ":"),
    )

    rendered = sanitize_diagnostic(diagnostic)
    parsed = json.loads(rendered)

    assert "QUOTED-MAX-AUTH" not in rendered
    assert parsed[key] == "<redacted>"
    assert parsed["mode"] == "inspect"


def test_over_bound_quoted_header_key_fails_closed() -> None:
    base = "headers.authorization"
    key = (" " * (257 - len(base))) + base
    assert len(key) == 257
    diagnostic = json.dumps(
        {key: "Digest response=QUOTED-OVERBOUND-AUTH", "mode": "inspect"},
        separators=(",", ":"),
    )

    rendered = sanitize_diagnostic(diagnostic)

    assert rendered == "<redacted>"
    assert "QUOTED-OVERBOUND-AUTH" not in rendered


def test_over_bound_real_mapping_key_fails_closed_for_its_value() -> None:
    base = "headers.authorization"
    key = (" " * (257 - len(base))) + base
    rendered = safe_json_dumps(
        {key: "Digest response=MAPPING-OVERBOUND-AUTH", "mode": "inspect"}
    )

    assert "MAPPING-OVERBOUND-AUTH" not in rendered
    assert "<redacted>" in rendered
    assert "inspect" in rendered
    assert isinstance(json.loads(rendered), dict)


@pytest.mark.parametrize(
    ("diagnostic", "canary"),
    [
        (
            r'{"Authoriz\qtion":"MALFORMED-ESCAPE-AUTH","mode":"inspect"}',
            "MALFORMED-ESCAPE-AUTH",
        ),
        (
            r'{"Authoriz\u000ation":"CONTROL-ESCAPE-AUTH","mode":"inspect"}',
            "CONTROL-ESCAPE-AUTH",
        ),
        (
            "{'Authorization\":'MISMATCHED-QUOTE-AUTH','mode':'inspect'}",
            "MISMATCHED-QUOTE-AUTH",
        ),
    ],
)
def test_malformed_quoted_assignment_keys_fail_closed(diagnostic: str, canary: str) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert rendered == "<redacted>"
    assert canary not in rendered


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("marketing_cookie", "PUBLIC-MARKETING-COOKIE"),
        ("analytics_cookie", "PUBLIC-ANALYTICS-COOKIE"),
        ("customer_cookies", "PUBLIC-CUSTOMER-COOKIES"),
        ("legal_authorization", "PUBLIC-LEGAL-AUTHORIZATION"),
        ("user_authorization", "PUBLIC-USER-AUTHORIZATION"),
        ("feature_authorization", "PUBLIC-FEATURE-AUTHORIZATION"),
        ("marketingCookie", "PUBLIC-MARKETING-CAMEL"),
        ("legalAuthorization", "PUBLIC-LEGAL-CAMEL"),
    ],
)
def test_ordinary_separated_authorization_and_cookie_fields_remain_visible(
    label: str, value: str
) -> None:
    mapping_rendered = safe_json_dumps({label: value})
    json_diagnostic = json.dumps({label: value, "mode": "inspect"}, separators=(",", ":"))
    python_diagnostic = repr({label: value, "mode": "inspect"})

    assert value in mapping_rendered
    assert value in sanitize_diagnostic(json_diagnostic)
    assert value in sanitize_diagnostic(python_diagnostic)
    assert "<redacted>" not in mapping_rendered


@pytest.mark.parametrize(
    "label",
    [
        "headers.authorization",
        "request.authorization",
        "response:authorization",
        "request.cookies",
        "headers.proxy_authorization",
        "response/set_cookie",
        "request.headers.authorization",
        "requestHeadersAuthorization",
        "network.request.headers.set_cookie",
    ],
)
def test_approved_separated_header_contexts_remain_secret(label: str) -> None:
    canary = "APPROVED-SEPARATED-HEADER-SECRET"
    value = (
        f"session={canary}; Secure"
        if "cookie" in label.casefold()
        else f"Digest response={canary}"
    )
    mapping_rendered = safe_json_dumps({label: value, "mode": "inspect"})
    diagnostic = json.dumps({label: value, "mode": "inspect"}, separators=(",", ":"))
    diagnostic_rendered = sanitize_diagnostic(diagnostic)

    assert canary not in mapping_rendered
    assert canary not in diagnostic_rendered
    assert "<redacted>" in mapping_rendered
    assert "<redacted>" in diagnostic_rendered
    assert "inspect" in mapping_rendered
    assert "inspect" in diagnostic_rendered


_MISSING_CLOSE_CANONICAL_SECRET_KEYS = [
    (r'{"Authoriz\u0061tion: DIRECT-MISSING-CLOSE-AUTH', "DIRECT-MISSING-CLOSE-AUTH"),
    (
        r'{"Proxy-Authoriz\u0061tion: DIRECT-MISSING-CLOSE-PROXY',
        "DIRECT-MISSING-CLOSE-PROXY",
    ),
    (r'{"Set-Cook\u0069e: DIRECT-MISSING-CLOSE-COOKIE', "DIRECT-MISSING-CLOSE-COOKIE"),
    (
        r'{"headers[Authoriz\u0061tion]: DIRECT-MISSING-CLOSE-BRACKET',
        "DIRECT-MISSING-CLOSE-BRACKET",
    ),
    (
        r'{"headers\u005bAuthorization\u005d: DIRECT-MISSING-CLOSE-ESCAPED-BRACKET',
        "DIRECT-MISSING-CLOSE-ESCAPED-BRACKET",
    ),
    (
        r'{"request\u002eheaders\u002eauthorization: DIRECT-MISSING-CLOSE-DOTTED',
        "DIRECT-MISSING-CLOSE-DOTTED",
    ),
    (r"{'Authoriz\u0061tion: DIRECT-MISSING-CLOSE-SINGLE", "DIRECT-MISSING-CLOSE-SINGLE"),
]


@pytest.mark.parametrize(("diagnostic", "canary"), _MISSING_CLOSE_CANONICAL_SECRET_KEYS)
def test_missing_close_canonical_escaped_secret_keys_fail_closed(
    diagnostic: str, canary: str
) -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    outputs = (
        sanitize_diagnostic(diagnostic),
        safe_json_dumps({"failure": {"message": diagnostic}}),
        json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict()),
    )

    for output in outputs:
        assert output == "<redacted>" or "<redacted>" in output
        assert canary not in output


def test_missing_close_canonical_nonsecret_key_remains_visible() -> None:
    diagnostic = r'{"mode\u002ename: PUBLIC-MISSING-CLOSE-CONTEXT'

    assert sanitize_diagnostic(diagnostic) == diagnostic


_MISSING_CLOSE_INTERNAL_DELIMITER_CASES = [
    (
        f"{{{quote}{qualifier}{internal_separator}{secret_suffix}: "
        f"INTERNAL-DELIMITER-{case_index}",
        f"INTERNAL-DELIMITER-{case_index}",
    )
    for case_index, (qualifier, secret_suffix, internal_separator, quote) in enumerate(
        (
            (qualifier, secret_suffix, internal_separator, quote)
            for qualifier in (
                "headers",
                "request",
                "response",
                "request:headers",
                "network:request:headers",
                "upstream:request:headers",
                "downstream:response:headers",
            )
            for secret_suffix in (
                r"Authoriz\u0061tion",
                r"Proxy-Authoriz\u0061tion",
                r"Set-Cook\u0069e",
            )
            for internal_separator in (":", "=")
            for quote in ('"', "'")
        )
    )
]


@pytest.mark.parametrize(("diagnostic", "canary"), _MISSING_CLOSE_INTERNAL_DELIMITER_CASES)
def test_missing_close_internal_delimiters_do_not_hide_canonical_secret_components(
    diagnostic: str, canary: str
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert rendered == "<redacted>"
    assert canary not in rendered


@pytest.mark.parametrize(
    ("diagnostic", "canary"),
    [
        (
            r'{"headers:Authoriz\u0061tion: NESTED-INTERNAL-AUTH',
            "NESTED-INTERNAL-AUTH",
        ),
        (
            r"{'request:headers:Proxy-Authoriz\u0061tion: NESTED-INTERNAL-PROXY",
            "NESTED-INTERNAL-PROXY",
        ),
        (
            r'{"response=Set-Cook\u0069e: NESTED-INTERNAL-COOKIE',
            "NESTED-INTERNAL-COOKIE",
        ),
    ],
)
def test_missing_close_internal_delimiter_secrets_are_removed_from_nested_failure(
    diagnostic: str, canary: str
) -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    outputs = (
        safe_json_dumps({"failure": {"message": diagnostic}}),
        json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict()),
    )

    for output in outputs:
        assert canary not in output
        assert "<redacted>" in output


def test_missing_close_internal_delimiter_nonsecret_key_remains_visible() -> None:
    diagnostic = r'{"mode:display\u002ename: PUBLIC-INTERNAL-CONTEXT'

    assert sanitize_diagnostic(diagnostic) == diagnostic


_ESCAPED_DIAGNOSTIC_VALUE_CASES = [
    (
        f'{{"message":"{label}{delimiter} {value}","mode":"inspect"}}',
        canary,
    )
    for label, value, canary in (
        ("Authorization", "Custom JSON-VALUE-AUTH", "JSON-VALUE-AUTH"),
        (r"Authoriz\u0061tion", "Custom JSON-VALUE-UNICODE-AUTH", "JSON-VALUE-UNICODE-AUTH"),
        (
            r"Proxy-Authoriz\u0061tion",
            "Digest JSON-VALUE-PROXY",
            "JSON-VALUE-PROXY",
        ),
        (
            r"Set-Cook\u0069e",
            "session=JSON-VALUE-SET-COOKIE; Secure",
            "JSON-VALUE-SET-COOKIE",
        ),
        ("Cookie", "session=JSON-VALUE-COOKIE; Secure", "JSON-VALUE-COOKIE"),
    )
    for delimiter in (r"\u003a", r"\u003d")
]


@pytest.mark.parametrize(("diagnostic", "canary"), _ESCAPED_DIAGNOSTIC_VALUE_CASES)
def test_valid_json_escaped_diagnostic_values_are_canonically_sanitized(
    diagnostic: str, canary: str
) -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    direct = sanitize_diagnostic(diagnostic)
    nested = safe_json_dumps({"failure": {"message": diagnostic}})
    failure = json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict())

    assert isinstance(json.loads(direct), dict)
    for output in (direct, nested, failure):
        assert canary not in output
        assert "<redacted>" in output
        assert "inspect" in output


@pytest.mark.parametrize(
    "diagnostic",
    [
        r'{"message":"legal_authorization\u003a allowed","mode":"inspect"}',
        r'{"message":"marketing_cookie\u003a enabled","mode":"inspect"}',
        r'{"message":"authorization_status\u003a denied","mode":"inspect"}',
    ],
)
def test_valid_json_escaped_ordinary_diagnostic_values_remain_visible(
    diagnostic: str,
) -> None:
    rendered = sanitize_diagnostic(diagnostic)
    parsed = json.loads(rendered)

    assert parsed["mode"] == "inspect"
    assert parsed["message"].split()[-1] in {"allowed", "enabled", "denied"}
    assert "<redacted>" not in rendered


@pytest.mark.parametrize(
    ("diagnostic", "canary"),
    [
        (
            r'{"Authoriz\u0061tion\u003a FULLY-ESCAPED-MALFORMED-AUTH',
            "FULLY-ESCAPED-MALFORMED-AUTH",
        ),
        (
            r'{"headers\u003aAuthoriz\u0061tion\u003a FULLY-ESCAPED-MALFORMED-QUALIFIED',
            "FULLY-ESCAPED-MALFORMED-QUALIFIED",
        ),
        (
            r'{"Set-Cook\u0069e\u003a session=FULLY-ESCAPED-MALFORMED-COOKIE',
            "FULLY-ESCAPED-MALFORMED-COOKIE",
        ),
    ],
)
def test_missing_close_fully_escaped_delimiters_fail_closed(
    diagnostic: str, canary: str
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert rendered == "<redacted>"
    assert canary not in rendered


def test_valid_json_quoted_value_decoder_handles_slash_quote_and_backslash_escapes() -> None:
    diagnostic = (
        r'{"message":"Authorization\u003a Custom ESCAPED\/SLASH'
        r'\\BACKSLASH\"QUOTE","mode":"inspect"}'
    )

    rendered = sanitize_diagnostic(diagnostic)
    parsed = json.loads(rendered)

    assert parsed["mode"] == "inspect"
    assert parsed["message"] == "Authorization: <redacted>"
    assert "ESCAPED" not in rendered
    assert "SLASH" not in rendered
    assert "BACKSLASH" not in rendered
    assert "QUOTE" not in rendered


_RECURSIVE_STRUCTURED_VALUE_CASES = [
    (
        json.dumps(
            {
                "message": json.dumps(
                    {"message": value, "mode": "inner"},
                    separators=(",", ":"),
                ),
                "mode": "outer",
            },
            separators=(",", ":"),
        ),
        canary,
    )
    for value, canary in (
        (r"Authorization\u003a Custom RECURSIVE-INNER-AUTH", "RECURSIVE-INNER-AUTH"),
        (
            r"Proxy-Authoriz\u0061tion\u003a Digest RECURSIVE-INNER-PROXY",
            "RECURSIVE-INNER-PROXY",
        ),
        (
            r"Set-Cook\u0069e\u003a session=RECURSIVE-INNER-COOKIE; Secure",
            "RECURSIVE-INNER-COOKIE",
        ),
    )
] + [
    (
        json.dumps({"message": value, "mode": "outer"}, separators=(",", ":")),
        canary,
    )
    for value, canary in (
        (r"Authorization\u003a Custom DOUBLE-ESCAPED-AUTH", "DOUBLE-ESCAPED-AUTH"),
        (
            r"Proxy-Authorization\u003a Digest DOUBLE-ESCAPED-PROXY",
            "DOUBLE-ESCAPED-PROXY",
        ),
        (
            r"Set-Cookie\u003a session=DOUBLE-ESCAPED-COOKIE; Secure",
            "DOUBLE-ESCAPED-COOKIE",
        ),
    )
]


@pytest.mark.parametrize(("diagnostic", "canary"), _RECURSIVE_STRUCTURED_VALUE_CASES)
def test_nested_and_double_escaped_structured_values_are_sanitized_recursively(
    diagnostic: str, canary: str
) -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    direct = sanitize_diagnostic(diagnostic)
    nested = safe_json_dumps({"failure": {"message": diagnostic}})
    failure = json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict())

    assert isinstance(json.loads(direct), dict)
    for output in (direct, nested, failure):
        assert canary not in output
        assert "<redacted>" in output
        assert "outer" in output


@pytest.mark.parametrize(
    "diagnostic",
    [
        json.dumps(
            {"message": r"legal_authorization\u003a allowed", "mode": "outer"},
            separators=(",", ":"),
        ),
        json.dumps(
            {"message": r"marketing_cookie\u003a enabled", "mode": "outer"},
            separators=(",", ":"),
        ),
        json.dumps(
            {"message": r"authorization_status\u003a denied", "mode": "outer"},
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "message": json.dumps(
                    {
                        "message": r"legal_authorization\u003a allowed",
                        "mode": "inner",
                    },
                    separators=(",", ":"),
                ),
                "mode": "outer",
            },
            separators=(",", ":"),
        ),
    ],
)
def test_nested_and_double_escaped_ordinary_controls_remain_visible(
    diagnostic: str,
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert isinstance(json.loads(rendered), dict)
    assert "<redacted>" not in rendered
    assert "outer" in rendered


@pytest.mark.parametrize(
    ("diagnostic", "canary", "safe_sibling"),
    [
        (
            r'["Authorization\u003a Custom ARRAY-ROOT-AUTH","safe-root"]',
            "ARRAY-ROOT-AUTH",
            "safe-root",
        ),
        (
            r'{"items":["Proxy-Authoriz\u0061tion\u003a Digest ARRAY-NESTED-PROXY",'
            r'"safe-proxy"],"mode":"inspect"}',
            "ARRAY-NESTED-PROXY",
            "safe-proxy",
        ),
        (
            r'{"items":["Set-Cook\u0069e\u003a session=ARRAY-NESTED-COOKIE; Secure",'
            r'"safe-cookie"],"mode":"inspect"}',
            "ARRAY-NESTED-COOKIE",
            "safe-cookie",
        ),
    ],
)
def test_json_array_string_values_preserve_structure_and_safe_siblings(
    diagnostic: str, canary: str, safe_sibling: str
) -> None:
    rendered = sanitize_diagnostic(diagnostic)
    parsed = json.loads(rendered)

    assert canary not in rendered
    assert "<redacted>" in rendered
    assert safe_sibling in rendered
    assert isinstance(parsed, (dict, list))


def test_recursive_structured_depth_budget_fails_closed_without_leaking() -> None:
    canary = "RECURSIVE-DEPTH-BUDGET-AUTH"
    payload: object = {
        "message": rf"Authorization\u003a Custom {canary}",
        "mode": "inner",
    }
    for depth in range(14):
        payload = {"child": payload, "mode": f"outer-{depth}"}
    diagnostic = json.dumps(payload, separators=(",", ":"))

    rendered = sanitize_diagnostic(diagnostic)

    assert canary not in rendered
    assert rendered == "<redacted>"


def test_json_array_secret_values_remain_safe_across_nested_failure_boundaries() -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    diagnostic = r'["Authorization\u003a Custom ARRAY-PUBLIC-AUTH","safe-public"]'
    outputs = (
        sanitize_diagnostic(diagnostic),
        safe_json_dumps({"failure": {"message": diagnostic}}),
        json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict()),
    )

    assert isinstance(json.loads(outputs[0]), list)
    for output in outputs:
        assert "ARRAY-PUBLIC-AUTH" not in output
        assert "<redacted>" in output
        assert "safe-public" in output


def _repeat_json_escape_layer(value: str, layers: int) -> str:
    for _ in range(layers - 1):
        value = value.replace("\\", "\\\\")
    return value


_JSON_STRING_ROOT_SECRET_CASES = [
    (source, canary)
    for base, canary_prefix in (
        (r"Authorization\u003a Custom", "ROOT-AUTH"),
        (r"Proxy-Authorization\u003a Digest", "ROOT-PROXY"),
        (r"Set-Cookie\u003a session=", "ROOT-COOKIE"),
    )
    for source, canary in (
        (
            f'"{base} {canary_prefix}-IMMEDIATE"',
            f"{canary_prefix}-IMMEDIATE",
        ),
        (
            json.dumps(
                f"{base} {canary_prefix}-REMAINING",
                separators=(",", ":"),
            ),
            f"{canary_prefix}-REMAINING",
        ),
    )
]


@pytest.mark.parametrize(("diagnostic", "canary"), _JSON_STRING_ROOT_SECRET_CASES)
def test_json_string_roots_are_recursively_sanitized_and_remain_valid_json(
    diagnostic: str, canary: str
) -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    direct = sanitize_diagnostic(diagnostic)
    nested = safe_json_dumps({"failure": {"message": diagnostic}})
    failure = json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict())

    assert isinstance(json.loads(direct), str)
    for output in (direct, nested, failure):
        assert canary not in output
        assert "<redacted>" in output


@pytest.mark.parametrize(
    "diagnostic",
    [
        json.dumps(r"legal_authorization\u003a allowed"),
        json.dumps(r"marketing_cookie\u003a enabled"),
        json.dumps(r"authorization_status\u003a denied"),
    ],
)
def test_json_string_root_ordinary_controls_remain_valid_and_visible(
    diagnostic: str,
) -> None:
    rendered = sanitize_diagnostic(diagnostic)

    assert json.loads(rendered).split()[-1] in {"allowed", "enabled", "denied"}
    assert "<redacted>" not in rendered


@pytest.mark.parametrize("layers", range(1, 7))
def test_json_string_root_escape_depths_are_bounded_and_secret_safe(layers: int) -> None:
    canary = f"ROOT-DEPTH-{layers}"
    value = _repeat_json_escape_layer(
        rf"Authorization\u003a Custom {canary}",
        layers,
    )
    diagnostic = json.dumps(value)

    rendered = sanitize_diagnostic(diagnostic)

    assert canary not in rendered
    assert "<redacted>" in rendered
    assert isinstance(json.loads(rendered), str)


_JSON_OBJECT_ESCAPED_KEY_CASES = [
    (
        json.dumps(
            {
                _repeat_json_escape_layer(base_key, layers): canary,
                "mode": "inspect",
            },
            separators=(",", ":"),
        ),
        canary,
        layers,
    )
    for base_key, prefix in (
        (r"Authoriz\u0061tion", "KEY-AUTH"),
        (r"Proxy-Authoriz\u0061tion", "KEY-PROXY"),
        (r"Cook\u0069e", "KEY-COOKIE"),
        (r"Set-Cook\u0069e", "KEY-SET-COOKIE"),
    )
    for layers in range(1, 7)
    for canary in (f"{prefix}-DEPTH-{layers}",)
]


@pytest.mark.parametrize(
    ("diagnostic", "canary", "layers"),
    _JSON_OBJECT_ESCAPED_KEY_CASES,
)
def test_json_object_keys_are_canonicalized_across_bounded_escape_layers(
    diagnostic: str, canary: str, layers: int
) -> None:
    from playwright_gpt_core.errors import Failure, FailureCategory

    direct = sanitize_diagnostic(diagnostic)
    nested = safe_json_dumps({"failure": {"message": diagnostic}})
    failure = json.dumps(Failure(FailureCategory.INVARIANT, diagnostic).to_dict())
    parsed = json.loads(direct)

    assert parsed["mode"] == "inspect"
    assert canary not in direct
    assert "<redacted>" in direct
    assert str(layers) in canary
    for output in (nested, failure):
        assert canary not in output
        assert "<redacted>" in output


@pytest.mark.parametrize(
    "key",
    [
        r"legal_authoriz\u0061tion",
        r"marketing_cook\u0069e",
        r"authoriz\u0061tion_status",
    ],
)
def test_json_object_preescaped_ordinary_keys_remain_visible(key: str) -> None:
    diagnostic = json.dumps({key: "visible", "mode": "inspect"}, separators=(",", ":"))
    rendered = sanitize_diagnostic(diagnostic)
    parsed = json.loads(rendered)

    assert parsed[key] == "visible"
    assert parsed["mode"] == "inspect"
    assert "<redacted>" not in rendered


def _compact_unicode_escape_layers(codepoint: str, layers: int) -> str:
    escaped = rf"\u{codepoint}"
    for _ in range(layers - 1):
        escaped = r"\u005c" + escaped[1:]
    return escaped


@pytest.mark.parametrize("layers", range(1, 7))
def test_json_string_root_safe_controls_hold_across_escape_depths(layers: int) -> None:
    value = "legal_authorization" + _compact_unicode_escape_layers("003a", layers) + " allowed"
    diagnostic = json.dumps(value)

    rendered = sanitize_diagnostic(diagnostic)

    assert json.loads(rendered) == value
    assert "<redacted>" not in rendered


@pytest.mark.parametrize("layers", range(1, 7))
def test_json_object_safe_keys_hold_across_escape_depths(layers: int) -> None:
    key = "legal_authoriz" + _compact_unicode_escape_layers("0061", layers) + "tion_status"
    diagnostic = json.dumps({key: "visible", "mode": "inspect"}, separators=(",", ":"))

    rendered = sanitize_diagnostic(diagnostic)
    parsed = json.loads(rendered)

    assert parsed[key] == "visible"
    assert parsed["mode"] == "inspect"
    assert "<redacted>" not in rendered


def test_json_string_root_over_depth_fails_closed_as_valid_json() -> None:
    canary = "ROOT-OVER-DEPTH-AUTH"
    value = "Authorization" + _compact_unicode_escape_layers("003a", 13) + f" Custom {canary}"

    rendered = sanitize_diagnostic(json.dumps(value))

    assert json.loads(rendered) == "<redacted>"
    assert canary not in rendered


def test_json_object_key_over_depth_fails_closed() -> None:
    canary = "KEY-OVER-DEPTH-AUTH"
    key = "Authoriz" + _compact_unicode_escape_layers("0061", 13) + "tion"
    diagnostic = json.dumps({key: canary, "mode": "inspect"}, separators=(",", ":"))

    rendered = sanitize_diagnostic(diagnostic)

    assert rendered == "<redacted>"
    assert canary not in rendered


def test_json_object_key_malformed_remaining_unicode_escape_fails_closed() -> None:
    canary = "KEY-MALFORMED-REMAINING-AUTH"
    diagnostic = json.dumps(
        {r"Authoriz\uZZZZtion": canary, "mode": "inspect"},
        separators=(",", ":"),
    )

    rendered = sanitize_diagnostic(diagnostic)

    assert rendered == "<redacted>"
    assert canary not in rendered


_NATIVE_MAPPING_ESCAPED_KEY_CASES = [
    (
        prefix + _compact_unicode_escape_layers(codepoint, layers) + suffix,
        f"NATIVE-{label}-DEPTH-{layers}",
        layers,
    )
    for prefix, codepoint, suffix, label in (
        ("Authoriz", "0061", "tion", "AUTH"),
        ("Proxy-Authoriz", "0061", "tion", "PROXY"),
        ("Cook", "0069", "e", "COOKIE"),
        ("Set-Cook", "0069", "e", "SET-COOKIE"),
    )
    for layers in range(1, 7)
]


@pytest.mark.parametrize(
    ("key", "canary", "layers"),
    _NATIVE_MAPPING_ESCAPED_KEY_CASES,
)
def test_native_mapping_keys_use_canonical_escape_layer_classification(
    key: str, canary: str, layers: int
) -> None:
    cleaned = redact({key: canary, "mode": "inspect"})
    rendered = safe_json_dumps({key: canary, "mode": "inspect"})
    parsed = json.loads(rendered)

    assert cleaned[key] == "<redacted>"
    assert cleaned["mode"] == "inspect"
    assert parsed[key] == "<redacted>"
    assert parsed["mode"] == "inspect"
    assert canary not in rendered
    assert str(layers) in canary


@pytest.mark.parametrize("layers", range(1, 7))
def test_native_mapping_safe_keys_hold_across_escape_depths(layers: int) -> None:
    key = "legal_authoriz" + _compact_unicode_escape_layers("0061", layers) + "tion_status"

    cleaned = redact({key: "visible", "mode": "inspect"})
    rendered = safe_json_dumps({key: "visible", "mode": "inspect"})

    assert cleaned[key] == "visible"
    assert cleaned["mode"] == "inspect"
    assert json.loads(rendered)[key] == "visible"
    assert "<redacted>" not in rendered


@pytest.mark.parametrize(
    "key",
    [
        r"Authoriz\uZZZZtion",
        "Authoriz\x61tion",
        "Authoriz\\",
        "Authoriz\x00ation".replace("\\x00", "\x00"),
        "x" * 257,
        "Authoriz" + _compact_unicode_escape_layers("0061", 13) + "tion",
    ],
)
def test_native_mapping_unprovable_keys_fail_closed_for_associated_value(
    key: str,
) -> None:
    canary = "NATIVE-UNPROVABLE-KEY-CANARY"

    cleaned = redact({key: canary, "mode": "inspect"})
    rendered = safe_json_dumps({key: canary, "mode": "inspect"})

    assert canary not in cleaned.values()
    assert canary not in rendered
    assert "<redacted>" in cleaned.values()
    assert "<redacted>" in rendered
    assert cleaned["mode"] == "inspect"


def test_native_mapping_non_string_keys_preserve_existing_coercion_policy() -> None:
    class DirectSecretKey:
        def __str__(self) -> str:
            return "authorization"

    visible = redact({7: "visible"})
    secret = redact({DirectSecretKey(): "hidden"})

    assert visible == {"7": "visible"}
    assert secret == {"authorization": "<redacted>"}
