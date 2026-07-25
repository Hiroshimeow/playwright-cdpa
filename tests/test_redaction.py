from __future__ import annotations

import json

from playwright_gpt_core.redaction import redact, safe_json_dumps


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
