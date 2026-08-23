from __future__ import annotations

import importlib.metadata

from readndraft_imap_mcp.cli import main


def run() -> None:
    distribution = importlib.metadata.distribution("readndraft-imap-mcp")
    scripts = {
        entry.name
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }
    assert "readndraft-imap-mcp" in scripts
    assert "readndraft-install" not in scripts
    assert "readndraft-approve" not in scripts
    assert main(["--help"]) == 0
    print(f"Distribution smoke test passed for {distribution.version}.")


if __name__ == "__main__":
    run()
