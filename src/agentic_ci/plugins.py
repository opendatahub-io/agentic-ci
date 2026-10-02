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
- :func:`find_skill_dir`: locates the directory of one installed skill
  (``agentic-ci skill-dir``), which the backends export as
  ``CLAUDE_SKILL_DIR`` for harnesses that do not set it themselves.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TypeGuard

from agentic_ci.git import clone_repo

DEFAULT_MANIFEST_PATH = "/usr/local/share/agentic-ci/plugin-skills.manifest.json"

_FALLBACK_SKILL_DIRS = [".agents/skills", ".claude/skills", ".opencode/skills", "skills"]

# Plugin manifests Codex reads ``skills`` paths from, first match wins.
_CODEX_MANIFEST_PATHS = [
    ".codex-plugin/plugin.json",
    ".claude-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
]

# Directory levels below a skills root that Codex's skill loader walks.
_CODEX_SKILL_SCAN_DEPTH = 6

# A skill name :func:`find_skill_dir` accepts: one path component that
# cannot be mistaken for a command-line option.
_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Longest skill directory :func:`parse_skill_dir` accepts (Linux PATH_MAX).
_SKILL_DIR_MAX_LEN = 4096

# Seconds a ``codex ... --json`` query may take.
_CODEX_JSON_TIMEOUT = 120

# ``codex plugin list`` entry fields that :func:`_codex_installed_path` builds a path from.
_CODEX_ENTRY_PATH_KEYS = ("installedPath", "marketplaceName", "name", "version")


def _manifest_path() -> Path:
    return Path(os.environ.get("PLUGIN_SKILLS_MANIFEST", DEFAULT_MANIFEST_PATH))


def _find_skill_names(
    root: Path, max_depth: int | None = None, skip_hidden: bool = False
) -> list[str]:
    """Return sorted skill names found under a directory tree.

    A skill name is the parent directory name of each SKILL.md file.
    """
    return sorted({path.name for path in _find_skill_dirs(root, max_depth, skip_hidden)})


def _find_skill_dirs(
    root: Path, max_depth: int | None = None, skip_hidden: bool = False
) -> list[Path]:
    """Return directories containing a non-symlink ``SKILL.md``.

    *max_depth* stops the walk that many directory levels below *root*, and
    *skip_hidden* skips dot-directories below it.
    """
    skill_dirs: list[Path] = []
    if root.is_symlink():
        return skill_dirs
    for current_root, dir_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current_root)
        if max_depth is not None and len(current_path.relative_to(root).parts) >= max_depth:
            dir_names.clear()
        dir_names[:] = [
            name
            for name in dir_names
            if not (current_path / name).is_symlink() and not (skip_hidden and name.startswith("."))
        ]
        skill_md = current_path / "SKILL.md"
        if "SKILL.md" in file_names and not skill_md.is_symlink():
            skill_dirs.append(current_path)
    return skill_dirs


