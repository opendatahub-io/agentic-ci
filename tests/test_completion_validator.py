"""Tests for verdict-path guard on SIGKILL promotion.

When an agent is SIGKILL'd but the stream processor detected completion,
_process_stream checks whether the verdict file exists before promoting
the exit code to 0.  If the file is missing, the original exit code is
preserved so run_skill does not treat the run as successful.
"""

import io
import json
import subprocess
from unittest import mock

from agentic_ci.backend import Backend
from agentic_ci.skill import SkillConfig, run_skill


class FakeHarness:
    name = "test"
    auth_mode = "api-key"

    def create_stream_processor(self, pid=0):
        return FakeStreamProcessor()

    def model_env_var(self):
        return "TEST_MODEL"

    def default_model(self):
        return "test-model"


class FakeStreamProcessor:
    def process_line(self, line):
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return False
        if msg.get("type") == "user":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, str) and "FULL RUN COMPLETE" in content:
                        return True
        return False

    def flush_errors(self):
        pass


class ConcreteBackend(Backend):
    """Minimal concrete backend for testing _process_stream."""

    def setup(self):
        pass

    def stop(self):
        pass

    def run(
        self, prompt, model, streaming=True, otel_port=None, otel_rate_file=None, extra_args=None
    ):
        return 0


def _make_proc(stdout_lines, returncode=-9):
    """Build a mock subprocess.Popen with given stdout and returncode."""
    proc = mock.MagicMock(spec=subprocess.Popen)
    proc.pid = 12345
    encoded_lines = [line.encode("utf-8") for line in stdout_lines]
    proc.stdout = iter(encoded_lines)
    proc.stderr = io.BytesIO(b"")
    proc.returncode = returncode
    proc.kill = mock.MagicMock()
    proc.wait = mock.MagicMock()
    return proc


FULL_RUN_MSG = json.dumps(
    {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "FULL RUN COMPLETE"}]},
    }
)


class TestVerdictPathGuard:
    def test_no_verdict_path_promotes_rc(self):
        """Without a verdict_path set, stream_complete promotes rc to 0."""
        backend = ConcreteBackend(harness=FakeHarness())
        proc = _make_proc([FULL_RUN_MSG + "\n"], returncode=-9)
        rc, stream_complete = backend._process_stream(proc, streaming=True)
        rc = backend._resolve_exit_code(rc, stream_complete)
        assert rc == 0

    def test_verdict_exists_promotes_rc(self, tmp_path):
        """With verdict_path pointing to an existing file, rc is promoted to 0."""
        verdict = tmp_path / "verdict.json"
        verdict.write_text('{"verdict": "committed"}')
        backend = ConcreteBackend(harness=FakeHarness())
        backend.verdict_path = verdict
        proc = _make_proc([FULL_RUN_MSG + "\n"], returncode=-9)
        rc, stream_complete = backend._process_stream(proc, streaming=True)
        rc = backend._resolve_exit_code(rc, stream_complete)
        assert rc == 0

    def test_verdict_missing_keeps_original_rc(self, tmp_path):
        """With verdict_path pointing to a missing file, original rc is preserved."""
        backend = ConcreteBackend(harness=FakeHarness())
        backend.verdict_path = tmp_path / "verdict.json"
        proc = _make_proc([FULL_RUN_MSG + "\n"], returncode=-9)
        rc, stream_complete = backend._process_stream(proc, streaming=True)
        rc = backend._resolve_exit_code(rc, stream_complete)
        assert rc == -9

    def test_no_stream_complete_no_promotion(self):
        """Without stream completion, rc is preserved regardless of verdict_path."""
        backend = ConcreteBackend(harness=FakeHarness())
        backend.verdict_path = None
        proc = _make_proc(['{"type": "system"}\n'], returncode=1)
        rc, stream_complete = backend._process_stream(proc, streaming=True)
        rc = backend._resolve_exit_code(rc, stream_complete)
        assert rc == 1

    def test_process_stream_returns_tuple(self):
        """_process_stream returns (rc, stream_complete) tuple."""
        backend = ConcreteBackend(harness=FakeHarness())
        proc = _make_proc([FULL_RUN_MSG + "\n"], returncode=-9)
        result = backend._process_stream(proc, streaming=True)
        assert isinstance(result, tuple)
        assert len(result) == 2
        rc, stream_complete = result
        assert rc == -9
        assert stream_complete is True

    def test_process_stream_no_completion(self):
        """_process_stream returns stream_complete=False when stream doesn't complete."""
        backend = ConcreteBackend(harness=FakeHarness())
        proc = _make_proc(['{"type": "system"}\n'], returncode=1)
        rc, stream_complete = backend._process_stream(proc, streaming=True)
        assert rc == 1
        assert stream_complete is False


