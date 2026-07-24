"""Tests for top-level agentic-ci CLI command flow."""

import sys
from unittest import mock

import pytest

from agentic_ci import cli


def _run_main(argv: list[str]) -> None:
    with (
        mock.patch.object(sys, "argv", argv),
        mock.patch("agentic_ci.cli.version", return_value="0.0.0"),
    ):
        cli.main()


def test_stop_cursor_does_not_require_cursor_api_key(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    backend = mock.Mock()

    with mock.patch("agentic_ci.cli.create_backend", return_value=backend):
        _run_main(["agentic-ci", "stop", "--harness", "cursor", "--backend", "local"])

    backend.stop.assert_called_once()


@pytest.mark.parametrize(
    "argv",
    [
        ["agentic-ci", "setup", "--harness", "cursor", "--backend", "local"],
        ["agentic-ci", "run", "--harness", "cursor", "--backend", "local", "hello"],
    ],
)
def test_setup_and_run_still_require_cursor_api_key(monkeypatch, capsys, argv):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    with (
        mock.patch("agentic_ci.cli.create_backend") as mock_create_backend,
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_main(argv)

    assert exc_info.value.code == 1
    assert "CURSOR_API_KEY must be set" in capsys.readouterr().err
    mock_create_backend.assert_not_called()
