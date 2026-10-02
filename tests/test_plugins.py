"""Tests for agentic_ci.plugins — enable_plugins and install_opencode_skills."""

import json
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from agentic_ci import cli
from agentic_ci.plugins import (
    _claude_skill_names,
    _codex_installed_path,
    _codex_marketplace_root,
    _codex_skill_names,
    _filter_codex,
    _find_skill_names,
    _run_codex_json,
    enable_plugins,
    find_skill_dir,
    install_claude_plugins,
    install_codex_plugins,
    install_opencode_skills,
    parse_skill_dir,
)


def test_codex_installed_path_treats_empty_codex_home_as_unset(monkeypatch, tmp_path):
    # Same rule as find_skill_dir's lookup (_codex_home): an empty CODEX_HOME
    # means ~/.codex, not the current directory.
    home = tmp_path / "home"
    plugin_root = home / ".codex" / "plugins" / "cache" / "market" / "plugin" / "1.0.0"
    plugin_root.mkdir(parents=True)
    cwd = tmp_path / "cwd"
    (cwd / "plugins" / "cache" / "market" / "plugin" / "1.0.0").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", "")
    entry = {"marketplaceName": "market", "name": "plugin", "version": "1.0.0"}
    assert _codex_installed_path(entry, None) == plugin_root


def _make_skill(path):
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {path.name}\n---\n")


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# -- _find_skill_names -------------------------------------------------------


class TestFindSkillNames:
    def test_finds_skills(self, tmp_path):
        (tmp_path / "greet").mkdir()
        (tmp_path / "greet" / "SKILL.md").touch()
        (tmp_path / "review").mkdir()
        (tmp_path / "review" / "SKILL.md").touch()
        assert _find_skill_names(tmp_path) == ["greet", "review"]

    def test_empty_dir(self, tmp_path):
        assert _find_skill_names(tmp_path) == []

    def test_nested_skills(self, tmp_path):
        (tmp_path / "deep" / "nested").mkdir(parents=True)
        (tmp_path / "deep" / "nested" / "SKILL.md").touch()
        assert _find_skill_names(tmp_path) == ["nested"]

    def test_ignores_symlinked_skill_trees(self, tmp_path):
        source = tmp_path / "source"
        real_skill = source / "real" / "SKILL.md"
        real_skill.parent.mkdir(parents=True)
        real_skill.touch()
        outside = tmp_path / "outside" / "escaped" / "SKILL.md"
        outside.parent.mkdir(parents=True)
        outside.touch()
        (real_skill.parent / "escaped").symlink_to(outside.parent, target_is_directory=True)

        assert _find_skill_names(source) == ["real"]


# -- _claude_skill_names / _codex_skill_names ---------------------------------


class TestClaudeSkillNames:
    def test_loads_direct_children_of_default_skills_dir(self, tmp_path):
        _make_skill(tmp_path / "skills" / "review")
        _make_skill(tmp_path / "skills" / "group" / "nested")
        _make_skill(tmp_path / ".claude" / "skills" / "project-only")

        assert _claude_skill_names(tmp_path, {}) == ["review"]

    def test_declared_paths_supplement_default(self, tmp_path):
        _make_skill(tmp_path / "skills" / "default")
        _make_skill(tmp_path / "from-entry" / "entry-skill")
        _make_skill(tmp_path / "from-manifest" / "manifest-skill")
        _make_skill(tmp_path / "single")
        _write_json(tmp_path / ".claude-plugin" / "plugin.json", {"skills": "./from-manifest"})

        entry = {"skills": ["./from-entry", "./single"]}
        assert _claude_skill_names(tmp_path, entry) == [
            "default",
            "entry-skill",
            "manifest-skill",
            "single",
        ]

    def test_follows_symlinks_inside_plugin_root_only(self, tmp_path):
        plugin = tmp_path / "repo"
        _make_skill(plugin / "plugins" / "member" / "skills" / "shared")
        _make_skill(tmp_path / "outside" / "escaped")
        links = plugin / "plugins" / "umbrella" / "skills"
        links.mkdir(parents=True)
        (links / "shared").symlink_to("../../member/skills/shared", target_is_directory=True)
        (links / "escaped").symlink_to(tmp_path / "outside" / "escaped", target_is_directory=True)
        (links / "dangling").symlink_to("../../missing", target_is_directory=True)

        entry = {"skills": ["./plugins/umbrella/skills"]}
        assert _claude_skill_names(plugin, entry) == ["shared"]

    def test_ignores_paths_outside_plugin_root(self, tmp_path):
        plugin = tmp_path / "plugin"
        plugin.mkdir()
        _make_skill(tmp_path / "sibling" / "escaped")

        entry = {"skills": ["../sibling", str(tmp_path / "sibling")]}
        assert _claude_skill_names(plugin, entry) == []

    def test_ignores_symlink_loops(self, tmp_path):
        _make_skill(tmp_path / "skills" / "good")
        (tmp_path / "skills" / "self").symlink_to("self")
        (tmp_path / "loop").symlink_to("loop")

        assert _claude_skill_names(tmp_path, {"skills": ["./loop"]}) == ["good"]

    def test_ignores_invalid_declarations(self, tmp_path):
        _make_skill(tmp_path / "skills" / "review")
        (tmp_path / ".claude-plugin").mkdir()
        (tmp_path / ".claude-plugin" / "plugin.json").write_text("{not json")

        assert _claude_skill_names(tmp_path, {"skills": 42}) == ["review"]


