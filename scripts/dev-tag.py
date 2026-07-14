#!/usr/bin/env python3
"""Compute the next dev image tag from the latest git version tag.

Reads the latest version tag (e.g. 0.3.24) and outputs the next patch
with a .dev suffix (e.g. 0.3.25.dev). Used by the dev image rebuild
workflow to tag images that pick up the latest unpinned dependencies
(skills, RPMs) without cutting a full release.
"""

import re
import subprocess
import sys


def main() -> None:
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("ERROR: could not read version from git tags", file=sys.stderr)
        sys.exit(1)

    match = re.match(r"(\d+)\.(\d+)\.(\d+)$", tag)
    if not match:
        print(f"ERROR: tag {tag!r} does not match X.Y.Z format", file=sys.stderr)
        sys.exit(1)

    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    print(f"{major}.{minor}.{patch + 1}.dev")


if __name__ == "__main__":
    main()
