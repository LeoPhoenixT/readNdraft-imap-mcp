# Changelog

## 0.8.1

### Changed

- Raised the supported runtime to Python 3.12.6 because strict draft-address
  parsing depends on behavior introduced in that patch release.
- Added broker Python runtime identity and shared compatibility reporting to
  authenticated health checks and `doctor` without exposing the executable.
- Made explicit broker shutdown prompt when no active work needs draining.

### Fixed

- Made both `readndraft-broker stop` and `readndraft-imap-mcp broker stop`
  invoke the supported broker shutdown command.
- Waited for an incompatible broker's endpoint and singleton ownership to be
  fully released before starting and exposing a compatible replacement.
- Audited draft build and validation failures for both draft creation and
  updates with a safe failure stage and exception category.

## 0.8.0

### Removed

- Removed the deprecated `skill` and `update` commands, direct skill-management
  APIs, bundled `readndraft-update` skill, and ignored `setup --install-skill`
  option. Use the client-native marketplace plugin lifecycle instead.
- Removed the undocumented `readndraft-install` console command and service
  installer helpers.

### Changed

- Made `migrate-plugin` preserve manifest-less, modified, and otherwise
  unrecognized legacy skill directories.
- Stopped embedding marketplace skills in the Python wheel; the shared plugin
  remains the supported skill distribution.

## 0.7.1

### Removed

- Removed the undocumented Phase 0 diagnostic CLI; normal MCP, doctor, and
  account functionality is unaffected.

## 0.6.0

### Added

- Added configurable sender display names for generated draft `From` headers.
- Preserved optional recipient display names in draft `To`, `Cc`, and `Bcc`
  headers.
- Added reply drafts with broker-derived `In-Reply-To` and `References`
  metadata from an exact source-message identity.

### Changed

- Persisted reply threading metadata across MCP-managed draft updates while
  retaining compatibility with existing draft tracking records.
- Advanced the authenticated local broker IPC contract to version 9.

## 0.5.1

### Added

- Added an audit repair command that archives an invalid audit log before a fresh
  chain begins.

### Fixed

- Prevented concurrent broker instances and cross-process audit chain forks.
- Retired version-mismatched brokers before launching upgraded versions.
- Reported audit integrity failures with fork-aware diagnostics and recovery guidance.

## 0.5.0

### Added

- Draft HTML accepts `tel:` links, `dir`/`lang`, line-wrapped CSS values, and
  common inert tags including `font`, `ruby`, `ins`, `del`, `mark`, `abbr`, `q`,
  `cite`, `figure`, `bdi`, `bdo`, and `wbr`.

### Changed

- Draft HTML validation failures name the rejected tag, attribute, or CSS
  property instead of returning only `request rejected`.

### Fixed

- Authored draft HTML accepts and safely replaces link `rel` values, allowing
  `get_email_html` output to pass directly back to `create_draft`.
- Inbound mail with line-wrapped `style` attributes retains safe styling.

### Security

- CSS values are whitespace-stripped before unsafe-marker scanning, ensuring
  spaced and line-wrapped forms such as `url (...)` remain rejected.
