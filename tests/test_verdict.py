"""Tests for verdict loading and validation."""

import json

import pytest

from agentic_ci.verdict import (
    VerdictError,
    load_verdict,
)


@pytest.fixture()
def verdict_file(tmp_path):
    """Helper to write a verdict JSON file."""

    def _write(data, filename="verdict.json"):
        p = tmp_path / filename
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    return _write


class TestLoadVerdict:
    def test_valid(self, verdict_file):
        path = verdict_file({"verdict": "committed", "summary": "Fixed"})
        result = load_verdict(
            path,
            required_fields={"verdict"},
            allowed_verdicts=frozenset({"committed"}),
        )
        assert result["verdict"] == "committed"

    def test_missing_file(self, tmp_path):
        with pytest.raises(VerdictError, match="not found"):
            load_verdict(
                tmp_path / "missing.json",
                required_fields={"verdict"},
                allowed_verdicts=frozenset({"ok"}),
            )

    def test_malformed_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{bad json", encoding="utf-8")
        with pytest.raises(VerdictError, match="Malformed JSON"):
            load_verdict(
                p,
                required_fields={"verdict"},
                allowed_verdicts=frozenset({"ok"}),
            )

    def test_not_object(self, verdict_file):
        path = verdict_file([1, 2, 3])
        with pytest.raises(VerdictError, match="must be a JSON object"):
            load_verdict(
                path,
                required_fields={"verdict"},
                allowed_verdicts=frozenset({"ok"}),
            )

    def test_missing_required_field(self, verdict_file):
        path = verdict_file({"verdict": "ok"})
        with pytest.raises(VerdictError, match="missing required field"):
            load_verdict(
                path,
                required_fields={"verdict", "summary"},
                allowed_verdicts=frozenset({"ok"}),
            )

    def test_unknown_verdict(self, verdict_file):
        path = verdict_file({"verdict": "banana"})
        with pytest.raises(VerdictError, match="Unknown"):
            load_verdict(
                path,
                required_fields={"verdict"},
                allowed_verdicts=frozenset({"ok"}),
            )

    def test_bool_field_validation(self, verdict_file):
        path = verdict_file({"verdict": "ok", "lint_passed": "yes"})
        with pytest.raises(VerdictError, match="must be bool"):
            load_verdict(
                path,
                required_fields={"verdict"},
                allowed_verdicts=frozenset({"ok"}),
            )

    def test_list_field_validation(self, verdict_file):
        path = verdict_file({"verdict": "ok", "files_changed": "not-a-list"})
        with pytest.raises(VerdictError, match="must be an array"):
            load_verdict(
                path,
                required_fields={"verdict"},
                allowed_verdicts=frozenset({"ok"}),
            )
