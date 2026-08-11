from __future__ import annotations

import importlib.metadata
import tempfile
from pathlib import Path

from readndraft_imap_mcp.cli import main
from readndraft_imap_mcp.platform.paths import AppPaths
from readndraft_imap_mcp.platform.skill import bundled_skill_dir, install_skill


def run() -> None:
    distribution = importlib.metadata.distribution("readndraft-imap-mcp")
    scripts = {
        entry.name
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }
    assert "readndraft-imap-mcp" in scripts
    assert "readndraft-approve" not in scripts
    source = bundled_skill_dir()
    assert (source / "SKILL.md").is_file()
    assert (source / "references" / "tool-workflows.md").is_file()
    assert (source / "references" / "confirmation-and-errors.md").is_file()
    assert main(["--help"]) == 0
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        paths = AppPaths(root / "config", root / "state", root / "runtime")
        target = install_skill("codex", paths=paths, home=root / "home")
        assert (target / "SKILL.md").is_file()
    print(f"Distribution smoke test passed for {distribution.version}.")


if __name__ == "__main__":
    run()
