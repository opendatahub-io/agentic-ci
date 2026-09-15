"""Tests for git helper functions."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from agentic_ci.git import (
    _GITHUB_URL_RE,
    _GITLAB_URL_RE,
    _collect_candidates,
    _dedup_gitlab_prefixes,
    _is_transient_push_error,
    _safe_float,
    _safe_int,
    checkout_branch,
    extract_all_repo_urls,
    get_changed_files,
    get_default_branch,
    git_output,
    push_branch,
    rebase_branch,
    strip_committed_files,
)


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


class TestRebaseBranch:
    def test_success(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            assert rebase_branch(tmp_path, "origin/main") is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["git", "rebase", "origin/main"]
            assert args[1]["cwd"] == str(tmp_path)

    def test_failure_aborts(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CalledProcessError(1, "git", stderr="conflict"),
                subprocess.CompletedProcess([], 0),
            ]
            assert rebase_branch(tmp_path, "origin/main") is False
            assert mock_run.call_count == 2
            abort_args = mock_run.call_args_list[1]
            assert abort_args[0][0] == ["git", "rebase", "--abort"]

    def test_invalid_ref_rejected(self, tmp_path: Path):
        assert rebase_branch(tmp_path, "--evil") is False
        assert rebase_branch(tmp_path, "") is False
        assert rebase_branch(tmp_path, "foo..bar") is False

    def test_git_not_found(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            assert rebase_branch(tmp_path, "origin/main") is False

    def test_real_rebase(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        default = _default_branch(repo)
        subprocess.run(
            ["git", "checkout", "-b", "feature"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        (repo / "feature.txt").write_text("feature\n")
        subprocess.run(["git", "add", "feature.txt"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "feature commit"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", default],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        (repo / "main.txt").write_text("main\n")
        subprocess.run(["git", "add", "main.txt"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main commit"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "feature"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        assert rebase_branch(repo, default) is True

        log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "feature commit" in log.stdout
        assert "main commit" in log.stdout

    def test_real_rebase_conflict_aborts(self, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        default = _default_branch(repo)
        subprocess.run(
            ["git", "checkout", "-b", "feature"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        (repo / "README.md").write_text("feature change\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "feature edit"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", default],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        (repo / "README.md").write_text("conflicting change\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main edit"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "feature"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        assert rebase_branch(repo, default) is False

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout.strip() == ""


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


def _default_branch(repo: Path) -> str:
    """Return the current branch name of a repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


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

    def test_stripped_file_excluded_from_get_changed_files(self, tmp_path: Path):
        """After stripping, get_changed_files must not report the file."""
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

        strip_committed_files(repo, ["autofix-output/*"])

        changed = get_changed_files(repo, base_ref="origin/HEAD")
        assert "fix.py" in changed
        assert "autofix-output/verdict.json" not in changed

    def test_logs_git_rm_failure(self, tmp_path: Path, caplog):
        """When git rm --cached fails, log the error with stderr details."""
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

        original_run = subprocess.run

        def _sabotage_rm(cmd, **kw):
            if cmd[:3] == ["git", "rm", "--cached"] and "verdict.json" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: not removing")
            return original_run(cmd, **kw)

        with patch("agentic_ci.git.subprocess.run", side_effect=_sabotage_rm):
            with caplog.at_level("ERROR", logger="agentic_ci.git"):
                stripped = strip_committed_files(repo, ["autofix-output/*"])

        assert stripped == []
        assert any("git rm --cached failed" in r.message for r in caplog.records)
        assert any("fatal: not removing" in r.message for r in caplog.records)

    def test_strip_across_multiple_commits(self, tmp_path: Path):
        """Artifact committed in an earlier commit is stripped by amending HEAD."""
        repo = _init_repo(tmp_path / "repo")
        (repo / "autofix-output").mkdir()
        (repo / "autofix-output" / "verdict.json").write_text('{"v":1}')
        (repo / "fix.py").write_text("print('fix')\n")
        subprocess.run(
            ["git", "add", "-f", "autofix-output/verdict.json", "fix.py"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "commit 1"], cwd=str(repo), capture_output=True, check=True
        )
        (repo / "fix2.py").write_text("print('fix2')\n")
        subprocess.run(["git", "add", "fix2.py"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "commit 2"], cwd=str(repo), capture_output=True, check=True
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
        assert "autofix-output/verdict.json" not in changed
        assert "fix.py" in changed
        assert "fix2.py" in changed
        assert (repo / "autofix-output" / "verdict.json").exists()


class TestCollectCandidates:
    def test_extracts_gitlab_url(self):
        text = "See https://gitlab.com/group/project for details."
        assert _collect_candidates(text, _GITLAB_URL_RE) == ["https://gitlab.com/group/project"]

    def test_extracts_github_url(self):
        text = "See https://github.com/org/repo for details."
        assert _collect_candidates(text, _GITHUB_URL_RE) == ["https://github.com/org/repo"]

    def test_gitlab_blob_url_extracts_repo_root(self):
        text = "Blob https://gitlab.com/group/project/-/blob/main/f.py"
        assert _collect_candidates(text, _GITLAB_URL_RE) == ["https://gitlab.com/group/project"]

    def test_gitlab_tree_url_extracts_repo_root(self):
        text = "Dir https://gitlab.com/group/project/-/tree/main/src"
        assert _collect_candidates(text, _GITLAB_URL_RE) == ["https://gitlab.com/group/project"]

    def test_gitlab_merge_request_url_extracts_repo_root(self):
        text = "MR https://gitlab.com/group/project/-/merge_requests/42"
        assert _collect_candidates(text, _GITLAB_URL_RE) == ["https://gitlab.com/group/project"]

    def test_gitlab_nested_group_blob_url_extracts_repo_root(self):
        text = "See https://gitlab.com/redhat/rhel-ai/core/infrastructure/-/blob/main/CODEOWNERS"
        assert _collect_candidates(text, _GITLAB_URL_RE) == [
            "https://gitlab.com/redhat/rhel-ai/core/infrastructure"
        ]

    def test_gitlab_file_url_deduplicates_with_plain_url(self):
        text = (
            "Repo https://gitlab.com/group/project "
            "and file https://gitlab.com/group/project/-/blob/main/f.py"
        )
        assert _collect_candidates(text, _GITLAB_URL_RE) == ["https://gitlab.com/group/project"]

    def test_github_pr_url_extracts_repo_root(self):
        text = "PR at https://github.com/org/repo/pull/42"
        assert _collect_candidates(text, _GITHUB_URL_RE) == ["https://github.com/org/repo"]

    def test_github_issues_url_extracts_repo_root(self):
        text = "Bug at https://github.com/org/repo/issues/10"
        assert _collect_candidates(text, _GITHUB_URL_RE) == ["https://github.com/org/repo"]

    def test_github_actions_url_extracts_repo_root(self):
        text = "CI run https://github.com/org/repo/actions/runs/123"
        assert _collect_candidates(text, _GITHUB_URL_RE) == ["https://github.com/org/repo"]

    def test_plain_github_repo_url_kept(self):
        text = "Clone https://github.com/org/repo to get started."
        assert _collect_candidates(text, _GITHUB_URL_RE) == ["https://github.com/org/repo"]

    def test_mixed_pr_and_plain_repo_urls(self):
        text = (
            "See PR https://github.com/other-org/other-repo/pull/7 "
            "and clone https://github.com/org/repo for the fix."
        )
        assert _collect_candidates(text, _GITHUB_URL_RE) == [
            "https://github.com/other-org/other-repo",
            "https://github.com/org/repo",
        ]

    def test_file_level_github_urls_extract_repo_root(self):
        text = (
            "See https://github.com/red-hat-data-services/ods-ci/blob/master/test.py "
            "and https://github.com/red-hat-data-services/ods-ci/tree/main/dir"
        )
        assert _collect_candidates(text, _GITHUB_URL_RE) == [
            "https://github.com/red-hat-data-services/ods-ci"
        ]

    def test_filters_file_extensions(self):
        text = "See https://github.com/org/repo.md for docs."
        assert _collect_candidates(text, _GITHUB_URL_RE) == []

    def test_filters_placeholders(self):
        text = "Use https://github.com/your-org/your-repo as a template."
        assert _collect_candidates(text, _GITHUB_URL_RE) == []

    def test_strips_trailing_slash_and_git_suffix(self):
        text = "Clone https://github.com/org/repo.git/ now."
        result = _collect_candidates(text, _GITHUB_URL_RE)
        assert result == ["https://github.com/org/repo"]

    def test_deduplicates(self):
        text = "https://github.com/org/repo and also https://github.com/org/repo again"
        assert _collect_candidates(text, _GITHUB_URL_RE) == ["https://github.com/org/repo"]

    def test_preserves_order(self):
        text = "https://github.com/org/beta then https://github.com/org/alpha"
        result = _collect_candidates(text, _GITHUB_URL_RE)
        assert result == [
            "https://github.com/org/beta",
            "https://github.com/org/alpha",
        ]

    def test_empty_text(self):
        assert _collect_candidates("", _GITHUB_URL_RE) == []
        assert _collect_candidates("", _GITLAB_URL_RE) == []


class TestDedupGitlabPrefixes:
    def test_removes_prefix_url(self):
        urls = [
            "https://gitlab.com/group",
            "https://gitlab.com/group/project",
        ]
        assert _dedup_gitlab_prefixes(urls) == ["https://gitlab.com/group/project"]

    def test_no_dedup_when_not_prefix(self):
        urls = [
            "https://gitlab.com/org-a/repo",
            "https://gitlab.com/org-b/repo",
        ]
        assert _dedup_gitlab_prefixes(urls) == urls

    def test_does_not_collapse_partial_name_match(self):
        urls = [
            "https://gitlab.com/group/repo",
            "https://gitlab.com/group/repo-v2",
        ]
        assert _dedup_gitlab_prefixes(urls) == urls

    def test_empty_list(self):
        assert _dedup_gitlab_prefixes([]) == []

    def test_single_url(self):
        urls = ["https://gitlab.com/group/repo"]
        assert _dedup_gitlab_prefixes(urls) == urls

    def test_three_level_nesting(self):
        urls = [
            "https://gitlab.com/a",
            "https://gitlab.com/a/b",
            "https://gitlab.com/a/b/c",
        ]
        assert _dedup_gitlab_prefixes(urls) == ["https://gitlab.com/a/b/c"]

    def test_preserves_original_order(self):
        urls = [
            "https://gitlab.com/org/other",
            "https://gitlab.com/group/project",
            "https://gitlab.com/group",
        ]
        result = _dedup_gitlab_prefixes(urls)
        assert result == [
            "https://gitlab.com/org/other",
            "https://gitlab.com/group/project",
        ]


class TestExtractAllRepoUrls:
    def test_no_urls(self):
        assert extract_all_repo_urls("No repo here.") == []

    def test_single_github_url(self):
        text = "Bug in https://github.com/org/repo"
        assert extract_all_repo_urls(text) == ["https://github.com/org/repo"]

    def test_single_gitlab_url(self):
        text = "Bug in https://gitlab.com/group/project"
        assert extract_all_repo_urls(text) == ["https://gitlab.com/group/project"]

    def test_multiple_github_urls(self):
        text = "See https://github.com/org/repo-a and https://github.com/org/repo-b"
        assert extract_all_repo_urls(text) == [
            "https://github.com/org/repo-a",
            "https://github.com/org/repo-b",
        ]

    def test_mixed_forges(self):
        text = "Frontend: https://github.com/org/ui Backend: https://gitlab.com/group/api"
        assert extract_all_repo_urls(text) == [
            "https://gitlab.com/group/api",
            "https://github.com/org/ui",
        ]

    def test_deduplicates_across_text(self):
        text = "https://github.com/org/repo mentioned twice: https://github.com/org/repo"
        assert extract_all_repo_urls(text) == ["https://github.com/org/repo"]

    def test_filters_subpaths(self):
        text = "https://github.com/org/repo is the repo. PR: https://github.com/org/repo/pull/42"
        assert extract_all_repo_urls(text) == ["https://github.com/org/repo"]

    def test_gitlab_prefix_dedup(self):
        text = "https://gitlab.com/redhat/rhel-ai and https://gitlab.com/redhat/rhel-ai/core/images"
        result = extract_all_repo_urls(text)
        assert result == ["https://gitlab.com/redhat/rhel-ai/core/images"]

    def test_preserves_order(self):
        text = "https://gitlab.com/group/beta then https://github.com/org/alpha"
        result = extract_all_repo_urls(text)
        assert result[0] == "https://gitlab.com/group/beta"
        assert result[1] == "https://github.com/org/alpha"

    def test_gitlab_nested_group_url(self):
        text = "Repo at https://gitlab.com/redhat/rhel-ai/agentic-ci/autofix"
        assert extract_all_repo_urls(text) == [
            "https://gitlab.com/redhat/rhel-ai/agentic-ci/autofix"
        ]

    def test_gitlab_file_level_urls_extract_repo_root(self):
        text = (
            "See https://gitlab.com/redhat/rhel-ai/core/infrastructure/-/blob/main/"
            "data/gitlab_security_exceptions.yml "
            "and https://gitlab.com/redhat/rhel-ai/core/infrastructure/-/blob/main/CODEOWNERS"
        )
        assert extract_all_repo_urls(text) == [
            "https://gitlab.com/redhat/rhel-ai/core/infrastructure"
        ]


class TestSafeEnvParsing:
    def test_safe_int_valid(self):
        assert _safe_int("5", 2) == 5

    def test_safe_int_invalid(self):
        assert _safe_int("not_a_number", 2) == 2

    def test_safe_int_empty(self):
        assert _safe_int("", 2) == 2

    def test_safe_float_valid(self):
        assert _safe_float("3.5", 5.0) == 3.5

    def test_safe_float_invalid(self):
        assert _safe_float("not_a_number", 5.0) == 5.0

    def test_safe_float_empty(self):
        assert _safe_float("", 5.0) == 5.0


class TestIsTransientPushError:
    def test_commit_refs_error(self):
        assert _is_transient_push_error("remote: fatal error in commit_refs") is True

    def test_cannot_lock_ref(self):
        assert _is_transient_push_error("cannot lock ref 'refs/heads/branch'") is True

    def test_connection_refused(self):
        assert _is_transient_push_error("Connection refused") is True

    def test_connection_reset(self):
        assert _is_transient_push_error("Connection reset by peer") is True

    def test_service_unavailable(self):
        assert _is_transient_push_error("Service Unavailable") is True

    def test_internal_server_error(self):
        assert _is_transient_push_error("Internal Server Error") is True

    def test_hung_up(self):
        assert _is_transient_push_error("The remote end hung up unexpectedly") is True

    def test_permission_denied_not_transient(self):
        assert _is_transient_push_error("Permission denied") is False

    def test_empty_string_not_transient(self):
        assert _is_transient_push_error("") is False

    def test_returned_error_502(self):
        assert _is_transient_push_error("The requested URL returned error: 502") is True

    def test_returned_error_503(self):
        assert _is_transient_push_error("The requested URL returned error: 503") is True

    def test_bare_502_not_transient(self):
        assert _is_transient_push_error("some unrelated 502 text") is False

    def test_unable_to_access_not_transient(self):
        """HTTP 403 'unable to access' should not be treated as transient."""
        assert (
            _is_transient_push_error(
                "fatal: unable to access 'https://github.com/org/repo.git/': "
                "The requested URL returned error: 403"
            )
            is False
        )

    def test_case_insensitive(self):
        assert _is_transient_push_error("FATAL ERROR IN COMMIT_REFS") is True


class TestPushBranch:
    def test_success(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CompletedProcess([], 0),
            ]
            assert push_branch(tmp_path) is True

    def test_push_failure_no_retry_on_non_transient(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CalledProcessError(128, "git", stderr="Permission to repo.git denied"),
            ]
            assert push_branch(tmp_path, max_retries=2) is False
            assert mock_run.call_count == 2

    @patch("agentic_ci.git.time.sleep")
    def test_retry_on_transient_error_then_success(self, mock_sleep, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CalledProcessError(
                    128, "git", stderr="remote: fatal error in commit_refs"
                ),
                subprocess.CompletedProcess([], 0),
            ]
            assert push_branch(tmp_path, max_retries=2, retry_delay=1.0) is True
            mock_sleep.assert_called_once_with(1.0)

    @patch("agentic_ci.git.time.sleep")
    def test_retry_exhausted(self, mock_sleep, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CalledProcessError(128, "git", stderr="cannot lock ref 'refs/heads/b'"),
                subprocess.CalledProcessError(128, "git", stderr="cannot lock ref 'refs/heads/b'"),
                subprocess.CalledProcessError(128, "git", stderr="cannot lock ref 'refs/heads/b'"),
            ]
            assert push_branch(tmp_path, max_retries=2, retry_delay=1.0) is False
            assert mock_run.call_count == 4
            assert mock_sleep.call_count == 2

    @patch("agentic_ci.git.time.sleep")
    def test_exponential_backoff(self, mock_sleep, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CalledProcessError(128, "git", stderr="fatal error in commit_refs"),
                subprocess.CalledProcessError(128, "git", stderr="fatal error in commit_refs"),
                subprocess.CompletedProcess([], 0),
            ]
            assert push_branch(tmp_path, max_retries=2, retry_delay=2.0) is True
            assert mock_sleep.call_args_list[0][0] == (2.0,)
            assert mock_sleep.call_args_list[1][0] == (4.0,)

    @patch("agentic_ci.git.time.sleep")
    def test_timeout_retried(self, mock_sleep, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.TimeoutExpired("git push", 120),
                subprocess.CompletedProcess([], 0),
            ]
            assert push_branch(tmp_path, max_retries=1, retry_delay=1.0) is True
            mock_sleep.assert_called_once()

    def test_invalid_remote_rejected(self, tmp_path: Path):
        assert push_branch(tmp_path, remote="--evil") is False
        assert push_branch(tmp_path, remote="") is False
        assert push_branch(tmp_path, remote="foo..bar") is False

    def test_invalid_branch_rejected(self, tmp_path: Path):
        assert push_branch(tmp_path, branch="--evil") is False
        assert push_branch(tmp_path, branch="foo..bar") is False

    def test_explicit_branch(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            assert push_branch(tmp_path, branch="my-branch") is True
            args = mock_run.call_args[0][0]
            assert args == [
                "git",
                "push",
                "--force-with-lease",
                "--set-upstream",
                "origin",
                "my-branch",
            ]

    def test_no_retries_when_max_retries_zero(self, tmp_path: Path):
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CalledProcessError(128, "git", stderr="fatal error in commit_refs"),
            ]
            assert push_branch(tmp_path, max_retries=0) is False
            assert mock_run.call_count == 2

    def test_negative_max_retries_clamped_to_zero(self, tmp_path: Path):
        """Negative max_retries is clamped to 0 (single attempt, no retry)."""
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CalledProcessError(128, "git", stderr="fatal error in commit_refs"),
            ]
            assert push_branch(tmp_path, max_retries=-1) is False
            assert mock_run.call_count == 2

    @patch("agentic_ci.git.time.sleep")
    def test_invalid_retry_delay_uses_default(self, mock_sleep, tmp_path: Path):
        """Non-finite retry_delay falls back to 5.0."""
        with patch("agentic_ci.git.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CalledProcessError(128, "git", stderr="fatal error in commit_refs"),
                subprocess.CompletedProcess([], 0),
            ]
            assert push_branch(tmp_path, max_retries=1, retry_delay=float("nan")) is True
            mock_sleep.assert_called_once_with(5.0)

    @patch("agentic_ci.git.time.sleep")
    def test_retry_log_shows_next_delay_not_cumulative(self, mock_sleep, tmp_path: Path):
        """before_sleep logs the upcoming retry delay, not the cumulative idle time."""
        with (
            patch("agentic_ci.git.subprocess.run") as mock_run,
            patch("agentic_ci.git.log") as mock_log,
        ):
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CalledProcessError(128, "git", stderr="fatal error in commit_refs"),
                subprocess.CalledProcessError(128, "git", stderr="fatal error in commit_refs"),
                subprocess.CompletedProcess([], 0),
            ]
            assert push_branch(tmp_path, max_retries=2, retry_delay=2.0) is True
            warning_calls = [c for c in mock_log.warning.call_args_list if "retrying" in str(c)]
            assert len(warning_calls) == 2
            logged_delay_1 = warning_calls[0][0][3]
            logged_delay_2 = warning_calls[1][0][3]
            assert logged_delay_1 == 2.0
            assert logged_delay_2 == 4.0
