"""Tests for save_job_artifacts in agentic_ci.skill."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agentic_ci.skill import save_job_artifacts


def test_prompt_saved(tmp_path: Path) -> None:
    """save_job_artifacts writes prompt.txt when prompt is provided."""
    save_job_artifacts(tmp_path, prompt="hello world")
    assert (tmp_path / "prompt.txt").read_text() == "hello world"


def test_output_copied(tmp_path: Path) -> None:
    """Claude output is copied from repo_dir to work_dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    work = tmp_path / "work"
    (repo / "claude-output.txt").write_text("some output", encoding="utf-8")

    save_job_artifacts(work, repo_dir=repo)

    assert (work / "claude_output.txt").read_text() == "some output"
    assert not (work / "claude_output_tail.txt").exists()


def test_tail_created_for_long_output(tmp_path: Path) -> None:
    """A tail file is created when output exceeds 500 lines."""
    repo = tmp_path / "repo"
    repo.mkdir()
    work = tmp_path / "work"
    lines = [f"line {i}" for i in range(600)]
    (repo / "claude-output.txt").write_text("\n".join(lines), encoding="utf-8")

    save_job_artifacts(work, repo_dir=repo)

    assert (work / "claude_output.txt").exists()
    tail = (work / "claude_output_tail.txt").read_text()
    tail_lines = tail.splitlines()
    assert len(tail_lines) == 500
    assert tail_lines[-1] == "line 599"
    assert tail_lines[0] == "line 100"


def test_graceful_on_exception(tmp_path: Path) -> None:
    """save_job_artifacts does not raise even on internal errors."""
    with patch("agentic_ci.skill.Path.mkdir", side_effect=OSError("disk full")):
        # Should not raise
        save_job_artifacts(tmp_path / "nonexistent", prompt="test")


def test_no_prompt_no_file(tmp_path: Path) -> None:
    """No prompt.txt is created when prompt is empty."""
    save_job_artifacts(tmp_path)
    assert not (tmp_path / "prompt.txt").exists()


def test_output_in_work_dir(tmp_path: Path) -> None:
    """Claude output in work_dir is handled when repo_dir has none."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "claude-output.txt").write_text("work output", encoding="utf-8")

    save_job_artifacts(work)

    # Output file already in work_dir, resolve() should match, so no copy
    # but no error either
    assert (work / "claude-output.txt").read_text() == "work output"
