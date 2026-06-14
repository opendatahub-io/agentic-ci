#!/usr/bin/env python3
"""Install Claude Code plugins from a marketplace non-interactively.

Populates ~/.claude/plugins/ cache and metadata files so the container
image ships with pre-cached skills, agents, commands, and hooks.

Usage:
    install-plugin.py --marketplace-repo opendatahub-io/skills-registry
    install-plugin.py --all [--no-enable]
    install-plugin.py plugin-a plugin-b [--no-enable]
    install-plugin.py --repo https://github.com/org/repo.git [--no-enable]

Options:
    --no-enable  Cache plugins without enabling in settings.json.
                 Plugins can be enabled at runtime via AGENT_ENABLED_PLUGINS.

Environment:
    CLAUDE_HOME  Override ~/.claude (default: $HOME/.claude)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _claude_home():
    return Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))


def _plugins_dir():
    return _claude_home() / "plugins"


def _settings_path():
    return _claude_home() / "settings.json"


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _ensure_settings():
    path = _settings_path()
    if not path.exists():
        _write_json(path, {})
    return path


def _ensure_installed_json():
    path = _plugins_dir() / "installed_plugins.json"
    if not path.exists():
        _write_json(path, {"version": 2, "plugins": {}})
    return path


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

        mkt_dir = _plugins_dir() / "marketplaces" / mkt_name
        mkt_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mkt_json_path, mkt_dir / "marketplace.json")

    settings = _read_json(_ensure_settings())
    mk = settings.setdefault("extraKnownMarketplaces", {})
    mk[mkt_name] = {"source": {"source": "github", "repo": repo}}
    _write_json(_settings_path(), settings)

    print(f"==> Marketplace '{mkt_name}' registered")


# ── plugin installation ─────────────────────────────────────────────────


def _clone_source(repo, ref, dest):
    """Clone a source repo.  Full SHA refs need clone+checkout; branches
    and tags can use --branch with --depth 1."""
    url = f"https://github.com/{repo}.git"
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref):
        _git("clone", "--quiet", url, str(dest))
        _git("-C", str(dest), "checkout", "--quiet", ref)
    else:
        _git("clone", "--depth", "1", "--branch", ref, "--quiet", url, str(dest))


def _copy_content(src, dest, plugin_entry):
    """Copy skills, agents, commands, hooks from cloned source into cache.

    When explicit paths are declared in the marketplace entry, preserve
    the original relative path so Claude Code finds them where it expects.
    """
    src = Path(src)
    dest = Path(dest)

    # skills
    skills_paths = plugin_entry.get("skills", [])
    if skills_paths:
        for sp in skills_paths:
            resolved = src / sp
            if resolved.is_dir():
                target = dest / Path(sp)
                target.mkdir(parents=True, exist_ok=True)
                shutil.copytree(resolved, target, dirs_exist_ok=True)
    else:
        for fallback in [".claude/skills", "skills"]:
            candidate = src / fallback
            if candidate.is_dir():
                shutil.copytree(candidate, dest / "skills", dirs_exist_ok=True)
                break

    # agents
    agents_paths = plugin_entry.get("agents", [])
    if agents_paths:
        for ap in agents_paths:
            resolved = src / ap
            target = dest / Path(ap)
            if resolved.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(resolved, target)
            elif resolved.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copytree(resolved, target, dirs_exist_ok=True)
    else:
        for fallback in [".claude/agents", "agents"]:
            candidate = src / fallback
            if candidate.is_dir():
                shutil.copytree(candidate, dest / "agents", dirs_exist_ok=True)
                break

    # commands, hooks — fallback only
    for name in ["commands", "hooks"]:
        for fallback in [f".claude/{name}", name]:
            candidate = src / fallback
            if candidate.is_dir():
                shutil.copytree(candidate, dest / name, dirs_exist_ok=True)
                break


def _update_installed_json(mkt_name, name, version, install_path, sha):
    path = _ensure_installed_json()
    data = _read_json(path)
    ts = _now_iso()
    data["plugins"][f"{name}@{mkt_name}"] = [
        {
            "scope": "user",
            "installPath": str(install_path),
            "version": version,
            "installedAt": ts,
            "lastUpdated": ts,
            "gitCommitSha": sha,
        }
    ]
    _write_json(path, data)


def _update_enabled_plugins(mkt_name, name):
    path = _ensure_settings()
    data = _read_json(path)
    ep = data.setdefault("enabledPlugins", {})
    key = f"{name}@{mkt_name}"
    if key not in ep:
        ep[key] = True
    _write_json(path, data)


def install_plugin(mkt_name, plugin_entry, enable=True):
    name = plugin_entry["name"]
    version = plugin_entry.get("version", "0.0.0")
    source = plugin_entry["source"]
    src_repo = source["repo"]
    src_ref = source.get("ref", "main")

    print(f"==> Installing {name}@{version} from {src_repo} ({src_ref})")

    cache_dir = _plugins_dir() / "cache" / mkt_name / name / version
    cache_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / "src"
        _clone_source(src_repo, src_ref, clone_dir)

        result = _git("-C", str(clone_dir), "rev-parse", "HEAD")
        commit_sha = result.stdout.strip()

        _copy_content(clone_dir, cache_dir, plugin_entry)

    (cache_dir / ".in_use").write_text("installed\n")
    _update_installed_json(mkt_name, name, version, cache_dir, commit_sha)
    if enable:
        _update_enabled_plugins(mkt_name, name)

    print(f"==> Installed {name}@{version} -> {cache_dir}")


# ── install modes ───────────────────────────────────────────────────────


def install_all(enable=True):
    mkt_base = _plugins_dir() / "marketplaces"
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
            install_plugin(mkt_name, entry, enable=enable)

    if not found:
        print("ERROR: no marketplaces registered", file=sys.stderr)
        sys.exit(1)


def install_named(names, enable=True):
    mkt_base = _plugins_dir() / "marketplaces"
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
                    install_plugin(mkt_name, entry, enable=enable)
                    installed = True
                    break
            if installed:
                break
        if not installed:
            print(f"ERROR: plugin '{target}' not found", file=sys.stderr)
            sys.exit(1)


def install_from_repo(url, enable=True):
    print(f"==> Installing plugin from repo {url}")

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / "src"
        _git("clone", "--depth", "1", "--quiet", url, str(clone_dir))

        result = _git("-C", str(clone_dir), "rev-parse", "HEAD")
        commit_sha = result.stdout.strip()
        repo_name = os.path.basename(url).removesuffix(".git")

        cache_dir = _plugins_dir() / "cache" / "local" / repo_name / "0.0.0"
        cache_dir.mkdir(parents=True, exist_ok=True)

        _copy_content(clone_dir, cache_dir, {"skills": [], "agents": []})

    (cache_dir / ".in_use").write_text("installed\n")
    _update_installed_json("local", repo_name, "0.0.0", cache_dir, commit_sha)
    if enable:
        _update_enabled_plugins("local", repo_name)

    print(f"==> Installed {repo_name} from {url} -> {cache_dir}")


# ── CLI ─────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Install Claude Code plugins from a marketplace.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--marketplace-repo", metavar="OWNER/REPO", help="Register a GitHub marketplace repo"
    )
    group.add_argument(
        "--all", action="store_true", help="Install all plugins from registered marketplaces"
    )
    group.add_argument("--repo", metavar="URL", help="Install plugin from a git URL")
    group.add_argument("names", nargs="*", default=[], help="Plugin names to install")
    parser.add_argument(
        "--no-enable", action="store_true", help="Cache plugins without enabling in settings.json"
    )

    args = parser.parse_args()
    enable = not args.no_enable

    if args.marketplace_repo:
        register_marketplace(args.marketplace_repo)
    elif args.all:
        install_all(enable=enable)
    elif args.repo:
        install_from_repo(args.repo, enable=enable)
    elif args.names:
        install_named(args.names, enable=enable)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
