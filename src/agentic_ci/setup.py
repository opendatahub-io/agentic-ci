"""Container bootstrap: install deps, create non-root user, install Claude Code.

Auth backend setup (API keys, GCP credentials, etc.) is the caller's
responsibility — pass credentials via environment variables.
"""

import os
import pwd
import shutil
import subprocess
import tempfile
import urllib.request


def _run(*args):
    print(f"  + {' '.join(args)}", flush=True)
    subprocess.run(args, check=True)


def _chown_recursive(path, user, group):
    for root, dirs, files in os.walk(path):
        shutil.chown(root, user, group)
        for name in dirs + files:
            shutil.chown(os.path.join(root, name), user, group)


def _write_gitconfig(user, entries):
    """Write git config entries to a user's ~/.gitconfig."""
    home = pwd.getpwnam(user).pw_dir
    gitconfig = os.path.join(home, ".gitconfig")
    existing = ""
    if os.path.exists(gitconfig):
        with open(gitconfig) as f:
            existing = f.read()
    with open(gitconfig, "a") as f:
        for section, key, value in entries:
            line = f"[{section}]\n\t{key} = {value}\n"
            if line not in existing:
                f.write(line)
    shutil.chown(gitconfig, user, user)


def setup(workspace=None, user="claude-ci", install_url="https://claude.ai/install.sh"):
    """Bootstrap a CI container for running Claude Code.

    Installs system dependencies, creates a non-root user, and installs
    Claude Code. The workspace directory (if it exists) is owned by the
    new user with git safe.directory configured.
    """
    if workspace is None:
        workspace = os.environ.get("WORKSPACE_DIR", "/workspace")

    _run(
        "microdnf",
        "install",
        "-y",
        "--nodocs",
        "git-core",
        "shadow-utils",
        "util-linux",
        "python3",
        "python3-pip",
        "diffutils",
    )

    _run("useradd", "-m", user)

    with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as f:
        req = urllib.request.Request(install_url, headers={"User-Agent": "agentic-ci"})
        with urllib.request.urlopen(req) as resp:
            f.write(resp.read())
        os.chmod(f.name, 0o755)
    try:
        _run("runuser", "-l", user, "-c", f"bash {f.name}")
    finally:
        os.unlink(f.name)

    if os.path.isdir(workspace):
        _chown_recursive(workspace, user, user)
        _write_gitconfig(
            user,
            [
                ("safe", "directory", workspace),
            ],
        )


def main(args=None):
    import argparse

    parser = argparse.ArgumentParser(description="Bootstrap CI container for Claude Code")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Working directory (default: $WORKSPACE_DIR or /workspace)",
    )
    parser.add_argument(
        "--user", default="claude-ci", help="Non-root user to create (default: claude-ci)"
    )
    parsed = parser.parse_args(args)
    setup(workspace=parsed.workspace, user=parsed.user)


if __name__ == "__main__":
    main()
