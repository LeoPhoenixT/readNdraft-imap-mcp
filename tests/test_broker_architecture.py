from __future__ import annotations

import ast
from pathlib import Path

BROKER_DIR = Path("src/readndraft_imap_mcp/broker")
DOMAIN_MODULES = ("search.py", "reads.py", "drafts.py", "mutations.py")


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_domain_modules_do_not_import_the_broker_service_runtime() -> None:
    for filename in DOMAIN_MODULES:
        imports = [
            node.module
            for node in ast.walk(_module(BROKER_DIR / filename))
            if isinstance(node, ast.ImportFrom)
        ]
        assert "service" not in imports
        assert "execution" not in imports


def test_execution_context_compatibility_import_is_preserved() -> None:
    from readndraft_imap_mcp.broker.execution import BrokerExecutionContext
    from readndraft_imap_mcp.broker.service import BrokerExecutionContext as CompatibilityContext

    assert CompatibilityContext is BrokerExecutionContext
