"""Tests for extra_skills config writing in run_skill()."""

import json

import pytest

from agentic_ci.skill import SkillConfig, run_skill


def _dry_run_skill(tmp_path, extra_skills, context_dir=".context"):
    """Helper: run_skill in dry-run mode and return the work_dir."""
    config = SkillConfig(
        skill_name="test-skill",
        extra_skills=extra_skills,
        context_dir=context_dir,
    )
    run_skill(
        config,
        ticket_key="TEST-1",
        work_dir=tmp_path,
        config_dir=tmp_path,
        dry_run=True,
    )
    return tmp_path


class TestExtraSkillsConfigWriting:
    def test_structured_dicts_written(self, tmp_path):
        hooks = [
            {"name": "preflight", "args": "--local --fix", "hooks": ["post_implement"]},
        ]
        _dry_run_skill(tmp_path, hooks)
        config_file = tmp_path / ".context" / "config.json"
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert data["extra_skills"] == hooks

    def test_multiple_skills_written(self, tmp_path):
        skills = [
            {"name": "skill-a"},
            {"name": "skill-b", "args": "--fix", "hooks": ["post_implement"]},
        ]
        _dry_run_skill(tmp_path, skills)
        data = json.loads((tmp_path / ".context" / "config.json").read_text())
        assert data["extra_skills"] == skills

    def test_empty_extra_skills_no_file(self, tmp_path):
        _dry_run_skill(tmp_path, [])
        assert not (tmp_path / ".context" / "config.json").exists()

    def test_custom_context_dir(self, tmp_path):
        _dry_run_skill(tmp_path, [{"name": "s"}], context_dir=".autofix-context")
        assert (tmp_path / ".autofix-context" / "config.json").exists()
        assert not (tmp_path / ".context" / "config.json").exists()

    def test_path_traversal_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="escapes work_dir"):
            _dry_run_skill(tmp_path, [{"name": "s"}], context_dir="../escape")

    def test_absolute_path_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="escapes work_dir"):
            _dry_run_skill(tmp_path, [{"name": "s"}], context_dir="/tmp/evil")

    def test_symlinked_context_dir_rejected(self, tmp_path):
        target = tmp_path / "real-dir"
        target.mkdir()
        link = tmp_path / ".context"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="symlink"):
            _dry_run_skill(tmp_path, [{"name": "s"}])

    def test_symlinked_config_json_rejected(self, tmp_path):
        ctx_dir = tmp_path / ".context"
        ctx_dir.mkdir()
        target = tmp_path / "decoy.json"
        target.write_text("{}")
        (ctx_dir / "config.json").symlink_to(target)
        with pytest.raises(ValueError, match="symlink"):
            _dry_run_skill(tmp_path, [{"name": "s"}])
