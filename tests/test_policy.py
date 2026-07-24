"""Tests for policy resolution."""

import pytest
import yaml

from agentic_ci.backends.openshell.policy import (
    CURSOR_ENDPOINTS,
    DEFAULT_ENDPOINTS,
    _parse_endpoint_string,
    generate_policy_yaml,
    resolve_endpoints,
)
from agentic_ci.harness import CursorHarness

CURSOR_TLS_SKIP_HOSTS = {
    "api2.cursor.sh",
    "*.cursor.sh",
    "*.api5.cursor.sh",
    "*.us.api5.cursor.sh",
}
CURSOR_POLICY_ENDPOINTS = [*DEFAULT_ENDPOINTS, *CURSOR_ENDPOINTS]


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


def test_non_cursor_endpoints_exclude_cursor_defaults():
    result = resolve_endpoints(tls_skip_hosts=None)
    for endpoint in CURSOR_ENDPOINTS:
        assert endpoint not in result


def test_cursor_tls_skip_hosts_include_cursor_defaults():
    result = resolve_endpoints(tls_skip_hosts=[("api2.cursor.sh", 443, "read-write")])
    for endpoint in CURSOR_ENDPOINTS:
        assert endpoint in result


# --- generate_policy_yaml tests ---


def test_policy_has_tls_skip_on_designated_endpoints():
    policy_str = generate_policy_yaml(
        CURSOR_POLICY_ENDPOINTS,
        ["/usr/local/bin/agent", "/usr/local/lib/cursor-agent/node"],
        tls_skip_hosts=CURSOR_TLS_SKIP_HOSTS,
    )
    policy = yaml.safe_load(policy_str)
    skip_eps = policy["network_policies"]["tls_skip"]["endpoints"]
    for ep in skip_eps:
        assert ep["tls"] == "skip", f"Expected tls: skip on {ep['host']}"
        assert ep["host"] in CURSOR_TLS_SKIP_HOSTS


def test_policy_standard_endpoints_have_no_tls():
    policy_str = generate_policy_yaml(
        CURSOR_POLICY_ENDPOINTS,
        ["/usr/local/bin/agent"],
        tls_skip_hosts=CURSOR_TLS_SKIP_HOSTS,
    )
    policy = yaml.safe_load(policy_str)
    standard_eps = policy["network_policies"]["standard"]["endpoints"]
    for ep in standard_eps:
        assert "tls" not in ep, f"Unexpected tls field on {ep['host']}"


def test_policy_includes_sandbox_defaults():
    policy_str = generate_policy_yaml(
        CURSOR_POLICY_ENDPOINTS,
        ["/usr/local/bin/agent"],
        tls_skip_hosts=CURSOR_TLS_SKIP_HOSTS,
    )
    policy = yaml.safe_load(policy_str)
    assert policy["version"] == 1
    assert policy["filesystem_policy"]["include_workdir"] is True
    assert "/sandbox" in policy["filesystem_policy"]["read_write"]
    assert policy["landlock"]["compatibility"] == "best_effort"
    assert policy["process"]["run_as_user"] == "sandbox"


def test_policy_binaries_propagated():
    bins = ["/usr/local/bin/agent", "/usr/local/lib/cursor-agent/node"]
    policy_str = generate_policy_yaml(
        CURSOR_POLICY_ENDPOINTS,
        bins,
        tls_skip_hosts=CURSOR_TLS_SKIP_HOSTS,
    )
    policy = yaml.safe_load(policy_str)
    for group in policy["network_policies"].values():
        paths = [b["path"] for b in group["binaries"]]
        assert paths == bins


def test_policy_otel_port_added():
    policy_str = generate_policy_yaml(
        CURSOR_POLICY_ENDPOINTS,
        ["/usr/local/bin/agent"],
        tls_skip_hosts=CURSOR_TLS_SKIP_HOSTS,
        otel_port=4318,
    )
    policy = yaml.safe_load(policy_str)
    standard_eps = policy["network_policies"]["standard"]["endpoints"]
    otel_eps = [e for e in standard_eps if e["host"] == "host.openshell.internal"]
    assert len(otel_eps) == 1
    assert otel_eps[0]["port"] == 4318
    assert otel_eps[0]["access"] == "read-write"