class TestCodexSkillNames:
    def test_searches_default_skills_dir_recursively(self, tmp_path):
        _make_skill(tmp_path / "skills" / "group" / "nested")
        _make_skill(tmp_path / "docs" / "stray")

        assert _codex_skill_names(tmp_path) == ["nested"]

    def test_declared_paths_replace_default(self, tmp_path):
        _make_skill(tmp_path / "skills" / "default")
        _make_skill(tmp_path / "custom" / "declared")
        _write_json(tmp_path / ".claude-plugin" / "plugin.json", {"skills": "./custom"})

        assert _codex_skill_names(tmp_path) == ["declared"]

    def test_codex_manifest_takes_precedence(self, tmp_path):
        _make_skill(tmp_path / "from-codex" / "codex-skill")
        _make_skill(tmp_path / "from-claude" / "claude-skill")
        _write_json(tmp_path / ".codex-plugin" / "plugin.json", {"skills": ["./from-codex"]})
        _write_json(tmp_path / ".claude-plugin" / "plugin.json", {"skills": ["./from-claude"]})

        assert _codex_skill_names(tmp_path) == ["codex-skill"]

    @pytest.mark.parametrize("path", ["custom", "./", "./../outside", "./custom/../../outside"])
    def test_rejected_paths_fall_back_to_default(self, tmp_path, path):
        _make_skill(tmp_path / "skills" / "default")
        _make_skill(tmp_path / "custom" / "declared")
        _write_json(tmp_path / ".codex-plugin" / "plugin.json", {"skills": [path]})

        assert _codex_skill_names(tmp_path) == ["default"]

    def test_skips_hidden_dirs_and_stops_at_scan_depth(self, tmp_path):
        _make_skill(tmp_path / "skills" / ".hidden" / "secret")
        _make_skill(tmp_path / "skills" / "a" / "b" / "c" / "d" / "e" / "deepest")
        _make_skill(tmp_path / "skills" / "a" / "b" / "c" / "d" / "e" / "f" / "too-deep")

        assert _codex_skill_names(tmp_path) == ["deepest"]

    def test_hidden_declared_root_is_searched(self, tmp_path):
        _make_skill(tmp_path / ".claude" / "skills" / "declared")
        _write_json(tmp_path / ".codex-plugin" / "plugin.json", {"skills": "./.claude/skills"})

        assert _codex_skill_names(tmp_path) == ["declared"]

    def test_adds_migrated_command_skills(self, tmp_path):
        _make_skill(tmp_path / "skills" / "default")
        _make_skill(tmp_path / ".codex-plugin" / "migrated-command-skills" / "command")

        assert _codex_skill_names(tmp_path) == ["command", "default"]

    def test_whole_repo_plugin_without_skills_dir_has_no_skills(self, tmp_path):
        _make_skill(tmp_path / "plugins" / "member" / "skills" / "shared")
        _write_json(tmp_path / ".codex-plugin" / "plugin.json", {"name": "umbrella"})

        assert _codex_skill_names(tmp_path) == []


# -- install_claude_plugins ---------------------------------------------------


class TestInstallClaudePlugins:
    def test_manifest_lists_the_skills_claude_loads(self, tmp_path):
        seed = tmp_path / "seed"
        _write_json(
            seed / "marketplaces" / "test-mkt" / ".claude-plugin" / "marketplace.json",
            {
                "name": "test-mkt",
                "plugins": [
                    {"name": "member"},
                    {"name": "umbrella", "strict": False, "skills": ["./plugins/umbrella/skills"]},
                    {"name": "stale", "strict": False, "skills": ["./helpers/skills"]},
                ],
            },
        )
        cache = seed / "cache" / "test-mkt"
        _make_skill(cache / "member" / "0.1.0" / "skills" / "shared")
        umbrella = cache / "umbrella" / "0.1.0"
        _make_skill(umbrella / "plugins" / "member" / "skills" / "shared")
        _make_skill(umbrella / "plugins" / "member" / "skills" / "member-only")
        links = umbrella / "plugins" / "umbrella" / "skills"
        links.mkdir(parents=True)
        (links / "shared").symlink_to("../../member/skills/shared", target_is_directory=True)
        _make_skill(cache / "stale" / "0.1.0" / "plugins" / "member" / "skills" / "shared")
        manifest = tmp_path / "manifest.json"

        with mock.patch(
            "agentic_ci.plugins.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            install_claude_plugins(seed, manifest_path=manifest)

        assert run.call_args_list == [
            mock.call(["claude", "plugin", "install", f"{name}@test-mkt"], capture_output=False)
            for name in ("member", "umbrella", "stale")
        ]
        assert json.loads(manifest.read_text()) == {"member": ["shared"], "umbrella": ["shared"]}

    def test_warns_about_plugins_without_skills(self, tmp_path, capsys):
        seed = tmp_path / "seed"
        _write_json(
            seed / "marketplaces" / "test-mkt" / ".claude-plugin" / "marketplace.json",
            {"name": "test-mkt", "plugins": [{"name": "good"}, {"name": "empty"}, {"name": "bad"}]},
        )
        _make_skill(seed / "cache" / "test-mkt" / "good" / "0.1.0" / "skills" / "review")
        (seed / "cache" / "test-mkt" / "empty" / "0.1.0").mkdir(parents=True)

        def fake_install(args, capture_output):
            return subprocess.CompletedProcess(args, 1 if args[-1] == "bad@test-mkt" else 0)

        with mock.patch("agentic_ci.plugins.subprocess.run", side_effect=fake_install):
            install_claude_plugins(seed, manifest_path=tmp_path / "manifest.json")

        assert "WARN: 2 plugin(s) provide no skills: bad, empty" in capsys.readouterr().out


# -- enable_plugins: Claude Code filtering ------------------------------------


class TestEnablePluginsClaude:
    def _write_settings(self, path, enabled_plugins):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"enabledPlugins": enabled_plugins}))

    def _read_enabled(self, path):
        return json.loads(path.read_text()).get("enabledPlugins", {})

    def test_filters_to_single_plugin(self, monkeypatch, tmp_path):
        settings = tmp_path / "settings.json"
        self._write_settings(settings, {"alpha@mkt": True, "beta@mkt": True})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha")
        monkeypatch.setenv("AGENT_TOOL", "claude")
        enable_plugins()
        ep = self._read_enabled(settings)
        assert ep["alpha@mkt"] is True
        assert ep["beta@mkt"] is False

    def test_enables_multiple(self, monkeypatch, tmp_path):
        settings = tmp_path / "settings.json"
        self._write_settings(settings, {"alpha@mkt": True, "beta@mkt": True})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha,beta")
        monkeypatch.setenv("AGENT_TOOL", "claude")
        enable_plugins()
        ep = self._read_enabled(settings)
        assert ep["alpha@mkt"] is True
        assert ep["beta@mkt"] is True

    def test_noop_when_unset(self, monkeypatch, tmp_path):
        settings = tmp_path / "settings.json"
        self._write_settings(settings, {"alpha@mkt": True, "beta@mkt": True})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("AGENT_ENABLED_PLUGINS", raising=False)
        monkeypatch.setenv("AGENT_TOOL", "claude")
        enable_plugins()
        ep = self._read_enabled(settings)
        assert ep["alpha@mkt"] is True
        assert ep["beta@mkt"] is True

    def test_empty_csv_treated_as_unset(self, monkeypatch, tmp_path):
        settings = tmp_path / "settings.json"
        self._write_settings(settings, {"alpha@mkt": True, "beta@mkt": True})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", ",,,")
        monkeypatch.setenv("AGENT_TOOL", "claude")
        enable_plugins()
        ep = self._read_enabled(settings)
        assert ep["alpha@mkt"] is True
        assert ep["beta@mkt"] is True

    def test_missing_agent_tool_exits(self, monkeypatch, tmp_path):
        settings = tmp_path / "settings.json"
        self._write_settings(settings, {"alpha@mkt": True})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha")
        monkeypatch.delenv("AGENT_TOOL", raising=False)
        with pytest.raises(SystemExit):
            enable_plugins()

    def test_missing_settings_returns_ok(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha")
        monkeypatch.setenv("AGENT_TOOL", "claude")
        enable_plugins()

    def test_unknown_plugin_exits(self, monkeypatch, tmp_path):
        settings = tmp_path / "settings.json"
        self._write_settings(settings, {"alpha@mkt": True})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "nonexistent")
        monkeypatch.setenv("AGENT_TOOL", "claude")
        with pytest.raises(SystemExit):
            enable_plugins()

    def test_warns_when_enabled_plugin_has_no_skills(self, monkeypatch, tmp_path, capsys):
        settings = tmp_path / "settings.json"
        self._write_settings(
            settings, {"alpha@mkt": True, "mcp-only@mkt": True, "emptied@mkt": True}
        )
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"alpha": ["review"], "emptied": []}))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(manifest))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha,mcp-only,emptied")
        monkeypatch.setenv("AGENT_TOOL", "claude")
        enable_plugins()
        err = capsys.readouterr().err
        assert "WARNING: enabled plugin(s) provide no skills: emptied, mcp-only" in err
        assert self._read_enabled(settings) == {
            "alpha@mkt": True,
            "mcp-only@mkt": True,
            "emptied@mkt": True,
        }

    def test_no_skills_warning_needs_manifest(self, monkeypatch, tmp_path, capsys):
        settings = tmp_path / "settings.json"
        self._write_settings(settings, {"alpha@mkt": True})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(tmp_path / "missing.json"))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha")
        monkeypatch.setenv("AGENT_TOOL", "claude")
        enable_plugins()
        assert "provide no skills" not in capsys.readouterr().err

    def test_malformed_json_returns_ok(self, monkeypatch, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text("NOT-JSON{{{")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha")
        monkeypatch.setenv("AGENT_TOOL", "claude")
        enable_plugins()

    def test_mixed_known_unknown_exits_with_matched_info(self, monkeypatch, tmp_path, capsys):
        settings = tmp_path / "settings.json"
        self._write_settings(settings, {"alpha@mkt": True, "beta@mkt": True})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "alpha,nonexistent")
        monkeypatch.setenv("AGENT_TOOL", "claude")
        with pytest.raises(SystemExit):
            enable_plugins()
        captured = capsys.readouterr()
        assert "Matched: alpha" in captured.err


