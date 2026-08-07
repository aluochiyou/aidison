from __future__ import annotations

import ast
from pathlib import Path


def test_generic_planning_and_execution_do_not_import_research_modules() -> None:
    root = Path(__file__).parents[2]
    generic_modules = (
        root / "src/aidison/runtime/planning.py",
        root / "src/aidison/application/execution.py",
        root / "src/aidison/infrastructure/planning.py",
    )

    forbidden: list[str] = []
    for path in generic_modules:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if "research" in node.module:
                    forbidden.append(f"{path.name}:{node.lineno}:{node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "research" in alias.name:
                        forbidden.append(f"{path.name}:{node.lineno}:{alias.name}")

    assert forbidden == []
