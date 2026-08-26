"""Plugin and skill installation and runtime filtering.

Provides three build-time operations and one runtime operation:

Build-time (called from Containerfiles via ``agentic-ci install-plugins``):

- :func:`install_claude_plugins` — uses native Claude Code CLI to install
  plugins from a marketplace seed directory.
- :func:`install_opencode_skills` — clones plugin repos and copies SKILL.md
  files into OpenCode's skills directory.
- :func:`install_codex_plugins` — installs native Codex plugins when the
  marketplace supports them, with a skills-only fallback for legacy
  marketplaces.

All generate a plugin-to-skill manifest at a well-known path.

Runtime (called from entrypoint.sh / OpenShell env script via
``agentic-ci enable-plugins``):

- :func:`enable_plugins` — reads ``AGENT_ENABLED_PLUGINS`` and disables
  unwanted plugins via harness-specific mechanisms.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from agentic_ci.git import clone_repo

DEFAULT_MANIFEST_PATH = "/usr/local/share/agentic-ci/plugin-skills.manifest.json"

_FALLBACK_SKILL_DIRS = [".agents/skills", ".claude/skills", ".opencode/skills", "skills"]


def _manifest_path() -> Path:
    return Path(os.environ.get("PLUGIN_SKILLS_MANIFEST", DEFAULT_MANIFEST_PATH))


def _find_skill_names(root: Path) -> list[str]:
    """Return sorted skill names found under a directory tree.

    A skill name is the parent directory name of each SKILL.md file.
    """
    return sorted({path.name for path in _find_skill_dirs(root)})


def _find_skill_dirs(root: Path) -> list[Path]:
    """Return directories containing a non-symlink ``SKILL.md``."""
    skill_dirs: list[Path] = []
    if root.is_symlink():
        return skill_dirs
    for current_root, dir_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current_root)
        dir_names[:] = [name for name in dir_names if not (current_path / name).is_symlink()]
        skill_md = current_path / "SKILL.md"
        if "SKILL.md" in file_names and not skill_md.is_symlink():
            skill_dirs.append(current_path)
    return skill_dirs


def _copy_tree(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_symlink():
            print(f"  WARN: skipping symlink in skills tree: {item}")
            continue
        target = dest / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_tree(item, target)
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
        source = entry.get("source", {})
        repo = source.get("repo")
        ref = source.get("ref", "main")
        source_path = source.get("path", "")
        if repo:
            url = f"https://github.com/{repo}.git"
            source_label = repo
        elif source.get("source") == "git-subdir" and source.get("url"):
            url = source["url"]
            source_label = url
        else:
            print(f"WARN: skipping {name}; unsupported marketplace source")
            continue

        print(f"==> Installing skills from {name} ({source_label} @ {ref})")

        with tempfile.TemporaryDirectory() as tmpdir:
            clone_dir = Path(tmpdir) / "src"
            if not clone_repo(url, clone_dir, branch=ref, depth=1):
                print(f"WARN: failed to clone {name}")
                continue

            skills_sources: list[Path] = []
            clone_root = clone_dir.resolve()
            source_root = (clone_root / source_path.removeprefix("./")).resolve()
            try:
                source_root.relative_to(clone_root)
            except ValueError:
                print(f"  WARN: rejected source path outside repository: {source_path}")
                continue
            if not source_root.is_dir():
                print(f"  WARN: source path not found: {source_path}")
                continue

            explicit_paths = entry.get("skills", [])
            if explicit_paths:
                for sp in explicit_paths:
                    sp = sp.removeprefix("./")
                    candidate = (source_root / sp).resolve()
                    try:
                        candidate.relative_to(source_root)
                    except ValueError:
                        print(f"  WARN: rejected skills path outside repository: {sp}")
                        continue
                    if candidate.is_dir():
                        skills_sources.append(candidate)

            if not skills_sources:
                for fallback in _FALLBACK_SKILL_DIRS:
                    candidate = (source_root / fallback).resolve()
                    try:
                        candidate.relative_to(source_root)
                    except ValueError:
                        continue
                    if candidate.is_dir():
                        skills_sources.append(candidate)

            if not skills_sources:
                print(f"  No skills found in {name}")
                continue

            skill_dirs: dict[str, Path] = {}
            duplicate_skill_names: set[str] = set()
            for src in skills_sources:
                for skill_dir in _find_skill_dirs(src):
                    skill_name = skill_dir.name
                    if skill_name in skill_dirs:
                        duplicate_skill_names.add(skill_name)
                    else:
                        skill_dirs[skill_name] = skill_dir

            owned_skill_names = {
                skill_name for skill_names in manifest.values() for skill_name in skill_names
            }
            collisions = sorted(duplicate_skill_names | (set(skill_dirs) & owned_skill_names))
            if collisions:
                print(
                    f"WARN: skipping {name}; destination path collision(s): {', '.join(collisions)}"
                )
                continue

            for skill_name, skill_dir in skill_dirs.items():
                _copy_tree(skill_dir, skills_dir / skill_name)
            if skill_dirs:
                manifest[name] = sorted(skill_dirs)

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"==> Skills installed to {skills_dir}")
    print(f"==> Manifest written to {manifest_path}")


def _codex_marketplace_root(marketplace_json: Path) -> Path:
    if marketplace_json.parent.name == ".claude-plugin":
        return marketplace_json.parent.parent
    if (
        marketplace_json.parent.name == "plugins"
        and marketplace_json.parent.parent.name == ".agents"
    ):
        return marketplace_json.parent.parent.parent
    return marketplace_json.parent


def _run_codex_json(args: list[str]) -> dict | None:
    try:
        result = subprocess.run(
            ["codex", *args, "--json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  WARN: failed to run codex {' '.join(args)}: {exc}")
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or "no stderr output"
        print(f"  WARN: codex {' '.join(args)} failed (exit {result.returncode}): {detail}")
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _safe_codex_operand(value: object, description: str) -> str | None:
    """Return a safe dynamic Codex operand, rejecting option-like values."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("-"):
        print(
            f"  WARN: rejecting Codex {description} that starts with '-'",
            file=sys.stderr,
        )
        return None
    return value