def _declared_skill_paths(value: object) -> list[str]:
    """Return a ``skills`` field (one path or a list of paths) as a list."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [path for path in value if isinstance(path, str)]
    return []


def _plugin_json_skill_paths(manifest: Path) -> list[str]:
    """Return the ``skills`` paths a plugin manifest declares, if any."""
    try:
        data = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return _declared_skill_paths(data.get("skills"))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_claude_skill(path: Path, root: Path) -> bool:
    # os.path.realpath, unlike Path.resolve() before Python 3.13, does not
    # raise on symlink loops; is_file() then rejects the result.
    skill_md = Path(os.path.realpath(path / "SKILL.md"))
    return _is_within(skill_md, root) and skill_md.is_file()


def _claude_skill_names(plugin_root: Path, entry: dict) -> list[str]:
    """Return the skills Claude Code loads from an installed plugin.

    Claude Code loads the direct children of the plugin's ``skills/``
    directory plus the paths declared by the marketplace *entry* and by
    ``.claude-plugin/plugin.json``; a declared path that holds a
    ``SKILL.md`` is a skill itself. Claude Code follows symlinks, which
    some plugins use to re-export skills from sibling plugins, so this
    does too, but only to targets inside the plugin root.
    """
    root = Path(os.path.realpath(plugin_root))
    declared = _declared_skill_paths(entry.get("skills"))
    declared += _plugin_json_skill_paths(root / ".claude-plugin" / "plugin.json")
    names: set[str] = set()
    for path in ["skills", *declared]:
        skills_root = Path(os.path.realpath(root / path.removeprefix("./")))
        if not _is_within(skills_root, root) or not skills_root.is_dir():
            continue
        if _is_claude_skill(skills_root, root):
            names.add(skills_root.name)
            continue
        for child in skills_root.iterdir():
            if _is_claude_skill(child, root):
                names.add(child.name)
    return sorted(names)


def _codex_skill_root(plugin_root: Path, path: str) -> Path | None:
    """Return a manifest ``skills`` path if Codex accepts it, else ``None``.

    Codex only accepts non-empty ``./``-relative paths without ``..``.
    """
    relative = path.removeprefix("./")
    if relative == path or not relative or relative.startswith("/"):
        return None
    if ".." in relative.split("/"):
        return None
    return plugin_root / relative


def _codex_skill_names(plugin_root: Path) -> list[str]:
    """Return the names of the skills Codex loads from an installed native plugin.

    See :func:`_codex_skill_dirs` for the rules.
    """
    return sorted({path.name for path in _codex_skill_dirs(plugin_root)})


def _codex_skill_dirs(plugin_root: Path) -> list[Path]:
    """Return the directories of the skills Codex loads from an installed native plugin.

    Codex takes ``skills`` paths from the first manifest in
    ``_CODEX_MANIFEST_PATHS``. They replace the default ``skills/``
    directory, ``.codex-plugin/migrated-command-skills`` is always added,
    and each root is searched ``_CODEX_SKILL_SCAN_DEPTH`` levels deep,
    skipping hidden directories below it. Codex drops symlinks when it
    installs a plugin, so :func:`_find_skill_names` skipping them matches.
    These rules follow ``codex-rs/core-plugins/src/loader.rs`` and
    ``codex-rs/ext/skills/src/loader/discovery.rs`` in openai/codex.
    """
    declared: list[str] = []
    for manifest in _CODEX_MANIFEST_PATHS:
        if (plugin_root / manifest).is_file():
            declared = _plugin_json_skill_paths(plugin_root / manifest)
            break
    roots: list[Path] = []
    for path in declared:
        skills_root = _codex_skill_root(plugin_root, path)
        if skills_root is not None:
            roots.append(skills_root)
    if not roots:
        roots.append(plugin_root / "skills")
    roots.append(plugin_root / ".codex-plugin" / "migrated-command-skills")
    dirs: list[Path] = []
    for skills_root in roots:
        dirs.extend(
            _find_skill_dirs(skills_root, max_depth=_CODEX_SKILL_SCAN_DEPTH, skip_hidden=True)
        )
    return dirs


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


def _warn_enabled_without_skills(enabled: set[str], manifest: dict[str, list[str]] | None) -> None:
    """Warn about enabled plugins that the manifest lists no skills for.

    Such a plugin is installed but contributes no skills, which is expected
    for MCP-only plugins and otherwise a sign of a broken registry entry.
    """
    if manifest is None:
        return
    empty = sorted(name for name in enabled if not manifest.get(name))
    if empty:
        print(
            f"WARNING: enabled plugin(s) provide no skills: {', '.join(empty)}",
            file=sys.stderr,
        )


def _warn_plugins_without_skills(names: list[str], manifest: dict[str, list[str]]) -> None:
    """Print one build-log line naming the plugins that provide no skills."""
    empty = sorted({name for name in names if name not in manifest})
    if empty:
        print(f"WARN: {len(empty)} plugin(s) provide no skills: {', '.join(empty)}")


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
    names: list[str] = []

    for mkt_json in sorted(seed_dir.glob("marketplaces/*/.claude-plugin/marketplace.json")):
        data = json.loads(mkt_json.read_text())
        mkt_name = data["name"]

        for entry in data.get("plugins", []):
            name = entry["name"]
            names.append(name)
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
                    skill_names = _claude_skill_names(version_dirs[-1], entry)
                    if skill_names:
                        manifest[name] = skill_names

    _warn_plugins_without_skills(names, manifest)
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
                owners = sorted(
                    plugin for plugin, skills in manifest.items() if set(skills) & set(collisions)
                )
                owned_by = f" (already installed by {', '.join(owners)})" if owners else ""
                print(
                    f"WARN: skipping {name}; destination path collision(s): "
                    f"{', '.join(collisions)}{owned_by}"
                )
                continue

            for skill_name, skill_dir in skill_dirs.items():
                _copy_tree(skill_dir, skills_dir / skill_name)
            if skill_dirs:
                manifest[name] = sorted(skill_dirs)

    _warn_plugins_without_skills([entry["name"] for entry in data.get("plugins", [])], manifest)
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


def _run_codex_json(args: list[str], env: Mapping[str, str] | None = None) -> dict | None:
    """Run ``codex <args> --json`` and return its JSON object, or ``None``.

    *env* replaces the process environment for the call (default: inherit).
    """
    try:
        result = subprocess.run(
            ["codex", *args, "--json"],
            capture_output=True,
            text=True,
            timeout=_CODEX_JSON_TIMEOUT,
            env=dict(env) if env is not None else None,
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


def _codex_home(env: Mapping[str, str] | None = None) -> Path:
    """Return ``$CODEX_HOME`` from *env* (default ``os.environ``), else ``~/.codex``."""
    env = os.environ if env is None else env
    return Path(env.get("CODEX_HOME") or Path.home() / ".codex")


def _codex_installed_path(
    entry: dict, added_path: Path | None, codex_home: Path | None = None
) -> Path | None:
    """Resolve where Codex installed a plugin listed by ``codex plugin list``.

    Prefers the path reported by ``codex plugin add``, then an ``installedPath``
    field on the list entry, and finally the Codex plugin cache layout
    ``$CODEX_HOME/plugins/cache/<marketplace>/<name>/<version>``. *codex_home*
    defaults to ``$CODEX_HOME`` from the process environment.
    """
    candidates: list[Path] = []
    if added_path is not None:
        candidates.append(added_path)
    listed_path = entry.get("installedPath", "")
    if listed_path:
        candidates.append(Path(listed_path))
    marketplace = entry.get("marketplaceName", "")
    name = entry.get("name", "")
    version = entry.get("version", "")
    if marketplace and name and version:
        if codex_home is None:
            codex_home = _codex_home()
        candidates.append(codex_home / "plugins" / "cache" / marketplace / name / version)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


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

    # ``codex plugin add --json`` reports ``installedPath``; ``codex plugin
    # list --json`` does not, so remember the path from the add result.
    installed_paths: dict[str, Path] = {}
    for entry in available:
        name = _safe_codex_operand(entry.get("name"), "plugin name")
        selector = _safe_codex_operand(entry.get("pluginId"), "plugin selector")
        if not selector and name:
            selector = f"{name}@{marketplace_name}"
        selector = _safe_codex_operand(selector, "plugin selector")
        if not selector:
            continue
        print(f"==> Installing {selector}")
        added_plugin = _run_codex_json(["plugin", "add", selector])
        if added_plugin is None:
            print(f"WARN: failed to install {selector}")
            continue
        installed_path = added_plugin.get("installedPath")
        if name and isinstance(installed_path, str) and installed_path:
            installed_paths[name] = Path(installed_path)

    installed = _run_codex_json(["plugin", "list"])
    manifest: dict[str, list[str]] = {}
    for entry in installed.get("installed", []) if installed else []:
        if entry.get("marketplaceName") != marketplace_name:
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        installed_path = _codex_installed_path(entry, installed_paths.get(name))
        if installed_path is None:
            print(f"  WARN: no install path known for {name}; skipping in manifest")
            continue
        skill_names = _codex_skill_names(installed_path)
        if skill_names:
            manifest[name] = skill_names

    _warn_plugins_without_skills(
        [entry["name"] for entry in available if isinstance(entry.get("name"), str)], manifest
    )
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
    _warn_enabled_without_skills(matched, _load_plugin_manifest())

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
    loaded_manifest = _load_plugin_manifest()
    manifest = loaded_manifest or {}

    native_names = {
        entry.get("name") for entry in native_plugins if isinstance(entry.get("name"), str)
    }
    matched = wanted & (native_names | set(manifest))
    _check_unmatched(wanted, matched)
    _warn_enabled_without_skills(matched, loaded_manifest)

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


# ---------------------------------------------------------------------------
# Runtime: locate a skill
# ---------------------------------------------------------------------------


def is_skill_name(name: object) -> TypeGuard[str]:
    """Whether *name* is a skill name :func:`find_skill_dir` looks up."""
    return isinstance(name, str) and _SKILL_NAME_RE.fullmatch(name) is not None


def parse_skill_dir(output: str) -> str | None:
    """Return the directory ``agentic-ci skill-dir`` printed, or ``None``.

    *output* comes from inside a container, so only one absolute path of
    printable characters is accepted.
    """
    skill_dir = output.removesuffix("\n")
    if not skill_dir.startswith("/") or len(skill_dir) > _SKILL_DIR_MAX_LEN:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in skill_dir):
        return None
    return skill_dir


def _codex_skill_dir_candidates(name: str, env: Mapping[str, str]) -> list[Path]:
    """Return every directory Codex may load the skill *name* from.

    Covers the native plugins ``codex plugin list`` reports (after
    ``enable-plugins`` has removed the unwanted ones) and the skills
    compatibility layer in ``$CODEX_HOME/skills``.
    """
    codex_home = _codex_home(env)
    candidates: list[Path] = []
    installed = _run_codex_json(["plugin", "list"], env=env)
    entries = installed.get("installed", []) if installed else []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        if any(not isinstance(entry.get(key, ""), str) for key in _CODEX_ENTRY_PATH_KEYS):
            continue
        plugin_root = _codex_installed_path(entry, None, codex_home)
        if plugin_root is None:
            continue
        candidates.extend(path for path in _codex_skill_dirs(plugin_root) if path.name == name)
    compat = codex_home / "skills" / name
    if not compat.is_symlink() and (compat / "SKILL.md").is_file():
        candidates.append(compat)
    return candidates


def _opencode_skill_dir_candidates(name: str, env: Mapping[str, str]) -> list[Path]:
    """Return the OpenCode skills directory entry for *name*, if installed."""
    config_dir = Path(env.get("OPENCODE_CONFIG_DIR") or Path.home() / ".config" / "opencode")
    skill_dir = config_dir / "skills" / name
    if not skill_dir.is_symlink() and (skill_dir / "SKILL.md").is_file():
        return [skill_dir]
    return []


def find_skill_dir(name: str, env: Mapping[str, str] | None = None) -> Path | None:
    """Return the installed directory of the skill *name*, or ``None``.

    Skills refer to their own scripts and schemas as ``${CLAUDE_SKILL_DIR}/...``.
    Claude Code fills that in itself; Codex and OpenCode do not, so the
    backends export the directory this returns. It looks where
    ``install-plugins`` puts skills for ``AGENT_TOOL`` in *env* (default
    ``os.environ``): Codex native plugins and ``$CODEX_HOME/skills``, or
    ``$OPENCODE_CONFIG_DIR/skills``. It returns ``None`` for any other
    ``AGENT_TOOL``, for a name that is not one path component, when no
    install has the skill, and when more than one does, since the agent
    could then load either and a guess may point at the wrong scripts.
    """
    env = os.environ if env is None else env
    if not is_skill_name(name):
        return None
    agent_tool = env.get("AGENT_TOOL")
    if agent_tool == "codex":
        candidates = _codex_skill_dir_candidates(name, env)
    elif agent_tool == "opencode":
        candidates = _opencode_skill_dir_candidates(name, env)
    else:
        return None
    unique = {Path(os.path.abspath(path)) for path in candidates}
    if len(unique) != 1:
        return None
    return unique.pop()


def print_skill_dir(name: str) -> int:
    """Print the directory :func:`find_skill_dir` finds for *name*; 1 when it finds none.

    Callers capture stdout as the directory, so it carries nothing else: the
    lookup's warnings (a failed ``codex plugin list``, say) go to stderr, and
    a directory :func:`parse_skill_dir` would reject is not printed.
    """
    with contextlib.redirect_stdout(sys.stderr):
        skill_dir = find_skill_dir(name)
    if skill_dir is None or parse_skill_dir(str(skill_dir)) is None:
        print(f"agentic-ci: no unique installed skill named {name!r}", file=sys.stderr)
        return 1
    print(skill_dir)
    return 0
