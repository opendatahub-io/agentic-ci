"""Tests for Podman backend."""

import json
import os
import subprocess as _subprocess
from unittest import mock

import pytest

from agentic_ci import plugins
from agentic_ci.backends.podman import PodmanBackend
from agentic_ci.harness import ClaudeCodeHarness, CodexHarness, OpenCodeHarness


@pytest.fixture()
def claude_harness():
    return ClaudeCodeHarness()


@pytest.fixture()
def opencode_harness():
    return OpenCodeHarness()


def test_build_env_args_claude_code(tmp_path, claude_harness):
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    args = backend._build_env_args()
    assert "--env" in args
    assert "CLAUDE_CODE_USE_VERTEX=1" in args
    assert "DISABLE_AUTOUPDATER=1" in args


def test_build_env_args_opencode(tmp_path, opencode_harness, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.setenv("VERTEX_LOCATION", "us-east1")
    backend = PodmanBackend(workdir=str(tmp_path), harness=opencode_harness)
    args = backend._build_env_args()
    assert "GOOGLE_CLOUD_PROJECT=my-proj" in args
    assert "VERTEX_LOCATION=us-east1" in args
    assert "OPENCODE_DISABLE_AUTOUPDATE=1" in args
    assert "CLAUDE_CODE_USE_VERTEX=1" not in args


def test_build_env_args_extra_env(tmp_path, claude_harness):
    backend = PodmanBackend(
        workdir=str(tmp_path), harness=claude_harness, extra_env={"MY_VAR": "value"}
    )
    args = backend._build_env_args()
    assert "MY_VAR=value" in args


def test_build_env_args_skips_extra_env_effort(tmp_path, claude_harness):
    """run() exports the effort per exec; a container-level copy would outlive it."""
    backend = PodmanBackend(
        workdir=str(tmp_path),
        harness=claude_harness,
        extra_env={"AGENT_REASONING_EFFORT": "max", "FOO": "bar"},
    )
    args = backend._build_env_args()
    assert "FOO=bar" in args
    assert not any(arg.startswith("AGENT_REASONING_EFFORT") for arg in args)


def test_build_env_args_does_not_forward_openai_credentials_to_claude(tmp_path, claude_harness):
    backend = PodmanBackend(
        workdir=str(tmp_path),
        harness=claude_harness,
        extra_env={"OPENAI_API_KEY": "unrelated-key"},
    )

    args = backend._build_env_args()

    assert not any("OPENAI_API_KEY" in arg for arg in args)
    assert not any("unrelated-key" in arg for arg in args)


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
def test_build_env_args_passes_anthropic_extra_env_by_reference(tmp_path, claude_harness, key):
    backend = PodmanBackend(
        workdir=str(tmp_path),
        harness=claude_harness,
        extra_env={key: "secret-value"},
    )

    # The harness also forwards the selected credential by reference; no duplicate.
    args = backend._build_env_args({key: "secret-value"})

    assert args.count(key) == 1
    assert not any("secret-value" in arg for arg in args)


def test_build_env_args_forwards_anthropic_extra_env_the_harness_skips(tmp_path, opencode_harness):
    backend = PodmanBackend(
        workdir=str(tmp_path),
        harness=opencode_harness,
        extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "secret-value"},
    )

    args = backend._build_env_args({"CLAUDE_CODE_OAUTH_TOKEN": "secret-value"})

    assert args.count("CLAUDE_CODE_OAUTH_TOKEN") == 1
    assert not any("secret-value" in arg for arg in args)


