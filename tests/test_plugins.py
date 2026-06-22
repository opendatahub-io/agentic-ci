"""Tests for agentic_ci.plugins — enable_plugins and install_opencode_skills."""

import json
import shutil
from unittest import mock

import pytest

from agentic_ci.plugins import (
    _find_skill_names,
    enable_plugins,
    install_opencode_skills,
)

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

    def test_missing_manifest_returns_ok(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_TOOL", "opencode")
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AGENT_ENABLED_PLUGINS", "plugin-a")
        monkeypatch.setenv("PLUGIN_SKILLS_MANIFEST", str(tmp_path / "nonexistent.json"))
        enable_plugins()


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

    def test_clone_failure_skips_plugin(self, tmp_path):
        mkt = self._make_marketplace(tmp_path)
        skills_dir = tmp_path / "skills"
        manifest = tmp_path / "manifest.json"

        with mock.patch("agentic_ci.plugins.clone_repo", return_value=False):
            install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert json.loads(manifest.read_text()) == {}

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

    def test_empty_marketplace(self, tmp_path):
        mkt = tmp_path / "marketplace.json"
        mkt.write_text(json.dumps({"name": "empty", "plugins": []}))
        skills_dir = tmp_path / "skills"
        manifest = tmp_path / "manifest.json"

        install_opencode_skills(mkt, skills_dir=skills_dir, manifest_path=manifest)

        assert json.loads(manifest.read_text()) == {}