# -- enable_plugins: OpenCode filtering ---------------------------------------


class TestEnablePluginsOpenCode:
    def _setup_skills_on_disk(self, tmp_path, manifest_data):
        """Create skill directories matching the manifest."""
        skills_dir = tmp_path / "skills"
        for skills in manifest_data.values():
            for name in skills:
                sd = skills_dir / name
                sd.mkdir(parents=True, exist_ok=True)
                (sd / "SKILL.md").write_text(f"---\nname: {name}\n---\n")

    def test_removes_unwanted_skill_dirs(self, monkeypatch, tmp_path):
        manifest_data = {"plugin-a": ["skill-a1", "skill-a2"], "plugin-b": ["skill-b1"]}
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(manifest_data))
        config_path = tmp_path / "opencode.json"
        config_path.write_text(json.dumps({"permission": {"*": "allow"}}))
        self._setup_skills_on_disk(tmp_path, manifest_data)
        monkeypatch.setenv("AGENT_TOOL", "opencode")
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "plugin-a")
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(manifest))
        enable_plugins()
        assert not (tmp_path / "skills" / "skill-b1").exists()
        assert (tmp_path / "skills" / "skill-a1" / "SKILL.md").is_file()
        assert (tmp_path / "skills" / "skill-a2" / "SKILL.md").is_file()

    def test_removes_orphan_skill_dirs(self, monkeypatch, tmp_path):
        """Skill dirs not tracked by any manifest entry are also removed."""
        manifest_data = {"plugin-a": ["skill-a1"]}
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(manifest_data))
        config_path = tmp_path / "opencode.json"
        config_path.write_text(json.dumps({}))
        self._setup_skills_on_disk(tmp_path, manifest_data)
        orphan = tmp_path / "skills" / "orphan-skill"
        orphan.mkdir(parents=True)
        (orphan / "SKILL.md").write_text("---\nname: orphan-skill\n---\n")
        monkeypatch.setenv("AGENT_TOOL", "opencode")
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "plugin-a")
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(manifest))
        enable_plugins()
        assert not orphan.exists()
        assert (tmp_path / "skills" / "skill-a1" / "SKILL.md").is_file()

    def test_missing_manifest_returns_ok(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("AGENT_TOOL", "opencode")
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "plugin-a")
        manifest = tmp_path / "nonexistent.json"
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(manifest))
        enable_plugins()
        assert f"{manifest} not found" in capsys.readouterr().err

    def test_invalid_manifest_returns_ok(self, monkeypatch, tmp_path, capsys):
        """An unreadable (invalid JSON) manifest warns and skips filtering."""
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{ not valid json")
        skills_dir = tmp_path / "skills" / "skill-a1"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("---\nname: skill-a1\n---\n")
        monkeypatch.setenv("AGENT_TOOL", "opencode")
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "plugin-a")
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(manifest))
        enable_plugins()
        # Filtering is skipped, so the on-disk skill is left untouched.
        assert (tmp_path / "skills" / "skill-a1" / "SKILL.md").is_file()
        assert "invalid JSON" in capsys.readouterr().err


# -- enable_plugins: Codex filtering -----------------------------------------


