"""Tests for the gate registry and CLI gate integration."""

import argparse
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentic_ci.cli import cmd_run
from agentic_ci.gates import (
    GATE_REGISTRY,
    gitleaks_scan,
    resolve_gates,
    validate_gate_env,
)
from agentic_ci.git import GitDiffError

# A secret-looking value planted in stderr or exception text.  Gate error
# strings must never contain it; the job log must keep it.
SECRET = "glpat-PLANTEDsecretTOKEN1234567890"


class TestGateRegistry:
    def test_built_in_gates_registered(self):
        assert "sensitive-files" in GATE_REGISTRY
        assert "commit-author" in GATE_REGISTRY
        assert "gitleaks" in GATE_REGISTRY

    def test_sensitive_files_is_post(self):
        assert GATE_REGISTRY["sensitive-files"].phase == "post"

    def test_commit_author_requires_bot_email(self):
        assert "BOT_EMAIL" in GATE_REGISTRY["commit-author"].required_env

    def test_gitleaks_has_no_required_env(self):
        assert GATE_REGISTRY["gitleaks"].required_env == []


class TestResolveGates:
    def test_resolve_known_gates(self):
        specs = resolve_gates(["sensitive-files", "gitleaks"])
        assert len(specs) == 2
        assert specs[0].name == "sensitive-files"
        assert specs[1].name == "gitleaks"

    def test_resolve_unknown_gate_exits(self):
        with pytest.raises(SystemExit, match="unknown gate"):
            resolve_gates(["nonexistent-gate"])


class TestValidateGateEnv:
    def test_all_vars_present(self):
        gates = [GATE_REGISTRY["commit-author"]]
        with patch.dict(os.environ, {"BOT_EMAIL": "bot@ci.com"}):
            validate_gate_env(gates)

    def test_missing_var_exits(self):
        gates = [GATE_REGISTRY["commit-author"]]
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit, match="BOT_EMAIL"):
                validate_gate_env(gates)

    def test_reports_all_missing_at_once(self):
        gates = [GATE_REGISTRY["commit-author"], GATE_REGISTRY["sensitive-files"]]
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                validate_gate_env(gates)
            msg = str(exc_info.value)
            assert "BOT_EMAIL" in msg

    def test_no_required_env_passes(self):
        gates = [GATE_REGISTRY["sensitive-files"]]
        with patch.dict(os.environ, {}, clear=True):
            validate_gate_env(gates)


class TestRunSensitiveFiles:
    def test_no_changes_passes(self):
        gate = GATE_REGISTRY["sensitive-files"]
        with patch("agentic_ci.gates.get_changed_files", return_value=[]):
            errors = gate.fn(workdir="/tmp/test")
        assert errors == []

    def test_sensitive_file_blocked(self):
        gate = GATE_REGISTRY["sensitive-files"]
        with patch("agentic_ci.gates.get_changed_files", return_value=[".env", "main.py"]):
            errors = gate.fn(workdir="/tmp/test")
        assert len(errors) == 1
        assert ".env" in errors[0]

    def test_git_diff_error_text_stays_in_log(self, caplog):
        gate = GATE_REGISTRY["sensitive-files"]
        with (
            caplog.at_level(logging.ERROR, logger="agentic_ci.gates"),
            patch(
                "agentic_ci.gates.get_changed_files",
                side_effect=GitDiffError(f"git diff failed: fatal: {SECRET}"),
            ),
        ):
            errors = gate.fn(workdir="/tmp/test")
        assert errors == [
            "Could not compute changed files (GitDiffError); see the CI job log",
        ]
        assert SECRET in caplog.text


