#!/usr/bin/env python3
"""Install skills from a marketplace registry for OpenCode.

Clones plugin repos from a marketplace and copies their skill directories
into OpenCode's global skills path (~/.config/opencode/skills/).

Usage:
    install-skills.py --marketplace-repo opendatahub-io/skills-registry
    install-skills.py --all
    install-skills.py plugin-a plugin-b

Environment:
    OPENCODE_CONFIG_DIR     Override ~/.config/opencode
                            (default: $HOME/.config/opencode)
    MARKETPLACE_CACHE_DIR   Override marketplace cache location
                            (default: $OPENCODE_CONFIG_DIR)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _config_dir():
    return Path(
        os.environ.get(
            "OPENCODE_CONFIG_DIR",
            Path.home() / ".config" / "opencode",
        )
    )


def _skills_dir():
    return _config_dir() / "skills"


def _marketplace_base():
    return Path(os.environ.get("MARKETPLACE_CACHE_DIR", str(_config_dir()))) / "marketplaces"


def _git(*args, **kwargs):
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
        **kwargs,
    )


def _read_json(path):
    with open(path) as f:
        return json.load(f)


# ── marketplace registration ────────────────────────────────────────────


def register_marketplace(repo):
    url = f"https://github.com/{repo}.git"
    print(f"==> Registering marketplace from {repo}")

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / "registry"
        _git("clone", "--depth", "1", "--quiet", url, str(clone_dir))

        mkt_json_path = clone_dir / ".claude-plugin" / "marketplace.json"
        if not mkt_json_path.exists():
            print(f"ERROR: {repo} has no .claude-plugin/marketplace.json", file=sys.stderr)
            sys.exit(1)

        mkt_data = _read_json(mkt_json_path)
        mkt_name = mkt_data.get("name") or os.path.basename(repo)

        mkt_dir = _marketplace_base() / mkt_name
        mkt_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mkt_json_path, mkt_dir / "marketplace.json")

    print(f"==> Marketplace '{mkt_name}' registered")


# ── skill installation ─────────────────────────────────────────────────


def _clone_source(repo, ref, dest):
    url = f"https://github.com/{repo}.git"
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref):
        _git("clone", "--quiet", url, str(dest))
        _git("-C", str(dest), "checkout", "--quiet", ref)
    else:
        _git("clone", "--depth", "1", "--branch", ref, "--quiet", url, str(dest))


def _copy_skills(src, dest):
    """Copy skills from cloned source into OpenCode skills directory."""
    src = Path(src)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    for search_path in [".claude/skills", ".opencode/skills", "skills"]:
        candidate = src / search_path
        if candidate.is_dir():
            shutil.copytree(candidate, dest, dirs_exist_ok=True)
            return True
    return False


def install_plugin(mkt_name, plugin_entry):
    name = plugin_entry["name"]
    version = plugin_entry.get("version", "0.0.0")
    source = plugin_entry["source"]
    src_repo = source["repo"]
    src_ref = source.get("ref", "main")

    print(f"==> Installing skills from {name}@{version} ({src_repo} @ {src_ref})")

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / "src"
        _clone_source(src_repo, src_ref, clone_dir)

        skills_dest = _skills_dir()
        found = _copy_skills(clone_dir, skills_dest)

    if found:
        print(f"==> Installed skills from {name}@{version} -> {skills_dest}")
    else:
        print(f"==> No skills found in {name}@{version}")


# ── install modes ───────────────────────────────────────────────────────


def install_all():
    mkt_base = _marketplace_base()
    if not mkt_base.exists():
        print("ERROR: no marketplaces registered", file=sys.stderr)
        sys.exit(1)

    found = False
    for mkt_json in sorted(mkt_base.glob("*/marketplace.json")):
        found = True
        mkt_name = mkt_json.parent.name
        data = _read_json(mkt_json)
        plugins = data.get("plugins", [])
        print(f"==> Marketplace '{mkt_name}': {len(plugins)} plugins")
        for entry in plugins:
            install_plugin(mkt_name, entry)

    if not found:
        print("ERROR: no marketplaces registered", file=sys.stderr)
        sys.exit(1)


def install_named(names):
    mkt_base = _marketplace_base()
    if not mkt_base.exists():
        print("ERROR: no marketplaces registered", file=sys.stderr)
        sys.exit(1)

    marketplaces = {}
    for mkt_json in sorted(mkt_base.glob("*/marketplace.json")):
        mkt_name = mkt_json.parent.name
        marketplaces[mkt_name] = _read_json(mkt_json)

    for target in names:
        installed = False
        for mkt_name, data in marketplaces.items():
            for entry in data.get("plugins", []):
                if entry["name"] == target:
                    install_plugin(mkt_name, entry)
                    installed = True
                    break
            if installed:
                break
        if not installed:
            print(f"ERROR: plugin '{target}' not found", file=sys.stderr)
            sys.exit(1)


# ── CLI ─────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Install skills from a marketplace for OpenCode.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--marketplace-repo", metavar="OWNER/REPO", help="Register a GitHub marketplace repo"
    )
    group.add_argument(
        "--all", action="store_true", help="Install all skills from registered marketplaces"
    )
    group.add_argument("names", nargs="*", default=[], help="Plugin names to install skills from")

    args = parser.parse_args()

    if args.marketplace_repo:
        register_marketplace(args.marketplace_repo)
    elif args.all:
        install_all()
    elif args.names:
        install_named(args.names)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