class TestEnablePluginsCodex:
    def test_filters_native_plugins_and_compatibility_skills(self, monkeypatch, tmp_path):
        codex_home = tmp_path / "codex"
        skills_dir = codex_home / "skills"
        for name in ("skill-a", "skill-b", "personal-skill"):
            skill_dir = skills_dir / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n")

        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"plugin-a": ["skill-a"], "plugin-b": ["skill-b"]}))
        installed = {
            "installed": [
                {
                    "name": "plugin-a",
                    "pluginId": "plugin-a@test",
                    "marketplaceName": "test",
                },
                {
                    "name": "plugin-b",
                    "pluginId": "plugin-b@test",
                    "marketplaceName": "test",
                },
            ]
        }

        monkeypatch.setenv("AGENT_TOOL", "codex")
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "plugin-a")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(manifest))

        with mock.patch(
            "agentic_ci.plugins._run_codex_json",
            side_effect=[installed, {"pluginId": "plugin-b@test"}],
        ) as run_codex:
            enable_plugins()

        assert run_codex.call_args_list[-1] == mock.call(["plugin", "remove", "plugin-b@test"])
        assert (skills_dir / "skill-a").is_dir()
        assert not (skills_dir / "skill-b").exists()
        assert (skills_dir / "personal-skill").is_dir()

    def test_warns_when_enabled_native_plugin_has_no_skills(self, monkeypatch, tmp_path, capsys):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"plugin-a": ["skill-a"]}))
        installed = {
            "installed": [
                {"name": "plugin-a", "pluginId": "plugin-a@test"},
                {"name": "umbrella", "pluginId": "umbrella@test"},
            ]
        }
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(manifest))

        with mock.patch("agentic_ci.plugins._run_codex_json", return_value=installed):
            _filter_codex({"plugin-a", "umbrella"})

        assert "WARNING: enabled plugin(s) provide no skills: umbrella" in capsys.readouterr().err

    def test_unknown_plugin_exits(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_TOOL", "codex")
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "missing")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(tmp_path / "missing-manifest.json"))

        with (
            mock.patch(
                "agentic_ci.plugins._run_codex_json",
                return_value={"installed": []},
            ),
            pytest.raises(SystemExit),
        ):
            enable_plugins()

    def test_plugin_list_failure_still_filters_compatibility_skills(self, monkeypatch, tmp_path):
        codex_home = tmp_path / "codex"
        skills_dir = codex_home / "skills"
        for name in ("skill-a", "skill-b", "personal-skill"):
            skill_dir = skills_dir / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").touch()

        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"plugin-a": ["skill-a"], "plugin-b": ["skill-b"]}))
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(manifest))

        with mock.patch("agentic_ci.plugins._run_codex_json", return_value=None) as run_codex:
            _filter_codex({"plugin-a"})

        run_codex.assert_called_once_with(["plugin", "list"])
        assert (skills_dir / "skill-a").is_dir()
        assert not (skills_dir / "skill-b").exists()
        assert (skills_dir / "personal-skill").is_dir()

    def test_failed_plugin_removal_exits_nonzero(self, monkeypatch, tmp_path, capsys):
        codex_home = tmp_path / "codex"
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"plugin-a": []}))
        installed = {
            "installed": [
                {
                    "name": "plugin-b",
                    "pluginId": "plugin-b@test",
                }
            ]
        }

        monkeypatch.setenv("AGENT_TOOL", "codex")
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "plugin-a")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(manifest))

        with (
            mock.patch(
                "agentic_ci.plugins._run_codex_json",
                side_effect=[installed, None],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            enable_plugins()

        assert exc_info.value.code == 1
        assert "could not enforce AGENT_ENABLED_PLUGINS" in capsys.readouterr().err

    def test_invalid_manifest_values_are_ignored(self, monkeypatch, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "plugin-a": ["skill-a", 123, None],
                    "plugin-b": "skill-b",
                    "plugin-c": None,
                    7: ["skill-c"],
                }
            )
        )
        monkeypatch.setenv("AGENT_TOOL", "codex")
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "plugin-a")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(manifest))

        with mock.patch("agentic_ci.plugins._run_codex_json", return_value={"installed": []}):
            enable_plugins()

    def test_rejects_option_like_plugin_selector(self, monkeypatch, tmp_path, capsys):
        marketplace = tmp_path / "marketplace.json"
        marketplace.write_text("{}")
        manifest = tmp_path / "manifest.json"

        responses = [
            {"marketplaceName": "trusted"},
            {
                "available": [
                    {"name": "--config=bad", "pluginId": "--config=bad"},
                ]
            },
            {"installed": []},
        ]

        with mock.patch("agentic_ci.plugins._run_codex_json", side_effect=responses) as run_codex:
            install_codex_plugins(marketplace, manifest_path=manifest)

        assert run_codex.call_args_list == [
            mock.call(["plugin", "marketplace", "add", str(tmp_path)]),
            mock.call(["plugin", "list", "--available", "--marketplace", "trusted"]),
            mock.call(["plugin", "list"]),
        ]
        assert "starts with '-'" in capsys.readouterr().err


# -- install_opencode_skills -------------------------------------------------