class TestRunCommitAuthor:
    def test_matching_author_passes(self):
        gate = GATE_REGISTRY["commit-author"]
        with (
            patch.dict(os.environ, {"BOT_EMAIL": "bot@ci.com"}),
            patch(
                "agentic_ci.gates.get_commit_info",
                return_value={"email": "bot@ci.com", "subject": "fix"},
            ),
        ):
            errors = gate.fn(workdir="/tmp/test")
        assert errors == []

    def test_wrong_author_fails(self):
        gate = GATE_REGISTRY["commit-author"]
        with (
            patch.dict(os.environ, {"BOT_EMAIL": "bot@ci.com"}),
            patch(
                "agentic_ci.gates.get_commit_info",
                return_value={"email": "human@ci.com", "subject": "fix"},
            ),
        ):
            errors = gate.fn(workdir="/tmp/test")
        assert len(errors) == 1
        assert "human@ci.com" in errors[0]

    def test_git_log_error_text_stays_in_log(self, caplog):
        gate = GATE_REGISTRY["commit-author"]
        exc = subprocess.CalledProcessError(128, ["git", "log", SECRET], stderr=f"fatal: {SECRET}")
        with (
            caplog.at_level(logging.ERROR, logger="agentic_ci.gates"),
            patch.dict(os.environ, {"BOT_EMAIL": "bot@ci.com"}),
            patch("agentic_ci.gates.get_commit_info", side_effect=exc),
        ):
            errors = gate.fn(workdir="/tmp/test")
        assert errors == ["Could not read commit info (CalledProcessError); see the CI job log"]
        assert f"fatal: {SECRET}" in caplog.text


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


# A GitHub token shape that gitleaks' built-in github-pat rule detects, built
# at runtime so this file itself does not trip secret scanners.
LEAKED_TOKEN = "ghp" + "_R2a9xQ7mK4pL8vN3bT6wY1zC5dF0hJ2sU9eA"


@pytest.fixture
def leaky_repo(tmp_path):
    """A repo whose one commit after ``base`` adds a GitHub token."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "bot@ci.com")
    _git(repo, "config", "user.name", "bot")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "creds.py").write_text(f'token = "{LEAKED_TOKEN}"  # gitleaks:allow\n')
    _git(repo, "add", "creds.py")
    _git(repo, "commit", "-q", "-m", "add creds")
    return repo, base


# Records what gitleaks was given, then checks the scan repository can
# read the commit range it was asked to scan.
FAKE_GITLEAKS = """#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
source = args[args.index("--source") + 1]
log_opts = next(a for a in args if a.startswith("--log-opts="))[len("--log-opts="):]
log = subprocess.run(["git", "-C", source, "log", "--format=%s", log_opts],
                     capture_output=True, text=True, check=True).stdout
