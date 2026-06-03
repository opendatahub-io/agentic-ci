"""Tests for skill metadata parsing."""

import logging
from pathlib import Path

import pytest

from agentic_ci.skill_metadata import (
    collect_artifacts,
    load_skill_metadata,
    parse_frontmatter,
)

_ARTIFACTS_STR = "claude-output.txt autofix-output/ .autofix-context/ tmp/orchestrator-state.yaml"

FULL_SKILL_MD = """\
---
name: autofix-resolve
description: >-
  Use when orchestrating a Jira ticket fix end-to-end. Dispatches to implement
  and review prompt agents in a loop, uses state.py for persistence, and
  evaluates findings to decide iteration. Never writes code directly.
allowed-tools: Read Write Bash Skill
user-invocable: true
metadata:
  x-artifacts: "claude-output.txt autofix-output/ .autofix-context/ tmp/orchestrator-state.yaml"
  author: aipcc-team
---

# Skill: Resolve / Iterate Orchestrator

Orchestrate the fix for a Jira ticket...
"""

SIMPLE_SKILL_MD = """\
---
name: autofix-triage
description: >-
  Use when assessing a Jira bug ticket for AI autofix readiness. Produces a
  structured JSON verdict.
allowed-tools: Read Grep Glob Write
---

# Skill: Triage Bug Readiness
"""


class TestParseFrontmatter:
    def test_basic_key_value(self):
        text = "---\nname: my-skill\n---\n"
        result = parse_frontmatter(text)
        assert result == {"name": "my-skill"}

    def test_multiple_key_values(self):
        text = "---\nname: my-skill\nallowed-tools: Read Write\n---\n"
        result = parse_frontmatter(text)
        assert result == {"name": "my-skill", "allowed-tools": "Read Write"}

    def test_block_list(self):
        text = "---\nitems:\n  - file1.txt\n  - dir/\n  - file2.yaml\n---\n"
        result = parse_frontmatter(text)
        assert result == {"items": ["file1.txt", "dir/", "file2.yaml"]}

    def test_nested_map(self):
        text = "---\nmetadata:\n  author: team-a\n  version: 1.0\n---\n"
        result = parse_frontmatter(text)
        assert result == {"metadata": {"author": "team-a", "version": "1.0"}}

    def test_nested_map_with_x_artifacts(self):
        text = '---\nmetadata:\n  x-artifacts: "output.txt data/"\n  author: test\n---\n'
        result = parse_frontmatter(text)
        assert result == {
            "metadata": {"x-artifacts": "output.txt data/", "author": "test"},
        }

    def test_folded_scalar(self):
        text = (
            "---\n"
            "description: >-\n"
            "  This is a long description that\n"
            "  spans multiple lines.\n"
            "---\n"
        )
        result = parse_frontmatter(text)
        assert result == {"description": "This is a long description that spans multiple lines."}

    def test_no_frontmatter(self):
        text = "# Just a heading\n\nSome body text.\n"
        result = parse_frontmatter(text)
        assert result == {}

    def test_empty_value_returns_empty_list(self):
        text = "---\nitems:\n---\n"
        result = parse_frontmatter(text)
        assert result == {"items": []}

    def test_boolean_string_value(self):
        text = "---\nuser-invocable: true\n---\n"
        result = parse_frontmatter(text)
        assert result == {"user-invocable": "true"}

    def test_false_boolean_string(self):
        text = "---\nuser-invocable: false\n---\n"
        result = parse_frontmatter(text)
        assert result == {"user-invocable": "false"}

    def test_body_after_frontmatter_ignored(self):
        text = "---\nname: test\n---\n\n# Heading\n\nBody content here.\n"
        result = parse_frontmatter(text)
        assert result == {"name": "test"}

    def test_full_skill_md(self):
        result = parse_frontmatter(FULL_SKILL_MD)
        assert result["name"] == "autofix-resolve"
        assert "end-to-end" in result["description"]
        assert "Never writes code directly." in result["description"]
        assert result["allowed-tools"] == "Read Write Bash Skill"
        assert result["user-invocable"] == "true"
        assert result["metadata"] == {
            "x-artifacts": _ARTIFACTS_STR,
            "author": "aipcc-team",
        }

    def test_simple_skill_md(self):
        result = parse_frontmatter(SIMPLE_SKILL_MD)
        assert result["name"] == "autofix-triage"
        assert "structured JSON verdict" in result["description"]
        assert result["allowed-tools"] == "Read Grep Glob Write"
        assert "metadata" not in result

    def test_empty_string(self):
        assert parse_frontmatter("") == {}

    def test_single_delimiter_no_close(self):
        text = "---\nname: test\n"
        assert parse_frontmatter(text) == {}

    def test_folded_scalar_followed_by_key(self):
        text = (
            "---\n"
            "description: >-\n"
            "  Line one of desc\n"
            "  line two of desc.\n"
            "name: after-folded\n"
            "---\n"
        )
        result = parse_frontmatter(text)
        assert result["description"] == "Line one of desc line two of desc."
        assert result["name"] == "after-folded"

    def test_nested_map_followed_by_key(self):
        text = "---\nmetadata:\n  author: team-a\nname: after-metadata\n---\n"
        result = parse_frontmatter(text)
        assert result["metadata"] == {"author": "team-a"}
        assert result["name"] == "after-metadata"

    def test_crlf_line_endings(self):
        text = "---\r\nname: crlf-skill\r\n---\r\n"
        result = parse_frontmatter(text)
        assert result == {"name": "crlf-skill"}