class TestInstallOpencodeSkills:
    def _make_mock_repo(self, tmp_path):
        repo = tmp_path / "mock-repo"
        skills = repo / "skills" / "greet"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("---\nname: greet\n---\nHello\n")
        return repo

    def _make_marketplace(self, tmp_path):
        mkt = tmp_path / "marketplace.json"
        mkt.write_text(
            json.dumps(
                {
                    "name": "test-mkt",
                    "plugins": [
                        {
                            "name": "mock-greet",
                            "version": "1.0.0",
                            "source": {"repo": "fake/mock", "ref": "main"},
                        }
                    ],
                }
            )
        )
        return mkt

    def test_installs_skills_and_writes_manifest(self, tmp_path):
        mock_repo = self._make_mock_repo(tmp_path)
        mkt = self._make_marketplace(tmp_path)
        skills_dir = tmp_path / "skills"
        manifest = tmp_path / "manifest.json"

        def fake_clone(url, dest, branch=None, depth=None):
            shutil.copytree(mock_repo, dest)
            return True

        with mock.patch("agentic_ci.plugins.clone_repo", side_effect=fake_clone):
            install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert (skills_dir / "greet" / "SKILL.md").is_file()
        data = json.loads(manifest.read_text())
        assert "mock-greet" in data
        assert "greet" in data["mock-greet"]

    def test_clone_failure_skips_plugin(self, tmp_path, capsys):
        mkt = self._make_marketplace(tmp_path)
        skills_dir = tmp_path / "skills"
        manifest = tmp_path / "manifest.json"

        with mock.patch("agentic_ci.plugins.clone_repo", return_value=False):
            install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert json.loads(manifest.read_text()) == {}
        assert "WARN: 1 plugin(s) provide no skills: mock-greet" in capsys.readouterr().out

    def test_installs_skills_from_git_subdir_source(self, tmp_path):
        repo = tmp_path / "mock-repo"
        skill = repo / "plugins" / "patternfly" / "pf-react" / "skills" / "pf-react"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: pf-react\n---\n")
        mkt = tmp_path / "marketplace.json"
        mkt.write_text(
            json.dumps(
                {
                    "plugins": [
                        {
                            "name": "pf-react",
                            "source": {
                                "source": "git-subdir",
                                "url": "https://github.com/rh-uxd/ai-helpers.git",
                                "path": "plugins/patternfly/pf-react",
                                "ref": "main",
                            },
                        }
                    ]
                }
            )
        )
        skills_dir = tmp_path / "skills"
        manifest = tmp_path / "manifest.json"

        def fake_clone(url, dest, branch=None, depth=None):
            assert url == "https://github.com/rh-uxd/ai-helpers.git"
            shutil.copytree(repo, dest)
            return True

        with mock.patch("agentic_ci.plugins.clone_repo", side_effect=fake_clone):
            install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert (skills_dir / "pf-react" / "SKILL.md").is_file()
        assert json.loads(manifest.read_text()) == {"pf-react": ["pf-react"]}

    def test_explicit_skills_paths(self, tmp_path):
        repo = tmp_path / "mock-repo"
        helpers = repo / "helpers" / "skills" / "helper-skill"
        helpers.mkdir(parents=True)
        (helpers / "SKILL.md").write_text("---\nname: helper-skill\n---\n")

        mkt = tmp_path / "marketplace.json"
        mkt.write_text(
            json.dumps(
                {
                    "name": "test-mkt",
                    "plugins": [
                        {
                            "name": "helpers",
                            "source": {"repo": "fake/helpers", "ref": "main"},
                            "skills": ["./helpers/skills"],
                        }
                    ],
                }
            )
        )

        skills_dir = tmp_path / "skills"
        manifest = tmp_path / "manifest.json"

        def fake_clone(url, dest, branch=None, depth=None):
            shutil.copytree(repo, dest)
            return True

        with mock.patch("agentic_ci.plugins.clone_repo", side_effect=fake_clone):
            install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert (skills_dir / "helper-skill" / "SKILL.md").is_file()
        data = json.loads(manifest.read_text())
        assert "helper-skill" in data["helpers"]

    def test_rejects_skills_path_outside_clone(self, tmp_path):
        repo = tmp_path / "mock-repo"
        repo.mkdir()
        mkt = tmp_path / "marketplace.json"
        mkt.write_text(
            json.dumps(
                {
                    "name": "test-mkt",
                    "plugins": [
                        {
                            "name": "escaping",
                            "source": {"repo": "fake/escaping", "ref": "main"},
                            "skills": ["../../etc"],
                        }
                    ],
                }
            )
        )

        skills_dir = tmp_path / "skills"
        manifest = tmp_path / "manifest.json"

        def fake_clone(url, dest, branch=None, depth=None):
            shutil.copytree(repo, dest)
            return True

        with (
            mock.patch("agentic_ci.plugins.clone_repo", side_effect=fake_clone),
            mock.patch("agentic_ci.plugins._copy_tree") as copy_tree,
        ):
            install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        copy_tree.assert_not_called()
        assert json.loads(manifest.read_text()) == {}

    def test_fallback_collects_all_matching_dirs(self, tmp_path):
        """Skills in both .claude/skills/ and skills/ are installed."""
        repo = tmp_path / "mock-repo"
        (repo / ".claude" / "skills" / "debug-skill").mkdir(parents=True)
        (repo / ".claude" / "skills" / "debug-skill" / "SKILL.md").write_text(
            "---\nname: debug-skill\n---\n"
        )
        (repo / "skills" / "main-skill").mkdir(parents=True)
        (repo / "skills" / "main-skill" / "SKILL.md").write_text("---\nname: main-skill\n---\n")

        mkt = self._make_marketplace(tmp_path)
        skills_dir = tmp_path / "skills"
        manifest = tmp_path / "manifest.json"

        def fake_clone(url, dest, branch=None, depth=None):
            shutil.copytree(repo, dest)
            return True

        with mock.patch("agentic_ci.plugins.clone_repo", side_effect=fake_clone):
            install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert (skills_dir / "debug-skill" / "SKILL.md").is_file()
        assert (skills_dir / "main-skill" / "SKILL.md").is_file()
        data = json.loads(manifest.read_text())
        assert sorted(data["mock-greet"]) == ["debug-skill", "main-skill"]

    def test_skips_nested_symlinks_when_copying(self, tmp_path):
        repo = tmp_path / "mock-repo"
        skill = repo / "skills" / "safe-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: safe-skill\n---\n")
        outside = tmp_path / "outside" / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("must not be copied")
        (skill / "linked-secret").symlink_to(outside)

        mkt = self._make_marketplace(tmp_path)
        skills_dir = tmp_path / "installed-skills"
        manifest = tmp_path / "manifest.json"

        def fake_clone(url, dest, branch=None, depth=None):
            shutil.copytree(repo, dest, symlinks=True)
            return True

        with mock.patch("agentic_ci.plugins.clone_repo", side_effect=fake_clone):
            install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert (skills_dir / "safe-skill" / "SKILL.md").is_file()
        assert not (skills_dir / "safe-skill" / "linked-secret").exists()
        assert "safe-skill" in json.loads(manifest.read_text())["mock-greet"]

    def test_skips_plugin_with_colliding_skill_name(self, tmp_path, capsys):
        first_repo = tmp_path / "first-repo"
        first_skill = first_repo / "skills" / "shared"
        first_skill.mkdir(parents=True)
        (first_skill / "SKILL.md").write_text("first\n")
        second_repo = tmp_path / "second-repo"
        second_skill = second_repo / "skills" / "shared"
        second_skill.mkdir(parents=True)
        (second_skill / "SKILL.md").write_text("second\n")

        mkt = tmp_path / "marketplace.json"
        mkt.write_text(
            json.dumps(
                {
                    "name": "test-mkt",
                    "plugins": [
                        {"name": "first", "source": {"repo": "fake/first", "ref": "main"}},
                        {
                            "name": "second",
                            "source": {"repo": "fake/second", "ref": "main"},
                        },
                    ],
                }
            )
        )
        skills_dir = tmp_path / "installed-skills"
        manifest = tmp_path / "manifest.json"

        def fake_clone(url, dest, branch=None, depth=None):
            source = first_repo if "fake/first" in url else second_repo
            shutil.copytree(source, dest)
            return True

        with mock.patch("agentic_ci.plugins.clone_repo", side_effect=fake_clone):
            install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert (skills_dir / "shared" / "SKILL.md").read_text() == "first\n"
        assert json.loads(manifest.read_text()) == {"first": ["shared"]}
        out = capsys.readouterr().out
        assert "collision(s): shared (already installed by first)" in out
        assert "WARN: 1 plugin(s) provide no skills: second" in out

    def test_skips_unowned_files_outside_complete_skill_dirs(self, tmp_path):
        first_repo = tmp_path / "first-repo"
        first_skill = first_repo / "skills" / "shared"
        first_skill.mkdir(parents=True)
        (first_skill / "SKILL.md").write_text("first\n")

        second_repo = tmp_path / "second-repo"
        second_beta = second_repo / "skills" / "beta"
        second_beta.mkdir(parents=True)
        (second_beta / "SKILL.md").write_text("beta\n")
        second_shared = second_repo / "skills" / "shared"
        second_shared.mkdir()
        (second_shared / "SECOND_MARKER.txt").write_text("must not be copied")

        mkt = tmp_path / "marketplace.json"
        mkt.write_text(
            json.dumps(
                {
                    "name": "test-mkt",
                    "plugins": [
                        {"name": "first", "source": {"repo": "fake/first", "ref": "main"}},
                        {
                            "name": "second",
                            "source": {"repo": "fake/second", "ref": "main"},
                        },
                    ],
                }
            )
        )
        skills_dir = tmp_path / "installed-skills"
        manifest = tmp_path / "manifest.json"

        def fake_clone(url, dest, branch=None, depth=None):
            source = first_repo if "fake/first" in url else second_repo
            shutil.copytree(source, dest)
            return True

        with mock.patch("agentic_ci.plugins.clone_repo", side_effect=fake_clone):
            install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert (skills_dir / "shared" / "SKILL.md").read_text() == "first\n"
        assert not (skills_dir / "shared" / "SECOND_MARKER.txt").exists()
        assert (skills_dir / "beta" / "SKILL.md").read_text() == "beta\n"
        assert json.loads(manifest.read_text()) == {
            "first": ["shared"],
            "second": ["beta"],
        }

    def test_empty_marketplace(self, tmp_path):
        mkt = tmp_path / "marketplace.json"
        mkt.write_text(json.dumps({"name": "empty", "plugins": []}))
        skills_dir = tmp_path / "skills"
        manifest = tmp_path / "manifest.json"

        install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert json.loads(manifest.read_text()) == {}


