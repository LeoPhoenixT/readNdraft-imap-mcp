# Changelog

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
