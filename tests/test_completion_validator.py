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
        rc = backend._process_stream(proc, streaming=True)
        assert rc == 0

    def test_verdict_exists_promotes_rc(self, tmp_path):
        """With verdict_path pointing to an existing file, rc is promoted to 0."""
        verdict = tmp_path / "verdict.json"
        verdict.write_text('{"verdict": "committed"}')
        backend = ConcreteBackend(harness=FakeHarness())
        backend.verdict_path = verdict
        proc = _make_proc([FULL_RUN_MSG + "\n"], returncode=-9)
        rc = backend._process_stream(proc, streaming=True)
        assert rc == 0

    def test_verdict_missing_keeps_original_rc(self, tmp_path):
        """With verdict_path pointing to a missing file, original rc is preserved."""
        backend = ConcreteBackend(harness=FakeHarness())
        backend.verdict_path = tmp_path / "verdict.json"
        proc = _make_proc([FULL_RUN_MSG + "\n"], returncode=-9)
        rc = backend._process_stream(proc, streaming=True)
        assert rc == -9

    def test_no_stream_complete_no_promotion(self):
        """Without stream completion, rc is preserved regardless of verdict_path."""
        backend = ConcreteBackend(harness=FakeHarness())
        backend.verdict_path = None
        proc = _make_proc(['{"type": "system"}\n'], returncode=1)
        rc = backend._process_stream(proc, streaming=True)
        assert rc == 1


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
