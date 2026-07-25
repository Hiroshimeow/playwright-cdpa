from __future__ import annotations

import ast
from pathlib import Path


def test_source_has_no_forbidden_browser_or_request_mutation_calls() -> None:
    forbidden: list[str] = []
    root = Path(__file__).resolve().parents[1] / "src"
    disallowed = {
        ("browser", "close"),
        ("page", "route"),
        ("route", "abort"),
        ("route", "fulfill"),
        ("request", "post"),
        ("request", "put"),
        ("request", "patch"),
        ("request", "delete"),
    }
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value.id if isinstance(node.func.value, ast.Name) else None
            if (owner, node.func.attr) in disallowed:
                forbidden.append(f"{path}:{node.lineno}:{owner}.{node.func.attr}()")
    assert forbidden == []