def test_policy_all_default_endpoints_present():
    policy_str = generate_policy_yaml(
        CURSOR_POLICY_ENDPOINTS,
        ["/usr/local/bin/agent"],
        tls_skip_hosts=CURSOR_TLS_SKIP_HOSTS,
    )
    policy = yaml.safe_load(policy_str)
    all_eps = []
    for group in policy["network_policies"].values():
        all_eps.extend(group["endpoints"])
    all_hosts = {ep["host"] for ep in all_eps}
    for ep_str in CURSOR_POLICY_ENDPOINTS:
        host = ep_str.split(":")[0]
        assert host in all_hosts, f"Missing endpoint host: {host}"


def test_policy_no_tls_skip_when_empty():
    """When tls_skip_hosts is empty, all endpoints go to standard group."""
    policy_str = generate_policy_yaml(
        DEFAULT_ENDPOINTS,
        ["/usr/local/bin/claude"],
        tls_skip_hosts=set(),
    )
    policy = yaml.safe_load(policy_str)
    assert "tls_skip" not in policy["network_policies"]
    assert "standard" in policy["network_policies"]
    standard_eps = policy["network_policies"]["standard"]["endpoints"]
    for ep in standard_eps:
        assert "tls" not in ep


def test_cursor_harness_tls_skip_hosts_integration(monkeypatch):
    """End-to-end: CursorHarness.tls_skip_hosts (tuples) → generate_policy_yaml.

    The harness returns list[tuple[str,int,str]] but the policy generator
    expects plain hostname strings.  This test verifies the transformation
    in sandbox.py works correctly.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    harness = CursorHarness()
    raw_hosts = harness.tls_skip_hosts  # list of tuples

    # Simulate the transformation sandbox.py now performs
    tls_host_names = [h for h, _, _ in raw_hosts]

    policy_str = generate_policy_yaml(
        CURSOR_POLICY_ENDPOINTS,
        harness.sandbox_binaries,
        tls_skip_hosts=tls_host_names,
    )
    policy = yaml.safe_load(policy_str)

    assert "tls_skip" in policy["network_policies"], (
        "tls_skip group missing — tls: skip would not be applied"
    )
    skip_eps = policy["network_policies"]["tls_skip"]["endpoints"]
    skip_hosts = {ep["host"] for ep in skip_eps}

    for host, _, _ in raw_hosts:
        assert host in skip_hosts, f"CursorHarness host {host!r} not in tls_skip group"
    for ep in skip_eps:
        assert ep["tls"] == "skip"


# --- _parse_endpoint_string validation tests ---


def test_parse_endpoint_string_valid():
    result = _parse_endpoint_string("github.com:443:full")
    assert result == {"host": "github.com", "port": 443, "access": "full"}


def test_parse_endpoint_string_with_protocol():
    result = _parse_endpoint_string("github.com:443:full:https")
    assert result == {"host": "github.com", "port": 443, "access": "full", "protocol": "https"}


def test_parse_endpoint_string_with_enforcement():
    result = _parse_endpoint_string("github.com:443:full:https:strict")
    assert result == {
        "host": "github.com",
        "port": 443,
        "access": "full",
        "protocol": "https",
        "enforcement": "strict",
    }


def test_parse_endpoint_string_too_few_parts():
    with pytest.raises(ValueError, match="Malformed endpoint"):
        _parse_endpoint_string("github.com")


def test_parse_endpoint_string_two_parts():
    with pytest.raises(ValueError, match="Malformed endpoint"):
        _parse_endpoint_string("github.com:443")


def test_parse_endpoint_string_non_numeric_port():
    with pytest.raises(ValueError, match="not a valid integer"):
        _parse_endpoint_string("github.com:abc:full")
