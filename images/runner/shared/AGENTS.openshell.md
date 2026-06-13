# CI Sandbox Environment

You are running inside a sandboxed CI environment with strict network, filesystem, and process restrictions.
Actions that violate these constraints will be blocked.
Do not retry blocked actions -- stop early to save tokens.

## Network Restrictions

Outbound network access is restricted by policy.
Only connections to explicitly allowed domains will succeed.
If a network request is blocked, do not retry it -- the policy will not change during your session.
If the blocked request is required to complete the task, stop and report the failure instead of searching for workarounds.

## Filesystem Restrictions

Filesystem access is enforced by Landlock. Writes to paths not listed below will fail silently.

- **Read-only:** `/usr`, `/lib`, `/etc`, `/proc`, `/dev/urandom`, `/app`, `/var/log`
- **Read-write:** `/sandbox` (home), `/tmp`, working directory

You **cannot** install system packages -- the package manager paths are
read-only and you have no root access. Files written outside the working
directory are **not preserved** after the session ends.

## Process Restrictions

- **No root access.** `sudo`, `su`, `dnf`, `microdnf` will not work.
- **No mount operations.** `mount`, `umount`, and filesystem namespace operations are blocked by seccomp.
- **No process debugging.** `ptrace`, `strace`, and cross-process memory access are blocked.
- **No raw sockets.** Only standard TCP/UDP connections through the sandbox proxy are allowed.

## Available Tools

These tools are pre-installed.
Do not attempt to install replacements or download binaries from the internet.

`python3`, `uv`, `ruff`, `git`, `gh`, `glab`, `make`, `curl`, `jq`, `shellcheck`

## Guidelines

- Use `uv` for Python package management, not `pip`.
- Use `gh` for GitHub operations and `glab` for GitLab operations.
- Write files only to the working directory or `/tmp`.
- If a network request is blocked, do not retry it. If it was required to complete the task, stop and report the failure.
- If a required tool is missing, stop and report it rather than trying to install it.
- Do not attempt to escalate privileges or bypass security controls.
