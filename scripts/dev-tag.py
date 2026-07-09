#!/usr/bin/env python3
"""Compute the next dev image tag from pyproject.toml version.

Reads the current version (e.g. 0.3.19) and outputs the next patch with
a .dev suffix (e.g. 0.3.20.dev). Used by the dev image rebuild workflow
to tag images that pick up the latest unpinned dependencies (skills,
RPMs) without cutting a full release.
"""

import re
import sys
from pathlib import Path


def main() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = pyproject.read_text()
    match = re.search(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text, re.MULTILINE)
    if not match:
        print("ERROR: could not parse version from pyproject.toml", file=sys.stderr)
        sys.exit(1)
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    print(f"{major}.{minor}.{patch + 1}.dev")


if __name__ == "__main__":
    main()
