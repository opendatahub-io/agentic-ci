"""Tests for agentic_ci.plugins — enable_plugins and install_opencode_skills."""

import json
import shutil
import subprocess
from unittest import mock

import pytest

from agentic_ci.plugins import (
    _codex_marketplace_root,
    _filter_codex,
    _find_skill_names,
    _run_codex_json,
    enable_plugins,
    install_codex_plugins,
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

    def test_skips_plugin_with_colliding_skill_name(self, tmp_path):
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

        assert "WARN: failed to install review-plugin@test-marketplace" in capsys.readouterr().out


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