class TestLoadSkillMetadata:
    def test_full_round_trip(self, tmp_path: Path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(FULL_SKILL_MD)
        meta = load_skill_metadata(skill_file)
        assert meta.name == "autofix-resolve"
        assert "end-to-end" in meta.description
        assert meta.allowed_tools == ["Read", "Write", "Bash", "Skill"]
        assert meta.user_invocable is True
        assert meta.metadata == {
            "x-artifacts": _ARTIFACTS_STR,
            "author": "aipcc-team",
        }
        assert meta.artifacts == [
            "claude-output.txt",
            "autofix-output/",
            ".autofix-context/",
            "tmp/orchestrator-state.yaml",
        ]

    def test_missing_metadata_defaults_empty(self, tmp_path: Path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(SIMPLE_SKILL_MD)
        meta = load_skill_metadata(skill_file)
        assert meta.metadata == {}
        assert meta.artifacts == []

    def test_metadata_without_artifacts(self, tmp_path: Path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("---\nname: test\nmetadata:\n  author: me\n---\n")
        meta = load_skill_metadata(skill_file)
        assert meta.metadata == {"author": "me"}
        assert meta.artifacts == []

    def test_missing_optional_fields_get_defaults(self, tmp_path: Path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("---\nname: minimal\n---\n")
        meta = load_skill_metadata(skill_file)
        assert meta.name == "minimal"
        assert meta.description == ""
        assert meta.allowed_tools == []
        assert meta.user_invocable is False
        assert meta.metadata == {}
        assert meta.artifacts == []

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            load_skill_metadata(Path("/nonexistent/SKILL.md"))


class TestCollectArtifacts:
    def _write_skill(self, tmp_path: Path, name: str, artifacts: str) -> Path:
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        content = f"---\nname: {name}\n"
        if artifacts:
            content += f'metadata:\n  x-artifacts: "{artifacts}"\n'
        content += "---\n"
        skill_file.write_text(content)
        return skill_file

    def test_deduplicates_across_skills(self, tmp_path: Path):
        skill_a = self._write_skill(tmp_path, "skill-a", "output.txt shared.log")
        skill_b = self._write_skill(tmp_path, "skill-b", "shared.log report.json")
        result = collect_artifacts(skill_a, skill_b)
        assert result == ["output.txt", "report.json", "shared.log"]

    def test_returns_sorted(self, tmp_path: Path):
        skill_file = self._write_skill(tmp_path, "z-skill", "z.txt a.txt m.txt")
        result = collect_artifacts(skill_file)
        assert result == ["a.txt", "m.txt", "z.txt"]

    def test_skips_missing_files(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        missing = tmp_path / "missing" / "SKILL.md"
        existing = self._write_skill(tmp_path, "ok", "keep.txt")
        with caplog.at_level(logging.WARNING):
            result = collect_artifacts(missing, existing)
        assert result == ["keep.txt"]
        assert "missing" in caplog.text.lower() or str(missing) in caplog.text

    def test_empty_input(self):
        assert collect_artifacts() == []

    def test_skill_with_no_artifacts(self, tmp_path: Path):
        skill_file = self._write_skill(tmp_path, "no-artifacts", "")
        result = collect_artifacts(skill_file)
        assert result == []