class TestOpenShellDownloadBeforeVerdict:
    """Verify OpenShellBackend downloads the workdir before checking the verdict."""

    def test_verdict_found_after_download(self, tmp_path):
        """When download creates the verdict file, _resolve_exit_code sees it and promotes rc."""
        backend = ConcreteBackend(harness=FakeHarness())
        verdict = tmp_path / "autofix-output" / ".autofix-verdict.json"
        backend.verdict_path = verdict

        proc = _make_proc([FULL_RUN_MSG + "\n"], returncode=-9)
        rc, stream_complete = backend._process_stream(proc, streaming=True)

        assert not verdict.exists()
        assert rc == -9
        assert stream_complete is True

        verdict.parent.mkdir(parents=True)
        verdict.write_text('{"verdict": "committed"}')

        rc = backend._resolve_exit_code(rc, stream_complete)
        assert rc == 0

    def test_verdict_still_missing_after_download(self, tmp_path):
        """When download doesn't produce the verdict file, rc is preserved."""
        backend = ConcreteBackend(harness=FakeHarness())
        backend.verdict_path = tmp_path / "autofix-output" / ".autofix-verdict.json"

        proc = _make_proc([FULL_RUN_MSG + "\n"], returncode=-9)
        rc, stream_complete = backend._process_stream(proc, streaming=True)

        rc = backend._resolve_exit_code(rc, stream_complete)
        assert rc == -9


class TestOpenShellRunVerdictOrdering:
    """Integration test: OpenShellBackend.run() downloads before verdict check."""

    def test_run_downloads_before_verdict_check(self, tmp_path, monkeypatch):
        """OpenShellBackend.run() must download the workdir before checking the verdict.

        Simulates an agent that writes a verdict file inside the sandbox.
        The mock download copies it to the host. The verdict check should
        find it and promote the exit code to 0.
        """
        from agentic_ci.backends.openshell import OpenShellBackend
        from agentic_ci.harness import ClaudeCodeHarness

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        harness = ClaudeCodeHarness()
        workdir = tmp_path / "repo"
        workdir.mkdir()
        backend = OpenShellBackend(workdir=str(workdir), harness=harness)
        verdict = workdir / "autofix-output" / ".autofix-verdict.json"
        backend.verdict_path = verdict

        call_order = []

        def mock_exec_cmd_streaming(cmd):
            call_order.append("exec")
            return _make_proc([FULL_RUN_MSG + "\n"], returncode=-9)

        def mock_download(sandbox_path, local_dest):
            call_order.append("download")
            verdict.parent.mkdir(parents=True, exist_ok=True)
            verdict.write_text('{"verdict": "committed"}')

        with (
            mock.patch.object(backend, "_write_env_script"),
            mock.patch(
                "agentic_ci.backends.openshell.sandbox.exec_cmd_streaming",
                side_effect=mock_exec_cmd_streaming,
            ),
            mock.patch(
                "agentic_ci.backends.openshell.sandbox.download",
                side_effect=mock_download,
            ),
        ):
            rc = backend.run(prompt="test", model="test-model", otel_port=None)

        assert rc == 0, f"Expected rc=0 (verdict found after download), got {rc}"
        assert call_order == ["exec", "download"]

    def test_run_preserves_rc_when_verdict_missing_after_download(self, tmp_path, monkeypatch):
        """If the verdict file is still missing after download, rc is preserved."""
        from agentic_ci.backends.openshell import OpenShellBackend
        from agentic_ci.harness import ClaudeCodeHarness

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        harness = ClaudeCodeHarness()
        workdir = tmp_path / "repo"
        workdir.mkdir()
        backend = OpenShellBackend(workdir=str(workdir), harness=harness)
        backend.verdict_path = workdir / "autofix-output" / ".autofix-verdict.json"

        def mock_exec_cmd_streaming(cmd):
            return _make_proc([FULL_RUN_MSG + "\n"], returncode=-9)

        def mock_download(sandbox_path, local_dest):
            pass

        with (
            mock.patch.object(backend, "_write_env_script"),
            mock.patch(
                "agentic_ci.backends.openshell.sandbox.exec_cmd_streaming",
                side_effect=mock_exec_cmd_streaming,
            ),
            mock.patch(
                "agentic_ci.backends.openshell.sandbox.download",
                side_effect=mock_download,
            ),
        ):
            rc = backend.run(prompt="test", model="test-model", otel_port=None)

        assert rc == -9, f"Expected rc=-9 (verdict still missing), got {rc}"


class TestRunSkillVerdictWiring:
    def test_default_runner_receives_verdict_path(self, tmp_path):
        """run_skill passes verdict_path to the default runner."""
        received_kwargs = {}

        def capture_runner(work_dir, prompt, output_file, **kwargs):
            received_kwargs.update(kwargs)
            (work_dir / "verdict.json").write_text('{"verdict": "committed"}')
            return 0

        config = SkillConfig(
            skill_name="test-skill",
            verdict_path_fn=lambda wd: wd / "verdict.json",
            verdict_loader=lambda wd: json.loads((wd / "verdict.json").read_text()),
        )

        with mock.patch("agentic_ci.skill._default_run_container", capture_runner):
            run_skill(
                config,
                ticket_key="TEST-1",
                work_dir=tmp_path,
                config_dir=tmp_path,
                dry_run=False,
            )

        assert "verdict_path" in received_kwargs
        assert received_kwargs["verdict_path"] == tmp_path / "verdict.json"

    def test_custom_runner_no_verdict_path(self, tmp_path):
        """Custom container_runner does not receive verdict_path."""
        received_kwargs = {}

        def custom_runner(work_dir, prompt, output_file, **kwargs):
            received_kwargs.update(kwargs)
            return 0

        config = SkillConfig(
            skill_name="test-skill",
            container_runner=custom_runner,
            verdict_loader=lambda wd: {"verdict": "committed"},
        )

        run_skill(
            config,
            ticket_key="TEST-1",
            work_dir=tmp_path,
            config_dir=tmp_path,
            dry_run=False,
        )

        assert "verdict_path" not in received_kwargs
