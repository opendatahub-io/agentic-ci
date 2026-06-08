"""Tests for git helper functions."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from agentic_ci.git import checkout_branch, get_default_branch, git_output, strip_committed_files


class TestCheckoutBranch:
    def test_success(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            assert checkout_branch(tmp_path, "feature/test") is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["git", "checkout", "feature/test"]
            assert args[1]["cwd"] == str(tmp_path)

    def test_checkout_failure(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git", stderr="error")
            assert checkout_branch(tmp_path, "feature/test") is False

    def test_git_not_found(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            assert checkout_branch(tmp_path, "feature/test") is False

    def test_invalid_ref_rejected(self, tmp_path: Path):
        assert checkout_branch(tmp_path, "--evil") is False
        assert checkout_branch(tmp_path, "") is False
        assert checkout_branch(tmp_path, "foo..bar") is False

    def test_valid_ref_names(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            assert checkout_branch(tmp_path, "main") is True
            assert checkout_branch(tmp_path, "feature/my-branch") is True
            assert checkout_branch(tmp_path, "v1.0.0") is True


class TestGetDefaultBranch:
    def test_returns_default_branch(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="origin/develop\n")
            assert get_default_branch(tmp_path) == "develop"

    def test_returns_main_on_error(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git", stderr="error")
            assert get_default_branch(tmp_path) == "main"

    def test_returns_main_when_origin_head(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="origin/HEAD\n")
            assert get_default_branch(tmp_path) == "main"

    def test_returns_main_on_file_not_found(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            assert get_default_branch(tmp_path) == "main"

    def test_strips_origin_prefix(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="origin/master\n")
            assert get_default_branch(tmp_path) == "master"


class TestGitOutput:
    def test_returns_stdout(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="abc123\n")
            assert git_output(tmp_path, "rev-parse", "HEAD") == "abc123"
            args = mock_run.call_args
            assert args[0][0] == ["git", "rev-parse", "HEAD"]

    def test_returns_none_on_error(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git", stderr="error")
            assert git_output(tmp_path, "log") is None

    def test_returns_none_on_file_not_found(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            assert git_output(tmp_path, "status") is None

    def test_strips_whitespace(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="  some output  \n")
            assert git_output(tmp_path, "diff") == "some output"

    def test_multiple_args(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="output\n")
            git_output(tmp_path, "log", "--oneline", "-5")
            args = mock_run.call_args
            assert args[0][0] == ["git", "log", "--oneline", "-5"]


def _init_repo(path: Path) -> Path:
    """Create a minimal git repo with one commit and an origin/HEAD ref."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(path)], cwd=str(path), capture_output=True
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/HEAD", "HEAD"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )
    return path


class TestStripCommittedFiles:
    def test_removes_matching_files_from_commit(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        (repo / "autofix-output").mkdir()
        (repo / "autofix-output" / "verdict.json").write_text("{}")
        (repo / "fix.py").write_text("print('fix')\n")
        subprocess.run(
            ["git", "add", "-f", "autofix-output/verdict.json", "fix.py"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "fix"], cwd=str(repo), capture_output=True, check=True
        )

        stripped = strip_committed_files(repo, ["autofix-output/*"])

        assert stripped == ["autofix-output/verdict.json"]
        result = subprocess.run(
            ["git", "diff", "--name-only", "origin/HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        changed = [f for f in result.stdout.strip().split("\n") if f]
        assert "fix.py" in changed
        assert "autofix-output/verdict.json" not in changed

    def test_noop_when_no_matches(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        (repo / "fix.py").write_text("print('fix')\n")
        subprocess.run(["git", "add", "fix.py"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "fix"], cwd=str(repo), capture_output=True, check=True
        )

        stripped = strip_committed_files(repo, ["autofix-output/*"])

        assert stripped == []

    def test_preserves_working_tree_copy(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        (repo / "output").mkdir()
        verdict = repo / "output" / "result.json"
        verdict.write_text("{}")
        subprocess.run(
            ["git", "add", "-f", "output/result.json"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "with output"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        strip_committed_files(repo, ["output/*"])

        assert verdict.exists()
