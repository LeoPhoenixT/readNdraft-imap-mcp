from pathlib import Path
import importlib.util


FORBIDDEN_SOURCE_TERMS = (
    "send_email",
    "submit_message",
    "smtplib",
    "mail.send",
)


def test_no_forbidden_submission_capability() -> None:
    roots = [Path("src"), Path("pyproject.toml")]
    text = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for root in roots
        for path in ([root] if root.is_file() else root.rglob("*.py"))
    )
    for term in FORBIDDEN_SOURCE_TERMS:
        assert term not in text


def test_mcp_frontend_has_no_credential_or_protocol_implementation() -> None:
    frontend = Path("src/readndraft_imap_mcp/mcp_server")
    text = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for path in frontend.rglob("*.py")
    )
    for term in ("keyring", "getpass", "imaplib", "load_secret", "save_secret"):
        assert term not in text


def test_on_demand_launcher_has_no_credential_or_account_implementation() -> None:
    source = Path("src/readndraft_imap_mcp/platform/launcher.py").read_text(
        encoding="utf-8"
    ).casefold()
    for term in (
        "keyring",
        "getpass",
        "imaplib",
        "load_secret",
        "save_secret",
        "accountfile",
        "accounts_file",
    ):
        assert term not in source


def test_no_generic_flag_mutation_api() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for path in Path("src").rglob("*.py")
    )
    for term in ("set_flags", "store_flags", "raw_store"):
        assert term not in source


def test_no_broad_expunge_fallback() -> None:
    source = Path("src/readndraft_imap_mcp/imap/client.py").read_text(encoding="utf-8")
    assert ".expunge(" not in source.casefold()
    assert 'uid("EXPUNGE", record.uid)' in source


def test_message_move_fallback_is_private_and_uses_targeted_uid_expunge() -> None:
    client = Path("src/readndraft_imap_mcp/imap/client.py").read_text(
        encoding="utf-8"
    )
    frontend = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for path in Path("src/readndraft_imap_mcp/mcp_server").rglob("*.py")
    )
    assert 'command("UID", "MOVE"' in client
    assert 'self.imap.uid("EXPUNGE", identity.uid)' in client
    assert ".expunge(" not in client.casefold()
    for forbidden_tool in (
        "copy_email",
        "delete_email",
        "expunge_email",
        "set_deleted",
        "raw_imap",
    ):
        assert f"def {forbidden_tool}" not in frontend


def test_repository_security_check_passes() -> None:
    path = Path("scripts/security_check.py").resolve()
    spec = importlib.util.spec_from_file_location("security_check", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0