def install_codex_plugins(
    marketplace_json: Path,
    skills_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> None:
    """Install plugins for Codex from a marketplace.

    Native Codex plugins are preferred. Legacy Claude-compatible marketplaces
    that do not expose Codex plugin packages fall back to installing their
    skills under ``CODEX_HOME/skills``.
    """
    marketplace_root = _codex_marketplace_root(marketplace_json)
    added = _run_codex_json(["plugin", "marketplace", "add", str(marketplace_root)])
    marketplace_name = _safe_codex_operand(
        added.get("marketplaceName") if added else None,
        "marketplace name",
    )
    if added and not marketplace_name:
        print(
            "  WARN: codex plugin marketplace add succeeded but returned no "
            "marketplaceName; falling back to skills compatibility layer"
        )

    listing = None
    if marketplace_name:
        listing = _run_codex_json(
            ["plugin", "list", "--available", "--marketplace", marketplace_name]
        )
    available = listing.get("available", []) if listing else []

    if not available:
        print("==> Marketplace has no native Codex plugins; installing skills compatibility layer")
        if skills_dir is None:
            codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            skills_dir = codex_home / "skills"
        install_opencode_skills(
            marketplace_json,
            skills_dir=skills_dir,
            manifest_path=manifest_path,
        )
        return

    for entry in available:
        name = _safe_codex_operand(entry.get("name"), "plugin name")
        selector = _safe_codex_operand(entry.get("pluginId"), "plugin selector")
        if not selector and name:
            selector = f"{name}@{marketplace_name}"
        selector = _safe_codex_operand(selector, "plugin selector")
        if not selector:
            continue
        print(f"==> Installing {selector}")
        if _run_codex_json(["plugin", "add", selector]) is None:
            print(f"WARN: failed to install {selector}")

    installed = _run_codex_json(["plugin", "list"])
    manifest: dict[str, list[str]] = {}
    for entry in installed.get("installed", []) if installed else []:
        if entry.get("marketplaceName") != marketplace_name:
            continue
        name = entry.get("name")
        installed_path = entry.get("installedPath")
        if not name or not installed_path:
            continue
        skill_names = _find_skill_names(Path(installed_path))
        if skill_names:
            manifest[name] = skill_names

    manifest_path = manifest_path or _manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
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

    manifest = _load_plugin_manifest(warn_if_missing=True)
    if manifest is None:
        return

    matched = wanted & set(manifest.keys())
    _check_unmatched(wanted, matched)

    wanted_skills: set[str] = set()
    for plugin_name, skills in manifest.items():
        if plugin_name in wanted:
            wanted_skills.update(skills)

    skills_dir = config_dir / "skills"
    if skills_dir.is_dir():
        for entry in skills_dir.iterdir():
            if entry.name not in wanted_skills:
                if entry.is_symlink():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry)


