from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MARKDOWN = [
    ROOT / "README.md",
    ROOT / "reference" / "README.md",
    *sorted((ROOT / "docs").rglob("*.md")),
]
LONG_HEX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{24,64}(?![0-9A-Fa-f])")
UUID = re.compile(
    r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[1-5][0-9A-Fa-f]{3}-"
    r"[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}\b"
)


def _allowed_checksums() -> set[str]:
    checksum_file = ROOT / "reference" / "SHA256SUMS"
    return {
        line.split()[0].casefold()
        for line in checksum_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def test_public_markdown_contains_only_truncated_runtime_identifiers() -> None:
    allowed_checksums = _allowed_checksums()
    findings: list[str] = []

    for path in PUBLIC_MARKDOWN:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for match in UUID.finditer(line):
                findings.append(
                    f"{path.relative_to(ROOT)}:{line_number}: UUID {match.group(0)}"
                )
            for match in LONG_HEX.finditer(line):
                value = match.group(0)
                if value.casefold() in allowed_checksums:
                    continue
                findings.append(
                    f"{path.relative_to(ROOT)}:{line_number}: long identifier {value}"
                )

    assert findings == [], "public Markdown must truncate runtime identifiers:\n" + "\n".join(
        findings
    )