def test_resolve_credentials_creates_config(monkeypatch, tmp_path, claude_harness):
    creds = json.dumps({"type": "authorized_user", "client_id": "test"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-project")

    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    backend._resolve_credentials()

    assert backend._config_dir is not None
    adc_path = os.path.join(
        backend._config_dir, ".config", "gcloud", "application_default_credentials.json"
    )
    assert os.path.isfile(adc_path)
    with open(adc_path) as f:
        assert json.loads(f.read())["client_id"] == "test"

    config_path = os.path.join(
        backend._config_dir, ".config", "gcloud", "configurations", "config_default"
    )
    assert os.path.isfile(config_path)
    with open(config_path) as f:
        content = f.read()
    assert "my-project" in content


def test_resolve_image_claude_code(monkeypatch, tmp_path, claude_harness):
    monkeypatch.setenv("CLAUDE_CONTAINER_IMAGE", "my-claude-image:latest")
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    backend._resolve_image()
    assert backend.image == "my-claude-image:latest"


def test_resolve_image_opencode(monkeypatch, tmp_path, opencode_harness):
    monkeypatch.setenv("OPENCODE_CONTAINER_IMAGE", "my-opencode-image:latest")
    backend = PodmanBackend(workdir=str(tmp_path), harness=opencode_harness)
    backend._resolve_image()
    assert backend.image == "my-opencode-image:latest"


def test_resolve_image_raises_with_correct_env_var(monkeypatch, tmp_path, opencode_harness):
    monkeypatch.delenv("OPENCODE_CONTAINER_IMAGE", raising=False)
    backend = PodmanBackend(workdir=str(tmp_path), harness=opencode_harness)
    with pytest.raises(RuntimeError, match="OPENCODE_CONTAINER_IMAGE"):
        backend._resolve_image()


def test_setup_codex_credentials_fail_fast(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/codex:test",
        harness=CodexHarness(),
    )

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        backend.setup()


def test_build_vol_args_claude_mount_target(monkeypatch, tmp_path, claude_harness):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds = json.dumps({"type": "authorized_user"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    backend._resolve_credentials()
    vol_args = backend._build_vol_args()
    mount_str = " ".join(vol_args)
    assert "/home/agent-ci/.config/gcloud/" in mount_str


def test_build_vol_args_opencode_mount_target(monkeypatch, tmp_path, opencode_harness):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    creds = json.dumps({"type": "authorized_user"})
    monkeypatch.setenv("GCLOUD_CREDENTIALS", creds)
    backend = PodmanBackend(workdir=str(tmp_path), harness=opencode_harness)
    backend._resolve_credentials()
    vol_args = backend._build_vol_args()
    mount_str = " ".join(vol_args)
    assert "/home/agent-ci/.config/gcloud/" in mount_str


def test_build_vol_args_api_key_no_gcloud_mounts(monkeypatch, tmp_path, claude_harness):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    vol_args = backend._build_vol_args()
    mount_str = " ".join(vol_args)
    assert "/workspace" in mount_str
    assert ".config/gcloud" not in mount_str


def test_build_env_args_api_key(monkeypatch, tmp_path, claude_harness):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    args = backend._build_env_args()
    assert "ANTHROPIC_API_KEY" in args
    assert "ANTHROPIC_API_KEY=sk-test-key" not in args
    assert "CLAUDE_CODE_USE_VERTEX=1" not in args


def test_setup_oauth_token_skips_gcloud_credentials(monkeypatch, tmp_path, claude_harness):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/test:latest",
        harness=claude_harness,
    )

    calls = []

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["podman", "container", "inspect"]:
            return _subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return _subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(_subprocess, "run", mock_run)

    with mock.patch.object(backend, "_resolve_credentials") as resolve_credentials:
        backend.setup()

    resolve_credentials.assert_not_called()
    run_cmd = next(c for c in calls if c[:2] == ["podman", "run"])
    assert "CLAUDE_CODE_OAUTH_TOKEN" in run_cmd
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-test" not in run_cmd
    assert "CLAUDE_CODE_USE_VERTEX=1" not in run_cmd
    assert ".config/gcloud" not in " ".join(run_cmd)


def test_setup_does_not_override_entrypoint(monkeypatch, tmp_path, claude_harness):
    """setup() passes sleep as the command, not --entrypoint, so the image entrypoint runs."""
    import subprocess as _subprocess

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/test:latest",
        harness=claude_harness,
    )

    calls = []
    original_run = _subprocess.run

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["podman", "rm"]:
            return _subprocess.CompletedProcess(cmd, 0)
        if cmd[:2] == ["podman", "run"]:
            return _subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["podman", "container", "inspect"]:
            return _subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(_subprocess, "run", mock_run)

    backend.setup()

    run_calls = [c for c in calls if c[:2] == ["podman", "run"]]
    assert len(run_calls) == 1
    run_cmd = run_calls[0]
    assert "--entrypoint" not in run_cmd
    image_idx = run_cmd.index("localhost/test:latest")
    assert run_cmd[image_idx + 1 : image_idx + 4] == ["bash", "-c", "sleep 1200"]


def test_container_name_is_unique(tmp_path, claude_harness):
    """Each PodmanBackend instance must get a unique container name."""
    b1 = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    b2 = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    assert b1._container_name != b2._container_name


def test_container_name_has_prefix(tmp_path, claude_harness):
    """Container name must start with 'agentic-ci-' for easy identification."""
    backend = PodmanBackend(workdir=str(tmp_path), harness=claude_harness)
    assert backend._container_name.startswith("agentic-ci-")
    # The suffix is a full 32-char hex string (uuid4)
    suffix = backend._container_name[len("agentic-ci-") :]
    assert len(suffix) == 32
    int(suffix, 16)  # raises ValueError if not valid hex


def test_setup_uses_instance_container_name(monkeypatch, tmp_path, claude_harness):
    """setup() must use the instance's unique container name, not a global constant."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/test:latest",
        harness=claude_harness,
    )

    calls = []
    original_run = _subprocess.run

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["podman", "rm"]:
            return _subprocess.CompletedProcess(cmd, 0)
        if cmd[:2] == ["podman", "run"]:
            return _subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["podman", "container", "inspect"]:
            return _subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(_subprocess, "run", mock_run)

    backend.setup()

    # rm call should use instance name
    rm_calls = [c for c in calls if c[:2] == ["podman", "rm"]]
    assert len(rm_calls) == 1
    assert rm_calls[0][3] == backend._container_name

    # run call should use instance name
    run_calls = [c for c in calls if c[:2] == ["podman", "run"]]
    assert len(run_calls) == 1
    name_idx = run_calls[0].index("--name")
    assert run_calls[0][name_idx + 1] == backend._container_name


def test_stop_uses_instance_container_name(monkeypatch, tmp_path, claude_harness):
    """stop() must use the instance's unique container name."""
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/test:latest",
        harness=claude_harness,
    )

    calls = []

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        return _subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(_subprocess, "run", mock_run)

    backend.stop()

    assert len(calls) == 1
    assert calls[0] == ["podman", "rm", "-f", backend._container_name]


def test_is_local_image_localhost(tmp_path, claude_harness):
    """localhost/ images are always considered local."""
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/myimage:latest",
        harness=claude_harness,
    )
    assert backend._is_local_image() is True