def _load_plugin_manifest(warn_if_missing: bool = False) -> dict[str, list[str]] | None:
    """Load the plugin-to-skills manifest.

    Returns the parsed mapping, or ``None`` when the manifest is missing or
    unreadable (invalid JSON). Callers for which the manifest is the sole
    source of truth (OpenCode) should treat ``None`` as "do not filter";
    callers with another source of truth (Codex native plugins) can fall back
    to ``{}``. A manifest whose top-level JSON is not an object is treated as
    an empty mapping.

    Set ``warn_if_missing`` to emit the "manifest not found" warning when the
    file is absent (OpenCode); Codex loads it silently.
    """
    manifest_path = _manifest_path()
    if not manifest_path.is_file():
        if warn_if_missing:
            print(
                f"WARNING: AGENT_ENABLED_PLUGINS is set but {manifest_path} not found",
                file=sys.stderr,
            )
        return None
    try:
        with open(manifest_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError):
        print(
            f"WARNING: {manifest_path} contains invalid JSON",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict):
        return {}
    return {
        name: [skill for skill in skills if isinstance(skill, str)]
        for name, skills in data.items()
        if isinstance(name, str) and isinstance(skills, list)
    }


def _filter_codex(wanted: set[str]) -> None:
    installed = _run_codex_json(["plugin", "list"])
    native_plugins = installed.get("installed", []) if installed else []
    manifest = _load_plugin_manifest() or {}

    native_names = {
        entry.get("name") for entry in native_plugins if isinstance(entry.get("name"), str)
    }
    matched = wanted & (native_names | set(manifest))
    _check_unmatched(wanted, matched)

    remove_failures: list[str] = []
    for entry in native_plugins:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in wanted:
            continue
        selector = _safe_codex_operand(entry.get("pluginId"), "plugin selector")
        if not selector:
            marketplace = _safe_codex_operand(entry.get("marketplaceName"), "marketplace name")
            safe_name = _safe_codex_operand(name, "plugin name")
            selector = f"{safe_name}@{marketplace}" if marketplace and safe_name else safe_name
        if not selector:
            remove_failures.append(str(name))
            continue
        if _run_codex_json(["plugin", "remove", selector]) is None:
            print(f"ERROR: failed to disable Codex plugin {selector}", file=sys.stderr)
            remove_failures.append(selector)

    if remove_failures:
        print(
            "ERROR: could not enforce AGENT_ENABLED_PLUGINS; still active: "
            + ", ".join(sorted(remove_failures)),
            file=sys.stderr,
        )
        sys.exit(1)

    wanted_skills: set[str] = set()
    for plugin_name, skills in manifest.items():
        if plugin_name in wanted:
            wanted_skills.update(skills)

    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    skills_dir = codex_home / "skills"
    if skills_dir.is_dir():
        managed_skills = {skill for skills in manifest.values() for skill in skills}
        for entry in skills_dir.iterdir():
            if entry.name in managed_skills and entry.name not in wanted_skills:
                if entry.is_symlink():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry)


def enable_plugins() -> None:
    """Filter active plugins based on ``AGENT_ENABLED_PLUGINS``."""
    wanted_csv = os.environ.get("AGENT_ENABLED_PLUGINS", "")
    if not wanted_csv:
        return

    if not re.match(r"^[a-zA-Z0-9_,. -]+$", wanted_csv):
        print(
            f"ERROR: AGENT_ENABLED_PLUGINS contains invalid characters: {wanted_csv!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    wanted = set(p.strip() for p in wanted_csv.split(",") if p.strip())
    if not wanted:
        return

    agent_tool = os.environ.get("AGENT_TOOL")
    if not agent_tool:
        print("ERROR: AGENT_TOOL must be set (claude, opencode, or codex)", file=sys.stderr)
        sys.exit(1)
    if agent_tool == "opencode":
        _filter_opencode(wanted)
    elif agent_tool == "claude":
        _filter_claude(wanted)
    elif agent_tool == "codex":
        _filter_codex(wanted)
    else:
        print(f"ERROR: unknown AGENT_TOOL: {agent_tool!r}", file=sys.stderr)
        sys.exit(1)
