# Release procedure

Publishing is intentionally separate from normal CI. A version tag triggers the
production release workflow; never create or push one until the release PR is
merged and its required checks have succeeded.

## Prepare and merge the release PR

1. Start from current `main` on `codex/release-X.Y.Z`.
2. Update the version in `pyproject.toml`, `uv.lock`,
   `src/readndraft_imap_mcp/__init__.py`, and `tests/test_release.py`.
3. Update user-facing setup, migration, security, and skill documentation for
   behavior changed by the release.
4. Run locally:

   ```console
   uv run --locked python scripts/release_check.py --tag vX.Y.Z
   uv run --locked pytest
   ```

5. Review the complete diff, push the branch, and open the release PR.
6. Merge only when the PR is ready, all six required `Test and security` checks
   have succeeded, review threads are resolved, and the merge state is clean.

## Publish from the protected tag

1. Fast-forward local `main` to the merged release commit and verify the version.
2. Confirm `vX.Y.Z` does not already exist locally or remotely.
3. Create one annotated `vX.Y.Z` tag on that exact commit and push it. Never
   move, delete, or recreate a released version tag.
4. Monitor `Publish release to PyPI`. The workflow reruns the Windows and Ubuntu
   test/security matrix, builds wheel and source distributions, verifies their
   metadata and contents, smoke-tests both artifacts, generates PEP 740
   attestations, and publishes to production PyPI through Trusted Publishing.
5. After publication, verify that the workflow created the matching public
   GitHub Release with generated notes and attached distributions.

TestPyPI, additional clean-machine checks, and real provider/client acceptance
are optional pre-release validation for changes that need them; they are not
automated tag gates. Record any such validation in the release PR. Never place a
PyPI token in the repository; the production workflow uses trusted identity.
