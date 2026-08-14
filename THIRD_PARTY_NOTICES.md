# Third-party software notices

readNdraft is licensed under Apache-2.0. Its Python dependencies are installed as
separate distributions by the package installer; their code is not copied into
the readNdraft wheel or source distribution. Each dependency remains subject to
its own license and includes its authoritative license material in its upstream
distribution.

This inventory reflects the runtime dependency graph locked for readNdraft
0.4.0. Platform markers mean that an individual installation contains only the
packages applicable to that operating system.

| Package | Version | License | Platform |
| --- | ---: | --- | --- |
| annotated-doc | 0.0.5 | MIT | All |
| annotated-types | 0.8.0 | MIT | All |
| anyio | 4.14.2 | MIT | All |
| attrs | 26.1.0 | MIT | All |
| certifi | 2026.7.22 | MPL-2.0 | All |
| cffi | 2.1.1 | MIT-0 | All |
| click | 8.4.2 | BSD-3-Clause | All |
| colorama | 0.4.6 | BSD-3-Clause | Windows |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause | All |
| css-inline | 0.21.0 | MIT | All |
| h11 | 0.16.0 | MIT | All |
| httpcore | 1.0.9 | BSD-3-Clause | All |
| httpx | 0.28.1 | BSD-3-Clause | All |
| httpx-sse | 0.4.3 | MIT | All |
| idna | 3.18 | BSD-3-Clause | All |
| jaraco.classes | 3.4.0 | MIT | All |
| jaraco.context | 6.1.2 | MIT | All |
| jaraco.functools | 4.6.0 | MIT | All |
| jeepney | 0.9.0 | MIT | Linux |
| jsonschema | 4.26.0 | MIT | All |
| jsonschema-specifications | 2025.9.1 | MIT | All |
| keyring | 25.7.0 | MIT | All |
| markdown-it-py | 4.2.0 | MIT | All |
| mcp | 1.29.0 | MIT | All |
| mdurl | 0.1.2 | MIT | All |
| more-itertools | 11.1.0 | MIT | All |
| nh3 | 0.3.6 | MIT | All |
| pycparser | 3.0 | BSD-3-Clause | All |
| pydantic | 2.13.4 | MIT | All |
| pydantic-core | 2.46.4 | MIT | All |
| pydantic-settings | 2.15.0 | MIT | All |
| Pygments | 2.20.0 | BSD-2-Clause | All |
| PyJWT | 2.13.0 | MIT | All |
| python-dotenv | 1.2.2 | BSD-3-Clause | All |
| python-multipart | 0.0.32 | Apache-2.0 | All |
| pywin32 | 312 | Mixed; see upstream license files and per-file notices | Windows |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | Windows |
| referencing | 0.37.0 | MIT | All |
| rich | 15.0.0 | MIT | All |
| rpds-py | 2026.6.3 | MIT | All |
| SecretStorage | 3.5.0 | BSD-3-Clause | Linux |
| shellingham | 1.5.4 | ISC | All |
| sse-starlette | 3.4.8 | BSD-3-Clause | All |
| starlette | 1.6.0 | BSD-3-Clause | All |
| typer | 0.27.1 | MIT | All |
| tinycss2 | 1.5.1 | BSD-3-Clause | All |
| tinyhtml5 | 2.1.0 | MIT | All |
| typing-extensions | 4.16.0 | PSF-2.0 | All |
| typing-inspection | 0.4.3 | MIT | All |
| uvicorn | 0.52.1 | BSD-3-Clause | All |
| webencodings | 0.5.1 | BSD-3-Clause | All |

Notable conditions:

- certifi is MPL-2.0 licensed. It remains a separate, unmodified dependency;
  redistribution or modification of its files must continue to comply with
  MPL-2.0.
- pywin32 contains components under different licenses. Its source-tree license
  files and per-file notices are authoritative and must be preserved if pywin32
  is ever bundled or redistributed with readNdraft.
- nh3 uses the Rust ammonia sanitizer, css-inline uses components from Servo,
  tinyhtml5 implements WHATWG HTML parsing, and tinycss2 parses CSS syntax. They
  are used because Python's standard library does not provide a maintained HTML
  sanitizer, standards-compliant HTML5 parser, CSS parser, or CSS cascade
  inliner suitable for treating authored email HTML as a security boundary.
- tinycss2 and webencodings use BSD-3-Clause licenses verified from their
  upstream license texts; their installed metadata reports only the generic
  `BSD License` classifier or label, so the license policy check carries narrow
  reviewed overrides for these two distributions.
- A future standalone executable, installer, container, or vendored dependency
  bundle must carry the complete applicable third-party license texts and
  notices. This inventory alone is not a substitute for those materials.

Build and development tools such as Hatchling, pytest, and pip-audit are not
distributed in readNdraft release artifacts. Their licenses remain available in
their independently installed distributions.
