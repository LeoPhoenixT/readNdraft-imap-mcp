# Release procedure

Publishing is intentionally separate from normal CI.

1. Confirm the PyPI project name and that Apache-2.0 metadata and license files
   are present in both built distributions.
2. Configure PyPI Trusted Publishing for this repository and release workflow.
3. Update and review the project version.
4. Run the full suite on Windows and Ubuntu.
5. Run the security policy, dependency audit, and license-policy check on both
   Windows and Ubuntu.
6. Build with `uv build --no-sources`.
7. Inspect wheel and source-distribution contents, including the Agent Skill.
8. Install the wheel in a clean environment and run the unified CLI smoke tests.
9. Publish to TestPyPI and complete clean-machine uvx tests.
10. Complete real Windows, Linux, target-client, and HKTV IMAP acceptance.
11. Create a protected version tag to publish to PyPI through trusted identity.

Never place a PyPI token in the repository. Production publication remains
blocked until Trusted Publisher setup and real-machine acceptance are completed
by a human.