# -- install_codex_plugins ---------------------------------------------------


class TestInstallCodexPlugins:
    def test_marketplace_root_plain_directory(self, tmp_path):
        marketplace = tmp_path / "registry" / "marketplace.json"
        marketplace.parent.mkdir()
        marketplace.touch()

        assert _codex_marketplace_root(marketplace) == marketplace.parent

    def test_installs_native_plugins_and_writes_manifest(self, tmp_path):
        marketplace_dir = tmp_path / ".agents" / "plugins"
        marketplace_dir.mkdir(parents=True)
        marketplace = marketplace_dir / "marketplace.json"
        marketplace.write_text("{}")

        installed_plugin = tmp_path / "installed-plugin"
        skill = installed_plugin / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: review\n---\n")
        manifest = tmp_path / "manifest.json"

        responses = [
            {"marketplaceName": "test-marketplace"},
            {
                "available": [
                    {
                        "name": "review-plugin",
                        "pluginId": "review-plugin@test-marketplace",
                    }
                ]
            },
            {"pluginId": "review-plugin@test-marketplace"},
            {
                "installed": [
                    {
                        "name": "review-plugin",
                        "marketplaceName": "test-marketplace",
                        "installedPath": str(installed_plugin),
                    }
                ]
            },
        ]

        with mock.patch("agentic_ci.plugins._run_codex_json", side_effect=responses) as run_codex:
            install_codex_plugins(marketplace, manifest_path=manifest)

        assert run_codex.call_args_list[0] == mock.call(
            ["plugin", "marketplace", "add", str(tmp_path)]
        )
        assert json.loads(manifest.read_text()) == {"review-plugin": ["review"]}

    def test_manifest_uses_installed_path_from_plugin_add(self, tmp_path, monkeypatch):
        """``codex plugin list`` omits installedPath; the add result supplies it."""
        marketplace_dir = tmp_path / ".claude-plugin"
        marketplace_dir.mkdir()
        marketplace = marketplace_dir / "marketplace.json"
        marketplace.write_text("{}")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

        installed_plugin = tmp_path / "plugins" / "cache" / "mkt" / "review-plugin" / "0.1.0"
        skill = installed_plugin / "skills" / "gitlab-code-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: gitlab-code-review\n---\n")
        manifest = tmp_path / "manifest.json"

        responses = [
            {"marketplaceName": "mkt"},
            {"available": [{"name": "review-plugin", "pluginId": "review-plugin@mkt"}]},
            {"pluginId": "review-plugin@mkt", "installedPath": str(installed_plugin)},
            {
                "installed": [
                    {
                        "name": "review-plugin",
                        "pluginId": "review-plugin@mkt",
                        "marketplaceName": "mkt",
                        "version": "0.1.0",
                    }
                ]
            },
        ]

        with mock.patch("agentic_ci.plugins._run_codex_json", side_effect=responses):
            install_codex_plugins(marketplace, manifest_path=manifest)

        assert json.loads(manifest.read_text()) == {"review-plugin": ["gitlab-code-review"]}

    def test_manifest_falls_back_to_codex_cache_layout(self, tmp_path, monkeypatch):
        """Without any reported path, derive it from the Codex plugin cache."""
        marketplace_dir = tmp_path / ".claude-plugin"
        marketplace_dir.mkdir()
        marketplace = marketplace_dir / "marketplace.json"
        marketplace.write_text("{}")
        codex_home = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        cached = codex_home / "plugins" / "cache" / "mkt" / "review-plugin" / "0.1.0"
        skill = cached / "skills" / "gitlab-code-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: gitlab-code-review\n---\n")
        manifest = tmp_path / "manifest.json"

        responses = [
            {"marketplaceName": "mkt"},
            {"available": [{"name": "review-plugin", "pluginId": "review-plugin@mkt"}]},
            {"pluginId": "review-plugin@mkt"},
            {
                "installed": [
                    {
                        "name": "review-plugin",
                        "pluginId": "review-plugin@mkt",
                        "marketplaceName": "mkt",
                        "version": "0.1.0",
                    }
                ]
            },
        ]

        with mock.patch("agentic_ci.plugins._run_codex_json", side_effect=responses):
            install_codex_plugins(marketplace, manifest_path=manifest)

        assert json.loads(manifest.read_text()) == {"review-plugin": ["gitlab-code-review"]}

    def test_manifest_lists_the_skills_codex_loads(self, tmp_path):
        marketplace = tmp_path / "marketplace.json"
        marketplace.write_text("{}")
        good = tmp_path / "installed" / "good"
        _make_skill(good / "skills" / "review")
        umbrella = tmp_path / "installed" / "umbrella"
        _make_skill(umbrella / "plugins" / "member" / "skills" / "shared")
        _write_json(umbrella / ".codex-plugin" / "plugin.json", {"name": "umbrella"})
        manifest = tmp_path / "manifest.json"

        responses = [
            {"marketplaceName": "mkt"},
            {"available": [{"name": "good"}, {"name": "umbrella"}]},
            {"installedPath": str(good)},
            {"installedPath": str(umbrella)},
            {
                "installed": [
                    {"name": "good", "marketplaceName": "mkt"},
                    {"name": "umbrella", "marketplaceName": "mkt"},
                ]
            },
        ]

        with mock.patch("agentic_ci.plugins._run_codex_json", side_effect=responses):
            install_codex_plugins(marketplace, manifest_path=manifest)

        assert json.loads(manifest.read_text()) == {"good": ["review"]}

    def test_legacy_marketplace_falls_back_to_skills(self, tmp_path):
        marketplace_dir = tmp_path / ".claude-plugin"
        marketplace_dir.mkdir()
        marketplace = marketplace_dir / "marketplace.json"
        marketplace.write_text("{}")
        manifest = tmp_path / "manifest.json"
        skills_dir = tmp_path / "skills"

        with (
            mock.patch(
                "agentic_ci.plugins._run_codex_json",
                side_effect=[
                    {"marketplaceName": "legacy"},
                    {"available": []},
                ],
            ),
            mock.patch("agentic_ci.plugins.install_opencode_skills") as install_skills,
        ):
            install_codex_plugins(
                marketplace,
                skills_dir=skills_dir,
                manifest_path=manifest,
            )

        install_skills.assert_called_once_with(
            marketplace,
            skills_dir=skills_dir,
            manifest_path=manifest,
        )

    def test_marketplace_add_without_name_falls_back_and_warns(self, tmp_path, capsys):
        """A marketplace add that returns no marketplaceName warns and falls back."""
        marketplace = tmp_path / "marketplace.json"
        marketplace.write_text("{}")
        manifest = tmp_path / "manifest.json"
        skills_dir = tmp_path / "skills"

        with (
            mock.patch(
                "agentic_ci.plugins._run_codex_json",
                side_effect=[{"ok": True}],
            ),
            mock.patch("agentic_ci.plugins.install_opencode_skills") as install_skills,
        ):
            install_codex_plugins(
                marketplace,
                skills_dir=skills_dir,
                manifest_path=manifest,
            )

        assert "returned no marketplaceName" in capsys.readouterr().out
        install_skills.assert_called_once_with(
            marketplace,
            skills_dir=skills_dir,
            manifest_path=manifest,
        )

    def test_final_plugin_list_failure_writes_empty_manifest(self, tmp_path):
        marketplace = tmp_path / "marketplace.json"
        marketplace.write_text("{}")
        manifest = tmp_path / "manifest.json"
        responses = [
            {"marketplaceName": "test-marketplace"},
            {
                "available": [
                    {
                        "name": "review-plugin",
                        "pluginId": "review-plugin@test-marketplace",
                    }
                ]
            },
            {"pluginId": "review-plugin@test-marketplace"},
            None,
        ]

        with mock.patch("agentic_ci.plugins._run_codex_json", side_effect=responses):
            install_codex_plugins(marketplace, manifest_path=manifest)

        assert json.loads(manifest.read_text()) == {}

    def test_plugin_add_failure_warns(self, tmp_path, capsys):
        marketplace = tmp_path / "marketplace.json"
        marketplace.write_text("{}")
        manifest = tmp_path / "manifest.json"
        responses = [
            {"marketplaceName": "test-marketplace"},
            {
                "available": [
                    {
                        "name": "review-plugin",
                        "pluginId": "review-plugin@test-marketplace",
                    }
                ]
            },
            None,
            {"installed": []},
        ]

        with mock.patch("agentic_ci.plugins._run_codex_json", side_effect=responses):
            install_codex_plugins(marketplace, manifest_path=manifest)

        out = capsys.readouterr().out
        assert "WARN: failed to install review-plugin@test-marketplace" in out
        assert "WARN: 1 plugin(s) provide no skills: review-plugin" in out