def test_is_local_image_cached_remote(monkeypatch, tmp_path, claude_harness):
    """Non-localhost images that exist locally should be treated as local."""
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="ghcr.io/org/image:latest",
        harness=claude_harness,
    )

    def mock_run(cmd, **kwargs):
        if cmd[:3] == ["podman", "image", "exists"]:
            return _subprocess.CompletedProcess(cmd, 0)
        return _subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(_subprocess, "run", mock_run)

    assert backend._is_local_image() is True


def test_is_local_image_missing_remote(monkeypatch, tmp_path, claude_harness):
    """Non-localhost images not present locally should not be treated as local."""
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="ghcr.io/org/image:latest",
        harness=claude_harness,
    )

    def mock_run(cmd, **kwargs):
        if cmd[:3] == ["podman", "image", "exists"]:
            return _subprocess.CompletedProcess(cmd, 1)
        return _subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(_subprocess, "run", mock_run)

    assert backend._is_local_image() is False


def test_run_passes_otel_port_to_implicit_setup(tmp_path, claude_harness):
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/test:latest",
        harness=claude_harness,
    )

    with (
        mock.patch.object(backend, "is_running", return_value=False),
        mock.patch.object(backend, "setup") as setup,
        mock.patch.object(backend, "_process_stream", return_value=(0, True)),
        mock.patch.object(backend, "_wait_for_otel_flush"),
        mock.patch("agentic_ci.backends.podman.subprocess.Popen"),
        mock.patch(
            "agentic_ci.backends.podman.subprocess.run",
            return_value=_subprocess.CompletedProcess([], 0),
        ),
    ):
        assert backend.run("test", "test-model", otel_port=4318) == 0

    setup.assert_called_once_with(otel_port=4318)


@pytest.mark.parametrize("effort", ["medium", None])
def test_run_exports_model_and_effort(tmp_path, claude_harness, effort):
    backend = PodmanBackend(
        workdir=str(tmp_path),
        image="localhost/test:latest",
        harness=claude_harness,
    )

    with (
        mock.patch.object(backend, "is_running", return_value=True),
        mock.patch.object(backend, "_process_stream", return_value=(0, True)),
        mock.patch.object(backend, "_wait_for_otel_flush"),
        mock.patch("agentic_ci.backends.podman.subprocess.Popen") as popen,
        mock.patch(
            "agentic_ci.backends.podman.subprocess.run",
            return_value=_subprocess.CompletedProcess([], 0),
        ),
    ):
        backend.run("test", "test-model", effort=effort)

    cmd = popen.call_args.args[0]
    assert cmd[cmd.index("AGENT_MODEL=test-model") - 1] == "--env"
    if effort is None:
        assert not any(arg.startswith("AGENT_REASONING_EFFORT=") for arg in cmd)
    else:
        assert cmd[cmd.index(f"AGENT_REASONING_EFFORT={effort}") - 1] == "--env"


# Where the Codex runner image installs autofix-triage.
_SKILL_DIR = (
    "/home/agent-ci/.codex/plugins/cache/opendatahub-skills/autofix-skills/0.1.0/skills/"
    "autofix-triage"
)


