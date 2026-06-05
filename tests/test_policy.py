"""Tests for policy resolution."""

from agentic_ci.backends.openshell.policy import DEFAULT_ENDPOINTS, resolve_endpoints


def test_default_endpoints_returned_when_no_flag(tmp_path):
    result = resolve_endpoints(flag_path=None)
    assert result == list(DEFAULT_ENDPOINTS)


def test_default_endpoints_returned_with_flag(tmp_path):
    flag_file = tmp_path / "custom.yml"
    flag_file.write_text("custom: true")
    result = resolve_endpoints(flag_path=str(flag_file))
    assert result == list(DEFAULT_ENDPOINTS)


def test_endpoints_include_vertex_ai():
    result = resolve_endpoints()
    assert any("aiplatform.googleapis.com" in ep for ep in result)


def test_endpoints_include_anthropic_api():
    result = resolve_endpoints()
    assert any("api.anthropic.com" in ep for ep in result)
