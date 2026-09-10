from __future__ import annotations

import ast
from pathlib import Path

BROKER_DIR = Path("src/readndraft_imap_mcp/broker")
DOMAIN_MODULES = ("search.py", "reads.py", "drafts.py", "mutations.py")


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_execution_context_exposes_only_execution_and_account_public_methods() -> None:
    tree = _module(BROKER_DIR / "service.py")
    context = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BrokerExecutionContext"
    )
    public_methods = {
        node.name
        for node in context.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    }
    assert public_methods == {"handle", "list_accounts"}


def test_compatibility_facade_has_no_domain_private_helpers() -> None:
    tree = _module(BROKER_DIR / "service.py")
    facade = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BrokerService")
    private_methods = {
        node.name
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_")
        and not node.name.startswith("__")
    }
    assert private_methods == {"_client_call", "_batch_client_call"}


def test_domain_modules_do_not_import_the_broker_service_runtime() -> None:
    for filename in DOMAIN_MODULES:
        imports = [
            node.module
            for node in ast.walk(_module(BROKER_DIR / filename))
            if isinstance(node, ast.ImportFrom)
        ]
        assert "service" not in imports