json.dump({"args": args, "cwd": os.getcwd(), "log": log,
           "env": {k: os.environ.get(k) for k in ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")},
           "config": open(args[args.index("--config") + 1]).read(),
           "ignore": os.listdir(args[args.index("--gitleaks-ignore-path") + 1])},
          open(os.environ["FAKE_GITLEAKS_OUT"], "w"))
"""


class TestGitleaksScan:
    def test_missing_binary_fails_closed(self, tmp_path):
        with patch("shutil.which", return_value=None):
            errors = gitleaks_scan(tmp_path)
        assert len(errors) == 1
        assert "not installed" in errors[0]

    def test_timeout_fails_closed(self, leaky_repo):
        repo, base = leaky_repo
        real_run = subprocess.run

        def run(cmd, *args, **kwargs):
            if cmd[0] == "gitleaks":
                raise subprocess.TimeoutExpired(cmd="gitleaks", timeout=120)
            return real_run(cmd, *args, **kwargs)

        with (
            patch("shutil.which", return_value="/usr/bin/gitleaks"),
            patch("subprocess.run", side_effect=run),
        ):
            errors = gitleaks_scan(repo, compare_ref=base)
        assert len(errors) == 1
        assert "timed out" in errors[0]

    def test_no_new_commits_skips_scan(self, leaky_repo):
        repo, _ = leaky_repo
        with (
            patch("shutil.which", return_value="/usr/bin/gitleaks"),
            patch("subprocess.run", wraps=subprocess.run) as run,
        ):
            assert gitleaks_scan(repo, compare_ref="HEAD") == []
        assert all(c.args[0][0] == "git" for c in run.call_args_list)

    def test_scan_ignores_agent_controlled_config(self, leaky_repo, tmp_path, monkeypatch):
        repo, base = leaky_repo
        (repo / ".gitleaks.toml").write_text('[allowlist]\npaths = [".*"]\n')
        (repo / ".gitleaksignore").write_text("anything\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "gitleaks"
        fake.write_text(FAKE_GITLEAKS)
        fake.chmod(0o755)
        out = tmp_path / "gitleaks.json"
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_GITLEAKS_OUT", str(out))
        monkeypatch.setenv("GITLEAKS_CONFIG", str(repo / ".gitleaks.toml"))
        monkeypatch.setenv("GITLEAKS_CONFIG_TOML", '[allowlist]\npaths = [".*"]\n')

        assert gitleaks_scan(repo, compare_ref=base) == []

        seen = json.loads(out.read_text())
        args = seen["args"]
        source = Path(args[args.index("--source") + 1])
        assert not source.is_relative_to(repo)
        assert not Path(seen["cwd"]).is_relative_to(repo)
        assert seen["log"] == "add creds\n"
        assert seen["config"] == "[extend]\nuseDefault = true\n"
        assert seen["ignore"] == []
        assert "--ignore-gitleaks-allow" in args
        assert seen["env"] == {"GITLEAKS_CONFIG": None, "GITLEAKS_CONFIG_TOML": None}
        assert not source.exists()

    def test_replace_ref_does_not_skip_the_scan(self, leaky_repo, tmp_path, monkeypatch):
        """A replace ref giving the base HEAD as a parent makes the range look empty."""
        repo, base = leaky_repo
        _git(repo, "replace", "--graft", base, "HEAD")
        assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "0"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "gitleaks"
        fake.write_text(FAKE_GITLEAKS)
        fake.chmod(0o755)
        out = tmp_path / "gitleaks.json"
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_GITLEAKS_OUT", str(out))

        assert gitleaks_scan(repo, compare_ref=base) == []

        assert json.loads(out.read_text())["log"] == "add creds\n"

    @pytest.mark.skipif(not shutil.which("gitleaks"), reason="gitleaks not installed")
    def test_replace_ref_does_not_hide_a_secret(self, leaky_repo):
        repo, base = leaky_repo
        _git(repo, "replace", "--graft", base, "HEAD")
        errors = gitleaks_scan(repo, compare_ref=base)
        assert errors and "detected potential secrets" in errors[0]

    @pytest.mark.skipif(not shutil.which("gitleaks"), reason="gitleaks not installed")
    def test_planted_allowlists_do_not_hide_a_secret(self, leaky_repo, monkeypatch):
        repo, base = leaky_repo
        (repo / ".gitleaks.toml").write_text('[allowlist]\npaths = [".*"]\n')
        fingerprint = _git(repo, "rev-parse", "HEAD") + ":creds.py:github-pat:1"
        (repo / ".gitleaksignore").write_text(fingerprint + "\n")
        monkeypatch.setenv("GITLEAKS_CONFIG", str(repo / ".gitleaks.toml"))
        errors = gitleaks_scan(repo, compare_ref=base)
        assert errors and "detected potential secrets" in errors[0]

    def test_rev_parse_error_stderr_stays_in_log(self, tmp_path, caplog):
        exc = subprocess.CalledProcessError(
            128, ["git", "rev-parse"], stderr=f"fatal: bad revision {SECRET}\n"
        )
        with (
            caplog.at_level(logging.ERROR, logger="agentic_ci.gates"),
            patch("shutil.which", return_value="/usr/bin/gitleaks"),
            patch("subprocess.run", side_effect=exc),
        ):
            errors = gitleaks_scan(tmp_path)
        assert errors == [
            "gitleaks pre-check failed: git rev-parse error (CalledProcessError); "
            "see the CI job log"
        ]
        assert SECRET not in errors[0]
        assert f"fatal: bad revision {SECRET}" in caplog.text

    def test_rev_list_timeout_fails_closed(self, leaky_repo, caplog):
        repo, base = leaky_repo
        real_run = subprocess.run

        def run(cmd, *args, **kwargs):
            if "rev-list" in cmd:
                raise subprocess.TimeoutExpired(cmd="git", timeout=30, stderr=SECRET.encode())
            return real_run(cmd, *args, **kwargs)

        with (
            caplog.at_level(logging.ERROR, logger="agentic_ci.gates"),
            patch("shutil.which", return_value="/usr/bin/gitleaks"),
            patch("subprocess.run", side_effect=run),
        ):
            errors = gitleaks_scan(repo, compare_ref=base)
        assert errors == [
            "gitleaks pre-check failed: git rev-list error (TimeoutExpired); see the CI job log"
        ]
        assert SECRET in caplog.text

    def test_git_os_error_fails_closed(self, tmp_path):
        with (
            patch("shutil.which", return_value="/usr/bin/gitleaks"),
            patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file", "git")),
        ):
            errors = gitleaks_scan(tmp_path)
        assert errors == [
            "gitleaks pre-check failed: git rev-parse error (FileNotFoundError); see the CI job log"
        ]


class TestRunJiraDescriptionEditors:
    ENV = {
        "TICKET_KEY": "TEST-1",
        "INTERNAL_DOMAIN_RE": r"@redhat\.com$",
    }

    def _run(self, env=None, client=None, from_env_exc=None):
        gate = GATE_REGISTRY["jira-description-editors"]
        from_env = (
            patch("agentic_ci.gates.JiraClient.from_env", side_effect=from_env_exc)
            if from_env_exc is not None
            else patch("agentic_ci.gates.JiraClient.from_env", return_value=client)
        )
        with patch.dict(os.environ, env or self.ENV, clear=True), from_env:
            return gate.fn()

    def test_invalid_pattern_error_text_stays_in_log(self, caplog):
        env = {"TICKET_KEY": "TEST-1", "INTERNAL_DOMAIN_RE": f"({SECRET}$"}
        with caplog.at_level(logging.ERROR, logger="agentic_ci.gates"):
            result = self._run(env=env, client=MagicMock())
        assert result == ["Invalid INTERNAL_DOMAIN_RE pattern; see the CI job log"]
        assert "missing )" in caplog.text

    def test_client_creation_error_text_stays_in_log(self, caplog):
        with caplog.at_level(logging.ERROR, logger="agentic_ci.gates"):
            result = self._run(from_env_exc=RuntimeError(f"bad token {SECRET}"))
        assert result == ["Could not create Jira client (RuntimeError); see the CI job log"]
        assert SECRET in caplog.text

    def test_get_issue_error_text_stays_in_log(self, caplog):
        client = MagicMock()
        client.get_issue.side_effect = RuntimeError(f"401 body {SECRET}")
        with caplog.at_level(logging.ERROR, logger="agentic_ci.gates"):
            result = self._run(client=client)
        assert result == ["Could not fetch issue TEST-1 (RuntimeError); see the CI job log"]
        assert SECRET in caplog.text

    def test_changelog_error_text_stays_in_log(self, caplog):
        client = MagicMock()
        client.get_issue.return_value = {"reporter_email": "dev@redhat.com"}
        client.get_description_editors.side_effect = RuntimeError(f"500 body {SECRET}")
        with caplog.at_level(logging.ERROR, logger="agentic_ci.gates"):
            result = self._run(client=client)
        assert result == ["Could not fetch changelog for TEST-1 (RuntimeError); see the CI job log"]
        assert SECRET in caplog.text

    def test_trusted_editors_pass(self):
        client = MagicMock()
        client.get_issue.return_value = {"reporter_email": "dev@redhat.com"}
        client.get_description_editors.return_value = ["dev@redhat.com"]
        assert self._run(client=client) == []

    def test_untrusted_editor_returns_one_error(self):
        client = MagicMock()
        client.get_issue.return_value = {"reporter_email": "dev@redhat.com"}
        client.get_description_editors.return_value = ["dev@redhat.com", "x@example.com"]
        assert self._run(client=client) == [
            "Description edited by untrusted user(s): x@example.com. "
            "Only users matching @redhat\\.com$ may author or edit the ticket description."
        ]


class TestCliPreGates:
    ENV = {
        "JIRA_URL": "https://jira.example.com",
        "JIRA_API_TOKEN": "token",
        "TICKET_KEY": "TEST-1",
        "INTERNAL_DOMAIN_RE": r"@redhat\.com$",
    }

    def test_blocked_description_editors_prints_one_line(self, tmp_path, capsys):
        client = MagicMock()
        client.get_issue.return_value = {"reporter_email": "dev@redhat.com"}
        client.get_description_editors.return_value = ["x@example.com"]
        args = argparse.Namespace(
            pre_gates="jira-description-editors",
            post_gates=None,
            workdir=str(tmp_path),
        )
        harness = MagicMock()
        with (
            patch.dict(os.environ, self.ENV, clear=True),
            patch("agentic_ci.gates.JiraClient.from_env", return_value=client),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_run(args, MagicMock(), harness)

        assert exc.value.code == 0
        harness.model_env_var.assert_not_called()
        out = capsys.readouterr().out.splitlines()
        assert [line for line in out if line.startswith("Pre-gate")] == [
            "Pre-gate jira-description-editors blocked: "
            "Description edited by untrusted user(s): x@example.com. "
            "Only users matching @redhat\\.com$ may author or edit the ticket description."
        ]
