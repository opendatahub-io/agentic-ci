"""Plugin and skill installation and runtime filtering.

Provides two build-time operations and one runtime operation:

Build-time (called from Containerfiles via ``agentic-ci install-plugins``):

- :func:`install_claude_plugins` — uses native Claude Code CLI to install
  plugins from a marketplace seed directory.
- :func:`install_opencode_skills` — clones plugin repos and copies SKILL.md
  files into OpenCode's skills directory.

Both generate a plugin-to-skill manifest at a well-known path.

Runtime (called from entrypoint.sh / OpenShell env script via
``agentic-ci enable-plugins``):

- :func:`enable_plugins` — reads ``AGENT_ENABLED_PLUGINS`` and disables
  unwanted plugins via harness-specific mechanisms.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from agentic_ci.git import clone_repo

DEFAULT_MANIFEST_PATH = "/usr/local/share/agentic-ci/plugin-skills.manifest.json"

_FALLBACK_SKILL_DIRS = [".claude/skills", ".opencode/skills", "skills"]


def _manifest_path() -> Path:
    return Path(os.environ.get("PLUGIN_SKILLS_MANIFEST", DEFAULT_MANIFEST_PATH))


def _find_skill_names(root: Path) -> list[str]:
    """Return sorted skill names found under a directory tree.

    A skill name is the parent directory name of each SKILL.md file.
    """
    names = set()
    for skill_md in root.rglob("SKILL.md"):
        names.add(skill_md.parent.name)
    return sorted(names)


def _copy_tree(src: Path, dest: Path) -> None:
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _check_unmatched(wanted: set[str], matched: set[str]) -> None:
    unmatched = sorted(wanted - matched)
    if not unmatched:
        return
    if matched:
        safe_matched = ", ".join(sorted(matched))
        if len(safe_matched) > 200:
            safe_matched = safe_matched[:200] + "..."
        print(f"Matched: {safe_matched}", file=sys.stderr)
    safe_names = ", ".join(unmatched)
    if len(safe_names) > 200:
        safe_names = safe_names[:200] + "..."
    print(
        f"ERROR: unknown plugin(s) in AGENT_ENABLED_PLUGINS: {safe_names}",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Build-time: install plugins
# ---------------------------------------------------------------------------


def install_claude_plugins(
    seed_dir: Path,
    manifest_path: Path | None = None,
) -> None:
    """Install all plugins from the seed directory using ``claude plugin install``.

    *seed_dir* is the ``CLAUDE_CODE_PLUGIN_CACHE_DIR`` populated by
    ``claude plugin marketplace add``.
    """
    manifest_path = manifest_path or _manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, list[str]] = {}

    for mkt_json in sorted(seed_dir.glob("marketplaces/*/.claude-plugin/marketplace.json")):
        data = json.loads(mkt_json.read_text())
        mkt_name = data["name"]

        for entry in data.get("plugins", []):
            name = entry["name"]
            plugin_id = f"{name}@{mkt_name}"
            print(f"==> Installing {plugin_id}")

            result = subprocess.run(
                ["claude", "plugin", "install", plugin_id],
                capture_output=False,
            )
            if result.returncode != 0:
                print(f"WARN: failed to install {name}")
                continue

            cache_dir = seed_dir / "cache" / mkt_name / name
            if cache_dir.is_dir():
                version_dirs = sorted(d for d in cache_dir.iterdir() if d.is_dir())
                if version_dirs:
                    skill_names = _find_skill_names(version_dirs[-1])
                    if skill_names:
                        manifest[name] = skill_names

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"==> Manifest written to {manifest_path}")


def install_opencode_skills(
    marketplace_json: Path,
    skills_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> None:
    """Clone plugin repos and copy SKILL.md files into *skills_dir*.

    *marketplace_json* is the path to the marketplace.json file from the
    skills registry.
    """
    if skills_dir is None:
        if os.environ.get("OPENCODE_SKILLS_DIR"):
            skills_dir = Path(os.environ["OPENCODE_SKILLS_DIR"])
        else:
            base = Path(os.environ.get("OPENCODE_CONFIG_DIR", Path.home() / ".config" / "opencode"))
            skills_dir = base / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = manifest_path or _manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, list[str]] = {}

    data = json.loads(marketplace_json.read_text())
    for entry in data.get("plugins", []):
        name = entry["name"]
        repo = entry["source"]["repo"]
        ref = entry["source"].get("ref", "main")
        url = f"https://github.com/{repo}.git"

        print(f"==> Installing skills from {name} ({repo} @ {ref})")

        with tempfile.TemporaryDirectory() as tmpdir:
            clone_dir = Path(tmpdir) / "src"
            if not clone_repo(url, clone_dir, branch=ref, depth=1):
                print(f"WARN: failed to clone {name}")
                continue

            skills_sources: list[Path] = []

            explicit_paths = entry.get("skills", [])
            if explicit_paths:
                for sp in explicit_paths:
                    sp = sp.removeprefix("./")
                    candidate = clone_dir / sp
                    if candidate.is_dir():
                        _copy_tree(candidate, skills_dir)
                        skills_sources.append(candidate)

            if not skills_sources:
                for fallback in _FALLBACK_SKILL_DIRS:
                    candidate = clone_dir / fallback
                    if candidate.is_dir():
                        _copy_tree(candidate, skills_dir)
                        skills_sources.append(candidate)
                        break

            if not skills_sources:
                print(f"  No skills found in {name}")
                continue

            all_skill_names: set[str] = set()
            for src in skills_sources:
                all_skill_names.update(_find_skill_names(src))
            if all_skill_names:
                manifest[name] = sorted(all_skill_names)

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"==> Skills installed to {skills_dir}")
    print(f"==> Manifest written to {manifest_path}")


# ---------------------------------------------------------------------------
# Runtime: filter plugins
# ---------------------------------------------------------------------------


def _filter_claude(wanted: set[str]) -> None:
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    settings_path = claude_home / "settings.json"

    if not settings_path.is_file():
        print(
            f"WARNING: AGENT_ENABLED_PLUGINS is set but {settings_path} not found",
            file=sys.stderr,
        )
        return

    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, ValueError):
        print(
            f"WARNING: {settings_path} contains invalid JSON, resetting to empty",
            file=sys.stderr,
        )
        settings = {}

    enabled = settings.get("enabledPlugins", {})
    if not enabled:
        return

    matched: set[str] = set()
    for key in enabled:
        name = key.split("@")[0]
        if name in wanted:
            enabled[key] = True
            matched.add(name)
        else:
            enabled[key] = False

    _check_unmatched(wanted, matched)

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def _filter_opencode(wanted: set[str]) -> None:
    config_dir = Path(os.environ.get("OPENCODE_CONFIG_DIR", Path.home() / ".config" / "opencode"))
    config_path = config_dir / "opencode.json"

    manifest_path = _manifest_path()
    if not manifest_path.is_file():
        print(
            f"WARNING: AGENT_ENABLED_PLUGINS is set but {manifest_path} not found",
            file=sys.stderr,
        )
        return

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except (json.JSONDecodeError, ValueError):
        print(
            f"WARNING: {manifest_path} contains invalid JSON",
            file=sys.stderr,
        )
        return

    matched = wanted & set(manifest.keys())
    _check_unmatched(wanted, matched)

    unwanted_skills = []
    for plugin_name, skills in manifest.items():
        if plugin_name not in wanted:
            unwanted_skills.extend(skills)

    if not unwanted_skills:
        return

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        config = {}

    permissions = config.setdefault("permission", {})
    skill_perms = permissions.setdefault("skill", {})
    for skill_name in unwanted_skills:
        skill_perms[skill_name] = "deny"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def enable_plugins() -> None:
    """Filter active plugins based on ``AGENT_ENABLED_PLUGINS``."""
    wanted_csv = os.environ.get("AGENT_ENABLED_PLUGINS", "")
    if not wanted_csv:
        return

    wanted = set(p.strip() for p in wanted_csv.split(",") if p.strip())
    if not wanted:
        return

    agent_tool = os.environ.get("AGENT_TOOL")
    if not agent_tool:
        print("ERROR: AGENT_TOOL must be set (claude or opencode)", file=sys.stderr)
        sys.exit(1)
    if agent_tool == "opencode":
        _filter_opencode(wanted)
    elif agent_tool == "claude":
        _filter_claude(wanted)
    else:
        print(f"ERROR: unknown AGENT_TOOL: {agent_tool!r}", file=sys.stderr)
        sys.exit(1)
