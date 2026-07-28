from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MARKDOWN = [
    ROOT / "README.md",
    ROOT / "reference" / "README.md",
    *sorted((ROOT / "docs").rglob("*.md")),
]
LONG_HEX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{24,64}(?![0-9A-Fa-f])")
UUID = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}"
    r"[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
)


def _allowed_checksums() -> set[str]:
    checksum_file = ROOT / "reference" / "SHA256SUMS"
    return {
        line.split()[0].casefold()
        for line in checksum_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _runtime_identifier_findings(text: str, allowed_checksums: set[str]) -> list[str]:
    findings = [f"UUID {match.group(0)}" for match in UUID.finditer(text)]
    for match in LONG_HEX.finditer(text):
        value = match.group(0)
        if value.casefold() in allowed_checksums:
            continue
        findings.append(f"long identifier {value}")
    return findings


def test_public_markdown_contains_only_truncated_runtime_identifiers() -> None:
    allowed_checksums = _allowed_checksums()
    findings: list[str] = []

    for path in PUBLIC_MARKDOWN:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            findings.extend(
                f"{path.relative_to(ROOT)}:{line_number}: {finding}"
                for finding in _runtime_identifier_findings(line, allowed_checksums)
            )

    assert findings == [], "public Markdown must truncate runtime identifiers:\n" + "\n".join(
        findings
    )


@pytest.mark.parametrize(
    "value",
    [
        "550e8400-e29b-41d4-a716-446655440000",
        "1ef21d2f-6b2a-6cc4-8c7a-0123456789ab",
        "018f47a2-7c89-7abc-8def-0123456789ab",
        "00000000-0000-0000-0000-000000000000",
        "550E8400-E29B-41D4-A716-446655440000",
        "0123456789abcdef0123456789abcdef",
    ],
)
def test_runtime_identifier_detector_rejects_full_shapes(value: str) -> None:
    assert _runtime_identifier_findings(value, set())


@pytest.mark.parametrize(
    "value",
    [
        "3c2de378…2dd0",
        "3c2de378...2dd0",
        "cdp-ccd7d89f…7662",
        "request-prefix-3c2de378",
    ],
)
def test_runtime_identifier_detector_allows_truncated_shapes(value: str) -> None:
    assert _runtime_identifier_findings(value, set()) == []


def test_runtime_identifier_detector_allows_only_exact_declared_checksum() -> None:
    checksum = next(iter(_allowed_checksums()))
    altered_checksum = ("0" if checksum[0] != "0" else "1") + checksum[1:]

    assert _runtime_identifier_findings(checksum, {checksum}) == []
    assert _runtime_identifier_findings(altered_checksum, {checksum})


def test_cdpa_adapter_keeps_operator_continuation_on_same_hop_identity() -> None:
    text = (ROOT / "docs" / "cdpa-adapter.md").read_text(encoding="utf-8")

    assert "request_id=<new exact hop request id>" not in text
    assert "same CDPA hop request ID" in text
    assert "legacy response waiting" in text
    assert "same-request continuation" in text


def test_public_docs_define_request_identity_endpoint_and_no_aliases() -> None:
    combined = "\n".join(
        [
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "live-facts-and-assumptions.md").read_text(encoding="utf-8"),
        ]
    )

    assert "1-160 ASCII letters, digits, dot, underscore, or hyphen" in combined
    assert "exact HTTPS ChatGPT conversation endpoint" in combined
    assert "No retired command or library aliases are retained" in combined
    assert "temporary command-line aliases" not in combined
    assert "zero-logic library aliases" not in combined


def test_helper_lifecycle_documents_manual_state_preservation_as_internal_only() -> None:
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    assert "durably preserves the exact owned page" in text
    assert "manual text, attachments, or a choice prompt" in text
    assert "not caller-controlled" in text
    assert "There is no public helper-tab policy option" in text


def test_request_id_recovery_scope_requires_one_result_root() -> None:
    combined = "\n".join(
        [
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "cdpa-adapter.md").read_text(encoding="utf-8"),
        ]
    )

    assert "same logical request must use the same result state root" in combined
    assert "coordination is not a cross-repository result ledger" in combined


def test_docs_define_state_aware_result_taxonomy() -> None:
    combined = "\n".join(
        [
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8"),
            (ROOT / "docs" / "cdpa-adapter.md").read_text(encoding="utf-8"),
        ]
    )

    assert "`UNKNOWN` always exposes `get_required`" in combined
    assert "nested failure remains the cause" in combined
    assert "terminal pre-click `FAILED` timeout" in combined
    assert "schema, identity, graph, corrupt-state, and local invariant" in combined
    assert "`ownership_timeout` is reserved for" in combined
    assert "no local request record" in combined
    assert "ownership conflicts after local state exists are `invariant_failure`" in combined
    assert (
        "owner mismatch during post-click cancellation is `cancellation_unproven`" in combined
    )
