"""Tests for policy resolution."""

from agentic_ci.backends.openshell.policy import DEFAULT_ENDPOINTS, resolve_endpoints


def test_default_endpoints_returned_when_no_flag(tmp_path):
    result = resolve_endpoints(flag_path=None, workdir=str(tmp_path))
    assert result == list(DEFAULT_ENDPOINTS)


def test_flag_file_endpoints_merged(tmp_path):
    flag_file = tmp_path / "custom.yml"
    flag_file.write_text("endpoints:\n  - 'jira.example.com:443:read-only'\n")
    result = resolve_endpoints(flag_path=str(flag_file))
    assert result == list(DEFAULT_ENDPOINTS) + ["jira.example.com:443:read-only"]


def test_flag_file_without_endpoints_key(tmp_path):
    flag_file = tmp_path / "custom.yml"
    flag_file.write_text("custom: true\n")
    result = resolve_endpoints(flag_path=str(flag_file))
    assert result == list(DEFAULT_ENDPOINTS)


def test_repo_policy_file_merged(tmp_path):
    policy_dir = tmp_path / ".agentic-ci"
    policy_dir.mkdir()
    policy_file = policy_dir / "openshell-policy.yml"
    policy_file.write_text("endpoints:\n  - 'internal.example.com:443:full'\n")
    result = resolve_endpoints(workdir=str(tmp_path))
    assert result == list(DEFAULT_ENDPOINTS) + ["internal.example.com:443:full"]


def test_flag_takes_precedence_over_repo(tmp_path):
    policy_dir = tmp_path / ".agentic-ci"
    policy_dir.mkdir()
    (policy_dir / "openshell-policy.yml").write_text(
        "endpoints:\n  - 'repo.example.com:443:full'\n"
    )
    flag_file = tmp_path / "flag.yml"
    flag_file.write_text("endpoints:\n  - 'flag.example.com:443:full'\n")
    result = resolve_endpoints(flag_path=str(flag_file), workdir=str(tmp_path))
    assert "flag.example.com:443:full" in result
    assert "repo.example.com:443:full" not in result


def test_duplicate_endpoints_deduplicated(tmp_path):
    flag_file = tmp_path / "custom.yml"
    flag_file.write_text("endpoints:\n  - 'github.com:443:full'\n")
    result = resolve_endpoints(flag_path=str(flag_file))
    assert result.count("github.com:443:full") == 1


def test_endpoints_include_vertex_ai():
    result = resolve_endpoints()
    assert any("aiplatform.googleapis.com" in ep for ep in result)


def test_endpoints_include_anthropic_api():
    result = resolve_endpoints()
    assert any("api.anthropic.com" in ep for ep in result)
