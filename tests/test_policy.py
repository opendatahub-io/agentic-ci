"""Tests for policy resolution."""

import os

from agentic_ci.policy import DEFAULT_POLICY, REPO_POLICY_PATH, resolve


def test_explicit_flag_takes_priority(tmp_path):
    flag_file = tmp_path / "custom.yml"
    flag_file.write_text("custom: true")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo_policy = repo_dir / REPO_POLICY_PATH
    repo_policy.parent.mkdir(parents=True)
    repo_policy.write_text("repo: true")

    result = resolve(flag_path=str(flag_file), workdir=str(repo_dir))
    assert result == str(flag_file.resolve())


def test_repo_policy_discovered(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    repo_policy = repo_dir / REPO_POLICY_PATH
    repo_policy.parent.mkdir(parents=True)
    repo_policy.write_text("repo: true")

    result = resolve(workdir=str(repo_dir))
    assert result == str(repo_policy.resolve())


def test_default_policy_written_to_temp(tmp_path):
    result = resolve(workdir=str(tmp_path))
    assert os.path.isfile(result)
    with open(result) as f:
        content = f.read()
    assert content == DEFAULT_POLICY
    os.unlink(result)


def test_flag_path_is_absolute(tmp_path):
    flag_file = tmp_path / "policy.yml"
    flag_file.write_text("test: true")
    result = resolve(flag_path=str(flag_file))
    assert os.path.isabs(result)