def _run_with_skill_lookup(backend, lookup):
    """Run *backend*, answering ``agentic-ci skill-dir`` with *lookup*; return (exec cmd, runs)."""
    runs = []

    def fake_run(cmd, *args, **kwargs):
        runs.append(cmd)
        if "skill-dir" in cmd:
            return lookup(cmd)
        return _subprocess.CompletedProcess(cmd, 0)

    with (
        mock.patch.object(backend, "is_running", return_value=True),
        mock.patch.object(backend, "_process_stream", return_value=(0, True)),
        mock.patch.object(backend, "_wait_for_otel_flush"),
        mock.patch("agentic_ci.backends.podman.subprocess.Popen") as popen,
        mock.patch("agentic_ci.backends.podman.subprocess.run", side_effect=fake_run),
    ):
        backend.run("test", "test-model")
    return popen.call_args.args[0], [cmd for cmd in runs if "skill-dir" in cmd]


def _found(stdout):
    return lambda cmd: _subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


def _codex_backend(tmp_path, **kwargs):
    backend = PodmanBackend(
        workdir=str(tmp_path), image="localhost/test:latest", harness=CodexHarness(), **kwargs
    )
    backend.skill_name = "autofix-triage"
    return backend


def test_run_exports_skill_dir_found_in_container(tmp_path):
    backend = _codex_backend(tmp_path)
    cmd, lookups = _run_with_skill_lookup(backend, _found(_SKILL_DIR + "\n"))
    assert lookups == [
        ["podman", "exec", backend._container_name, "agentic-ci", "skill-dir", "autofix-triage"]
    ]
    assert cmd[cmd.index(f"CLAUDE_SKILL_DIR={_SKILL_DIR}") - 1] == "--env"
    # The env goes to podman exec, before the container name.
    assert cmd.index(f"CLAUDE_SKILL_DIR={_SKILL_DIR}") < cmd.index(backend._container_name)


def test_skill_dir_lookup_outlasts_its_codex_query(tmp_path):
    # The host must not give up on podman exec while the codex plugin list
    # inside the container may still be running (and then keep running).
    timeouts = []
    with mock.patch("agentic_ci.backends.podman.subprocess.run") as run:
        run.side_effect = lambda cmd, **kwargs: (
            timeouts.append(kwargs["timeout"]) or _found(_SKILL_DIR + "\n")(cmd)
        )
        args = _codex_backend(tmp_path)._skill_dir_env_args()
    assert args == ["--env", f"CLAUDE_SKILL_DIR={_SKILL_DIR}"]
    assert timeouts and timeouts[0] > plugins._CODEX_JSON_TIMEOUT


@pytest.mark.parametrize(
    "lookup",
    [
        lambda cmd: _subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found"),
        # An image whose agentic-ci predates skill-dir.
        lambda cmd: _subprocess.CompletedProcess(cmd, 2, stdout="", stderr="invalid choice"),
        _found("relative/dir\n"),
        _found("/one\n/two\n"),
        mock.Mock(side_effect=_subprocess.TimeoutExpired("podman", 60)),
        mock.Mock(side_effect=OSError("no podman")),
    ],
)
def test_run_without_a_usable_skill_dir_exports_nothing(tmp_path, lookup):
    cmd, lookups = _run_with_skill_lookup(_codex_backend(tmp_path), lookup)
    assert len(lookups) == 1
    assert not any(arg.startswith("CLAUDE_SKILL_DIR") for arg in cmd)


def test_run_skips_skill_dir_for_claude_code(tmp_path, claude_harness):
    backend = PodmanBackend(workdir=str(tmp_path), image="img", harness=claude_harness)
    backend.skill_name = "autofix-triage"
    cmd, lookups = _run_with_skill_lookup(backend, _found(_SKILL_DIR + "\n"))
    assert lookups == []
    assert not any(arg.startswith("CLAUDE_SKILL_DIR") for arg in cmd)


def test_run_skips_skill_dir_without_a_skill_name(tmp_path):
    backend = _codex_backend(tmp_path)
    backend.skill_name = None
    cmd, lookups = _run_with_skill_lookup(backend, _found(_SKILL_DIR + "\n"))
    assert lookups == []
    assert not any(arg.startswith("CLAUDE_SKILL_DIR") for arg in cmd)


def test_run_keeps_caller_skill_dir(tmp_path):
    backend = _codex_backend(tmp_path, extra_env={"CLAUDE_SKILL_DIR": "/caller/dir"})
    cmd, lookups = _run_with_skill_lookup(backend, _found(_SKILL_DIR + "\n"))
    assert lookups == []
    assert not any(arg.startswith("CLAUDE_SKILL_DIR") for arg in cmd)
    # It reached the container when it started.
    assert "CLAUDE_SKILL_DIR=/caller/dir" in backend._build_env_args({})
