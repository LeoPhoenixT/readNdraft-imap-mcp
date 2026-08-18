from __future__ import annotations

import tarfile
import tomllib
import zipfile
import importlib.util
import subprocess
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "release_check", Path("scripts/release_check.py").resolve()
)
assert _SPEC and _SPEC.loader
_RELEASE_CHECK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RELEASE_CHECK)


def test_release_accepts_approved_license() -> None:
    assert _RELEASE_CHECK.errors_for("v0.5.0") == []


def test_release_workflow_uses_version_merge_sha_and_separates_publish() -> None:
    text = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "branches: [main]" in text
    assert "- pyproject.toml" in text
    assert 'tags:\n      - "v[0-9]+' not in text
    assert "BEFORE_SHA: ${{ github.event.before }}" in text
    assert "RELEASE_SHA: ${{ github.sha }}" in text
    assert "current != previous" in text
    assert "ref: ${{ needs.prepare.outputs.sha }}" in text
    assert "git tag -a \"${RELEASE_TAG}\" \"${RELEASE_SHA}\"" in text
    assert "git rev-list -n 1 \"${RELEASE_TAG}\"" in text
    assert "needs: [prepare, test]" in text
    assert "needs: [prepare, build, tag]" in text
    assert text.count("id-token: write") == 1
    assert "uv publish --trusted-publishing always" in text
    assert "needs: [prepare, publish]" in text
    assert "contents: write" in text
    assert "gh release create" in text
    assert "--verify-tag" in text
    assert "GH_REPO: ${{ github.repository }}" in text
    assert "--generate-notes" in text
    assert "persist-credentials: false" in text
    assert "scripts/validate_plugin_versions.py" in text


def test_testpypi_workflow_requires_exact_candidate_sha() -> None:
    text = Path(".github/workflows/test-release.yml").read_text(encoding="utf-8")
    assert "release_sha:" in text
    assert "Exact 40-character commit SHA to test and publish" in text
    assert "Validate immutable candidate SHA" in text
    assert '[[ ! "${RELEASE_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]' in text
    assert 'echo "sha=${RELEASE_SHA,,}" >> "${GITHUB_OUTPUT}"' in text
    assert text.count("ref: ${{ needs.prepare.outputs.sha }}") == 2
    assert "ref: ${{ inputs.release_sha }}" not in text
    assert "ref: ${{ github.event.repository.default_branch }}" not in text
    assert "needs: [prepare, test]" in text
    assert "needs: [prepare, build]" in text
    assert "scripts/validate_plugin_versions.py" in text
    assert "testpypi-dist-${{ needs.prepare.outputs.sha }}" in text


def test_built_distributions_contain_unified_cli_and_skills(tmp_path) -> None:
    subprocess.run(
        ["uv", "build", "--no-sources", "--out-dir", str(tmp_path)],
        check=True,
    )
    wheels = list(tmp_path.glob("*.whl"))
    sdists = list(tmp_path.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        assert "readndraft_imap_mcp/_skills/readndraft-email/SKILL.md" in names
        assert "readndraft_imap_mcp/_skills/readndraft-update/SKILL.md" in names
        assert any(name.endswith("/licenses/LICENSE") for name in names)
        assert any(name.endswith("/licenses/THIRD_PARTY_NOTICES.md") for name in names)
        entry_points = next(name for name in names if name.endswith("entry_points.txt"))
        entry_point_text = archive.read(entry_points).decode("utf-8")
        assert "[console_scripts]" in entry_point_text
        assert "readndraft-imap-mcp" in entry_point_text
        assert "readndraft-approve" not in entry_point_text
        assert "[gui_scripts]" in entry_point_text
        assert "readndraft-mcp" in entry_point_text
        assert "readndraft-launch" in entry_point_text
    with tarfile.open(sdists[0]) as archive:
        names = set(archive.getnames())
        assert any(name.endswith("/skills/readndraft-email/SKILL.md") for name in names)
        assert any(name.endswith("/skills/readndraft-update/SKILL.md") for name in names)
        assert any(name.endswith("/LICENSE") for name in names)
        assert any(name.endswith("/THIRD_PARTY_NOTICES.md") for name in names)


def test_project_has_public_and_approved_license_metadata() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["authors"] == [{"name": "LeoPhoenixT"}]
    assert project["urls"]["Repository"].endswith("/readNdraft-imap-mcp")
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE", "THIRD_PARTY_NOTICES.md"]


def test_installed_dependency_license_policy_passes() -> None:
    completed = subprocess.run(
        ["uv", "run", "--locked", "python", "scripts/license_check.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
