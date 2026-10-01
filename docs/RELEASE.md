# Release procedure

Publishing is separate from normal PR CI. A push to `main` that changes the
project version starts the production release workflow. Merging a version-bump
PR is therefore the final manual production-release action; the workflow creates
the release tag after its tests and build succeed.

## Prepare and merge the release PR

1. Start from current `main` on `codex/release-X.Y.Z`.
2. Update the version in `pyproject.toml`, `uv.lock`,
   `src/readndraft_imap_mcp/__init__.py`, and `tests/test_release.py`. Synchronize
   the Codex/Claude plugin manifests, Claude marketplace version, plugin MCP
   runtime pin, and current installation examples.
3. Update user-facing setup, migration, security, and skill documentation for
   behavior changed by the release.
4. Run locally:

   ```console
   uv run --locked python scripts/release_check.py --tag vX.Y.Z
   uv run --locked python scripts/validate_plugin_versions.py
   uv run --locked pytest
   ```

5. Review the complete diff, push the branch, and open the release PR.
6. Merge only when the PR is ready, all six required `Test and security` checks
   have succeeded, review threads are resolved, and the merge state is clean.

## Publish the immutable merged commit

1. After an explicitly authorized merge, monitor `Publish release to PyPI`.
   It compares the project version at the push's `before` SHA with the immutable
   pushed commit SHA. An unchanged version skips publication.
2. The workflow tests and builds that exact SHA, then creates its annotated
   `vX.Y.Z` tag. An existing tag is accepted only when it resolves to the same
   SHA; a conflicting tag fails the release. Never manually create, move,
   delete, or recreate the normal production tag.
3. The workflow reruns the Windows and Ubuntu
   test/security matrix, builds wheel and source distributions, verifies their
   metadata and contents, smoke-tests both artifacts, generates PEP 740
   attestations, and publishes to production PyPI through Trusted Publishing.
4. After publication, verify the wheel and source distribution on PyPI and
   confirm that the workflow created the matching public GitHub Release with
   generated notes.

TestPyPI, additional clean-machine checks, and real provider/client acceptance
are optional pre-release validation for changes that need them; they are not
automated production gates. A TestPyPI dispatch requires both `release_tag` and
the exact 40-character `release_sha`. Record any additional validation in the
release PR. Never place a
PyPI token in the repository; the production workflow uses trusted identity.
