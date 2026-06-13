# CI Sandbox Environment

You are running inside a CI container. Adjust your behavior to work within these constraints.

## Constraints

- **No root access.** You cannot use `sudo`, `dnf`, `microdnf`, or install system packages.
- **Workspace.** Your project files are mounted at `/workspace`. Only modify files in this directory.
- **Network is unrestricted.** You can reach any external host.

## Available Tools

These tools are pre-installed. Do not attempt to install replacements or alternatives.

`python3`, `uv`, `ruff`, `git`, `gh`, `glab`, `make`, `curl`, `jq`, `shellcheck`

## Guidelines

- Use `uv` for Python package management, not `pip`.
- Use `gh` for GitHub operations and `glab` for GitLab operations.
- Write files only to `/workspace` or `/tmp`. Changes elsewhere may not persist.
- If a required tool is missing, work with what is available rather than trying to install it.