@pytest.mark.parametrize("error", [FileNotFoundError(), OSError("exec failed")])
def test_run_codex_json_handles_missing_or_unexecutable_binary(error):
    with mock.patch("agentic_ci.plugins.subprocess.run", side_effect=error):
        assert _run_codex_json(["plugin", "list"]) is None


def test_run_codex_json_times_out_and_warns(capsys):
    error = subprocess.TimeoutExpired(["codex", "plugin", "list"], 120)
    with mock.patch("agentic_ci.plugins.subprocess.run", side_effect=error) as run:
        assert _run_codex_json(["plugin", "list"]) is None

    assert run.call_args.kwargs["timeout"] == 120
    assert "WARN: failed to run codex plugin list" in capsys.readouterr().out


def test_run_codex_json_reports_stderr(capsys):
    result = subprocess.CompletedProcess(
        ["codex", "plugin", "list"],
        returncode=2,
        stdout="",
        stderr="plugin registry unavailable",
    )
    with mock.patch("agentic_ci.plugins.subprocess.run", return_value=result):
        assert _run_codex_json(["plugin", "list"]) is None

    output = capsys.readouterr().out
    assert "exit 2" in output
    assert "plugin registry unavailable" in output


# -- find_skill_dir -----------------------------------------------------------


def _codex_list_entry(name, version, marketplace="opendatahub-skills", **extra):
    """One ``installed`` entry as ``codex plugin list --json`` (Codex 0.153.4) prints it."""
    entry = {
        "pluginId": f"{name}@{marketplace}",
        "name": name,
        "marketplaceName": marketplace,
        "version": version,
        "installed": True,
        "enabled": True,
        "source": {
            "source": "git",
            "url": f"https://github.com/opendatahub-io/{name}.git",
            "ref": "main",
        },
        "marketplaceSource": {
            "sourceType": "local",
            "source": "/sandbox/.codex/marketplaces/skills-registry",
        },
        "installPolicy": "AVAILABLE",
        "authPolicy": "ON_INSTALL",
    }
    entry.update(extra)
    return entry


def _codex_cache(codex_home, name, version, marketplace="opendatahub-skills"):
    return codex_home / "plugins" / "cache" / marketplace / name / version


