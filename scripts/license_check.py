from __future__ import annotations

import sys
from importlib.metadata import Distribution, distributions

ALLOWED_EXPRESSIONS = {
    "Apache-2.0",
    "Apache-2.0 OR BSD-2-Clause",
    "Apache-2.0 OR BSD-3-Clause",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "MIT",
    "MIT-0",
    "MPL-2.0",
    "PSF-2.0",
}

LICENSE_ALIASES = {
    "apache 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "mit license": "MIT",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "psfl": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
}

# These packages need package-specific review rather than a lossy conversion of
# generic Python metadata. Keep the rationale synchronized with
# THIRD_PARTY_NOTICES.md.
REVIEWED_OVERRIDES = {
    "colorama": "BSD-3-Clause",
    "pywin32": "MIXED-PER-UPSTREAM",
    "pywin32-ctypes": "BSD-3-Clause",
    "tinycss2": "BSD-3-Clause",
    "webencodings": "BSD-3-Clause",
}


def _classifier_license(distribution: Distribution) -> str | None:
    for classifier in distribution.metadata.get_all("Classifier", []):
        if classifier.startswith("License :: OSI Approved :: "):
            label = classifier.rsplit(" :: ", 1)[-1].casefold()
            if label in LICENSE_ALIASES:
                return LICENSE_ALIASES[label]
    return None


def license_expression(distribution: Distribution) -> str | None:
    name = distribution.metadata.get("Name", "").casefold()
    if name in REVIEWED_OVERRIDES:
        return REVIEWED_OVERRIDES[name]

    value = distribution.metadata.get("License-Expression")
    if value and value.strip().upper() != "UNKNOWN":
        return value.strip()

    value = distribution.metadata.get("License")
    if value and value.strip().upper() != "UNKNOWN":
        normalized = LICENSE_ALIASES.get(value.strip().casefold(), value.strip())
        if normalized in ALLOWED_EXPRESSIONS:
            return normalized

    return _classifier_license(distribution)


def errors_for_installed() -> list[str]:
    errors: list[str] = []
    for distribution in sorted(
        distributions(), key=lambda item: item.metadata.get("Name", "").casefold()
    ):
        name = distribution.metadata.get("Name", "unknown")
        expression = license_expression(distribution)
        if expression is None:
            errors.append(f"{name}: missing or unrecognized license metadata")
        elif expression != "MIXED-PER-UPSTREAM" and expression not in ALLOWED_EXPRESSIONS:
            errors.append(f"{name}: license is not approved: {expression}")
    return errors


def main() -> int:
    errors = errors_for_installed()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Installed dependency license policy passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