class TestFindSkillDirCodex:
    """The sandbox image layout: native plugins in $CODEX_HOME/plugins/cache."""

    @pytest.fixture
    def codex_home(self, tmp_path):
        home = tmp_path / ".codex"
        autofix = _codex_cache(home, "autofix-skills", "0.1.0")
        for skill in ("autofix-repo-resolve", "autofix-resolve", "autofix-triage"):
            _make_skill(autofix / "skills" / skill)
        _make_skill(_codex_cache(home, "assess-rfe", "1.0.0") / "skills" / "export-rubric")
        _make_skill(_codex_cache(home, "assess-strat", "1.0.0") / "skills" / "export-rubric")
        return home

    @pytest.fixture
    def listing(self):
        return {
            "installed": [
                _codex_list_entry("autofix-skills", "0.1.0"),
                _codex_list_entry("assess-rfe", "1.0.0"),
                _codex_list_entry("assess-strat", "1.0.0"),
            ]
        }

    def _find(self, name, codex_home, listing):
        env = {"AGENT_TOOL": "codex", "CODEX_HOME": str(codex_home)}
        with mock.patch("agentic_ci.plugins._run_codex_json", return_value=listing) as run_codex:
            return find_skill_dir(name, env), run_codex

    def test_finds_skill_in_plugin_cache(self, codex_home, listing):
        found, run_codex = self._find("autofix-triage", codex_home, listing)
        assert found == _codex_cache(codex_home, "autofix-skills", "0.1.0") / (
            "skills/autofix-triage"
        )
        run_codex.assert_called_once()
        assert run_codex.call_args.args == (["plugin", "list"],)
        assert run_codex.call_args.kwargs["env"]["CODEX_HOME"] == str(codex_home)

    def test_skill_in_two_plugins_is_ambiguous(self, codex_home, listing):
        found, _ = self._find("export-rubric", codex_home, listing)
        assert found is None

    def test_skill_of_a_removed_plugin_is_not_a_candidate(self, codex_home, listing):
        # enable-plugins removed assess-strat, so only assess-rfe is listed.
        listing["installed"] = [e for e in listing["installed"] if e["name"] != "assess-strat"]
        found, _ = self._find("export-rubric", codex_home, listing)
        assert found == _codex_cache(codex_home, "assess-rfe", "1.0.0") / "skills/export-rubric"

    def test_disabled_plugin_is_skipped(self, codex_home, listing):
        listing["installed"][2]["enabled"] = False
        found, _ = self._find("export-rubric", codex_home, listing)
        assert found == _codex_cache(codex_home, "assess-rfe", "1.0.0") / "skills/export-rubric"

    def test_unknown_skill(self, codex_home, listing):
        found, _ = self._find("no-such-skill", codex_home, listing)
        assert found is None

    def test_follows_declared_skill_paths(self, tmp_path):
        home = tmp_path / ".codex"
        root = _codex_cache(home, "custom", "local")
        _make_skill(root / "custom-skills" / "group" / "deep-skill")
        _write_json(root / ".codex-plugin" / "plugin.json", {"skills": "./custom-skills"})
        found, _ = self._find(
            "deep-skill", home, {"installed": [_codex_list_entry("custom", "local")]}
        )
        assert found == root / "custom-skills" / "group" / "deep-skill"

    def test_prefers_installed_path_from_listing(self, tmp_path):
        root = tmp_path / "elsewhere"
        _make_skill(root / "skills" / "moved")
        entry = _codex_list_entry("moved-plugin", "1.0.0", installedPath=str(root))
        found, _ = self._find("moved", tmp_path / ".codex", {"installed": [entry]})
        assert found == root / "skills" / "moved"

    def test_finds_compatibility_layer_skill(self, tmp_path):
        home = tmp_path / ".codex"
        _make_skill(home / "skills" / "legacy")
        found, _ = self._find("legacy", home, None)
        assert found == home / "skills" / "legacy"

    def test_plugin_and_compatibility_copies_are_ambiguous(self, codex_home, listing):
        _make_skill(codex_home / "skills" / "autofix-triage")
        found, _ = self._find("autofix-triage", codex_home, listing)
        assert found is None

    @pytest.mark.parametrize(
        "entry",
        [
            "not-a-dict",
            {"name": 7, "marketplaceName": "m", "version": "1"},
            {"name": "p", "marketplaceName": "m", "version": "1", "installedPath": 3},
        ],
    )
    def test_malformed_listing_entries_are_skipped(self, tmp_path, entry):
        found, _ = self._find("anything", tmp_path / ".codex", {"installed": [entry]})
        assert found is None

    def test_malformed_listing_is_ignored(self, tmp_path):
        found, _ = self._find("anything", tmp_path / ".codex", {"installed": "oops"})
        assert found is None

    @pytest.mark.parametrize("name", ["", "../autofix-triage", "skills/autofix-triage", "-h", "."])
    def test_rejects_names_that_are_not_one_path_component(self, codex_home, listing, name):
        found, run_codex = self._find(name, codex_home, listing)
        assert found is None
        run_codex.assert_not_called()

    def test_reads_os_environ_by_default(self, codex_home, listing, monkeypatch):
        monkeypatch.setenv("AGENT_TOOL", "codex")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        with mock.patch("agentic_ci.plugins._run_codex_json", return_value=listing):
            found = find_skill_dir("autofix-resolve")
        assert found == _codex_cache(codex_home, "autofix-skills", "0.1.0") / (
            "skills/autofix-resolve"
        )


class TestFindSkillDirOtherTools:
    def test_opencode_skills_dir(self, tmp_path):
        _make_skill(tmp_path / "skills" / "autofix-triage")
        env = {"AGENT_TOOL": "opencode", "OPENCODE_CONFIG_DIR": str(tmp_path)}
        assert find_skill_dir("autofix-triage", env) == tmp_path / "skills" / "autofix-triage"

    def test_opencode_missing_skill(self, tmp_path):
        env = {"AGENT_TOOL": "opencode", "OPENCODE_CONFIG_DIR": str(tmp_path)}
        assert find_skill_dir("autofix-triage", env) is None

    def test_opencode_symlinked_skill_is_ignored(self, tmp_path):
        _make_skill(tmp_path / "real")
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "linked").symlink_to(tmp_path / "real")
        env = {"AGENT_TOOL": "opencode", "OPENCODE_CONFIG_DIR": str(tmp_path)}
        assert find_skill_dir("linked", env) is None

    @pytest.mark.parametrize("agent_tool", ["claude", "", "unknown"])
    def test_other_tools_find_nothing(self, tmp_path, agent_tool):
        _make_skill(tmp_path / "skills" / "autofix-triage")
        env = {"AGENT_TOOL": agent_tool, "OPENCODE_CONFIG_DIR": str(tmp_path)}
        with mock.patch("agentic_ci.plugins._run_codex_json") as run_codex:
            assert find_skill_dir("autofix-triage", env) is None
        run_codex.assert_not_called()


class TestParseSkillDir:
    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("/sandbox/.codex/skills/a\n", "/sandbox/.codex/skills/a"),
            ("/path with space/$HOME/x\n", "/path with space/$HOME/x"),
            ("", None),
            ("\n", None),
            ("relative/path\n", None),
            ("/two\n/lines\n", None),
            ("/tab\there\n", None),
            ("/esc\x1b[31m\n", None),
            ("/" + "a" * 4096 + "\n", None),
        ],
    )
    def test_accepts_one_absolute_printable_path(self, output, expected):
        assert parse_skill_dir(output) == expected


class TestSkillDirCli:
    def _main(self, monkeypatch, *argv):
        monkeypatch.setattr("sys.argv", ["agentic-ci", "skill-dir", *argv])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        return exc.value.code

    def test_prints_the_directory(self, monkeypatch, tmp_path, capsys):
        _make_skill(tmp_path / "skills" / "autofix-triage")
        monkeypatch.setenv("AGENT_TOOL", "opencode")
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        assert self._main(monkeypatch, "autofix-triage") == 0
        out = capsys.readouterr().out
        assert out == f"{tmp_path / 'skills' / 'autofix-triage'}\n"
        assert parse_skill_dir(out) == str(tmp_path / "skills" / "autofix-triage")

    def test_exits_1_with_nothing_on_stdout_when_not_found(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("AGENT_TOOL", "opencode")
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        assert self._main(monkeypatch, "autofix-triage") == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "no unique installed skill" in captured.err

    def test_lookup_warnings_stay_off_stdout(self, monkeypatch, tmp_path, capsys):
        # codex plugin list fails (its warning normally goes to stdout), but
        # the compatibility layer still has the skill.
        _make_skill(tmp_path / "skills" / "legacy")
        monkeypatch.setenv("AGENT_TOOL", "codex")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch("agentic_ci.plugins.subprocess.run", return_value=failed):
            assert self._main(monkeypatch, "legacy") == 0
        captured = capsys.readouterr()
        assert captured.out == f"{tmp_path / 'skills' / 'legacy'}\n"
        assert "WARN: codex plugin list failed" in captured.err

    def test_directory_it_could_not_hand_back_safely_is_not_printed(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("AGENT_TOOL", "opencode")
        with mock.patch("agentic_ci.plugins.find_skill_dir", return_value=Path("/a\nb")):
            assert self._main(monkeypatch, "autofix-triage") == 1
        assert capsys.readouterr().out == ""
