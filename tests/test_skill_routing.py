"""Tests for run_routed_skill() and the _AgentSession runner in skill.py."""

import json
from unittest import mock

import pytest

from agentic_ci.harness import create_harness
from agentic_ci.routing import ModelTier, RouteDecision, route_path
from agentic_ci.sandbox_profile import Resources, SandboxProfile
from agentic_ci.skill import (
    RoutedSkillResult,
    SkillConfig,
    _AgentSession,
    _default_run_container,
    run_routed_skill,
    run_skill,
)

TRACE_ID = "0123456789abcdef0123456789abcdef"
SPAN_ID = "fedcba9876543210"


class FakeSession:
    """Stands in for _AgentSession: records every run() and writes agent outputs.

    ``difficulty`` controls what the classifier run writes to route.json
    (``None`` writes nothing). ``classifier_rc`` / ``main_rcs`` control exit
    codes; ``main_rcs`` is consumed one value per main run across sessions.
    """

    instances: list = []
    difficulty = "low"
    classifier_rc = 0
    main_rcs: list = []
    write_verdict = True

    def __init__(self, work_dir, **kwargs):
        self.work_dir = work_dir
        self.kwargs = kwargs
        self.run_dir = work_dir / "_run"
        self.harness = create_harness(kwargs.get("harness_name", "claude-code"))
        self.default_model = "default-model"
        self.trace_id = TRACE_ID
        self.span_id = SPAN_ID
        self.runs = []
        self.completion_files = []
        self.entered = 0
        self.exited = 0
        FakeSession.instances.append(self)

    def __enter__(self):
        self.entered += 1
        self.run_dir.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return False

    def run(
        self, prompt, *, model, effort=None, extra_args=None, output_file=None, completion_file=None
    ):
        self.runs.append(
            {
                "prompt": prompt,
                "model": model,
                "effort": effort,
                "extra_args": extra_args,
                "output_file": output_file,
            }
        )
        self.completion_files.append(completion_file)
        if "TASK TO RATE" in prompt:
            if FakeSession.difficulty is not None:
                route_path(self.work_dir).write_text(
                    json.dumps({"difficulty": FakeSession.difficulty, "reason": "r"})
                )
            return FakeSession.classifier_rc
        if FakeSession.write_verdict:
            (self.work_dir / "verdict.json").write_text('{"verdict": "committed"}')
        return FakeSession.main_rcs.pop(0) if FakeSession.main_rcs else 0


@pytest.fixture(autouse=True)
def _reset_fake_session():
    FakeSession.instances = []
    FakeSession.difficulty = "low"
    FakeSession.classifier_rc = 0
    FakeSession.main_rcs = []
    FakeSession.write_verdict = True
    with mock.patch("agentic_ci.skill._AgentSession", FakeSession):
        yield


def _config(**overrides):
    kwargs = {
        "skill_name": "test-skill",
        "verdict_loader": lambda wd: json.loads((wd / "verdict.json").read_text()),
    }
    kwargs.update(overrides)
    return SkillConfig(**kwargs)


def _run(config, tmp_path, **kwargs):
    return run_routed_skill(
        config, ticket_key="TEST-1", work_dir=tmp_path, config_dir=tmp_path, **kwargs
    )


def _all_runs():
    return [run for session in FakeSession.instances for run in session.runs]


def _main_runs():
    return [run for run in _all_runs() if "TASK TO RATE" not in run["prompt"]]


def _classifier_runs():
    return [run for run in _all_runs() if "TASK TO RATE" in run["prompt"]]


def _routed_events(tmp_path):
    log_file = tmp_path / "_run" / "claude-otel.jsonl"
    if not log_file.exists():
        return []
    spans = []
    for line in log_file.read_text().splitlines():
        record = json.loads(line)
        for rs in record["payload"]["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                spans.extend(s for s in ss["spans"] if s["name"] == "skill.routed")
    return spans


class TestRunRoutedSkill:
    def test_classifier_then_main_run_in_one_session(self, tmp_path):
        FakeSession.difficulty = "medium"
        result = _run(_config(), tmp_path)

        assert isinstance(result, RoutedSkillResult)
        assert result.rc == 0
        assert len(FakeSession.instances) == 1
        session = FakeSession.instances[0]
        assert (session.entered, session.exited) == (1, 1)
        classifier, main = session.runs
        assert classifier["model"] == "default-model"
        assert classifier["effort"] == "low"
        assert classifier["extra_args"] == ["--max-turns", "10"]
        assert classifier["output_file"] == tmp_path / "_run" / "classifier-output.txt"
        assert main["model"] == "claude-sonnet-4-5"
        assert main["effort"] == "high"
        assert main["extra_args"] is None
        assert main["output_file"] == tmp_path / "agent-output.txt"
        # The classifier completes on its route file; the skill run keeps the verdict.
        assert session.completion_files == [tmp_path / "_run" / "route.json", None]
        assert result.route == RouteDecision(
            model="claude-sonnet-4-5", effort="high", tier="medium", source="classifier", reason="r"
        )

    def test_config_model_tiers_override(self, tmp_path):
        FakeSession.difficulty = "low"
        config = _config(model_tiers={"low": ModelTier("custom-small", "xhigh")})
        result = _run(config, tmp_path)
        assert _main_runs()[0]["model"] == "custom-small"
        assert _main_runs()[0]["effort"] == "xhigh"
        assert result.route.tier == "low"

    def test_classifier_max_turns_forwarded(self, tmp_path):
        _run(_config(), tmp_path, classifier_max_turns=3)
        assert _classifier_runs()[0]["extra_args"] == ["--max-turns", "3"]

    def test_custom_classifier_prompt_builder(self, tmp_path):
        _run(_config(), tmp_path, classifier_prompt_builder=lambda t: f"TASK TO RATE: {t}")
        assert _classifier_runs()[0]["prompt"].startswith("TASK TO RATE: ")

    def test_fallback_on_classifier_failure(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_REASONING_EFFORT", raising=False)
        FakeSession.classifier_rc = 1
        result = _run(_config(), tmp_path)
        assert result.rc == 0
        main = _main_runs()[0]
        assert main["model"] == "default-model"
        # fallback carries the registry default effort, same as an unrouted run
        assert main["effort"] == "high"
        assert result.route.source == "fallback"
        assert result.route.tier is None
        assert result.route.effort == "high"

    def test_fallback_on_missing_route_file(self, tmp_path):
        FakeSession.difficulty = None
        result = _run(_config(), tmp_path)
        assert result.route.source == "fallback"
        assert _main_runs()[0]["model"] == "default-model"

    def test_transient_retry_reuses_decision(self, tmp_path):
        FakeSession.difficulty = "high"
        FakeSession.main_rcs = [137, 0]
        result = _run(_config(), tmp_path)
        assert result.rc == 0
        assert len(FakeSession.instances) == 2
        assert len(_classifier_runs()) == 1
        assert [run["model"] for run in _main_runs()] == ["claude-opus-4-6", "claude-opus-4-6"]
        assert result.route.tier == "high"

    def test_verdict_missing_rerun_reuses_decision(self, tmp_path):
        FakeSession.difficulty = "medium"
        FakeSession.write_verdict = False
        result = _run(_config(), tmp_path)
        assert result.rc == 1
        assert len(_classifier_runs()) == 1
        assert len(_main_runs()) == 2
        assert all(run["model"] == "claude-sonnet-4-5" for run in _main_runs())

    def test_force_tier_skips_classifier(self, tmp_path):
        result = _run(_config(), tmp_path, force_tier="high")
        assert _classifier_runs() == []
        assert _main_runs()[0]["model"] == "claude-opus-4-6"
        assert result.route.source == "forced"

    def test_unknown_force_tier_raises_before_session(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown force_tier"):
            _run(_config(), tmp_path, force_tier="ultra")
        assert FakeSession.instances == []

    def test_invalid_model_tiers_raise_before_session(self, tmp_path):
        config = _config(model_tiers={"high": ModelTier("claude-opus-4-6", "turbo")})
        with pytest.raises(ValueError, match="Unsupported Claude Code effort"):
            _run(config, tmp_path)
        assert FakeSession.instances == []

    def test_custom_container_runner_rejected(self, tmp_path):
        config = _config(container_runner=lambda *a, **k: 0)
        with pytest.raises(ValueError, match="default container runner"):
            _run(config, tmp_path)

    def test_dry_run_has_no_route(self, tmp_path):
        verdict = tmp_path / "given.json"
        verdict.write_text('{"verdict": "committed"}')
        result = _run(_config(), tmp_path, dry_run=True, dry_run_verdict_path=verdict)
        assert result == RoutedSkillResult(rc=0, route=None)
        assert FakeSession.instances == []

    def test_pre_gate_block_has_no_route(self, tmp_path):
        config = _config(pre_gates=[lambda **kw: "blocked"])
        result = _run(config, tmp_path)
        assert result == RoutedSkillResult(rc=0, route=None)
        assert FakeSession.instances == []

    def test_verdict_path_and_harness_forwarded_to_runner(self, tmp_path):
        config = _config(
            harness_name="codex",
            backend_name="local",
            verdict_path_fn=lambda wd: wd / "verdict.json",
        )
        FakeSession.difficulty = "low"
        _run(config, tmp_path)
        session = FakeSession.instances[0]
        assert session.kwargs["verdict_path"] == tmp_path / "verdict.json"
        assert session.kwargs["harness_name"] == "codex"
        assert session.kwargs["backend_name"] == "local"
        assert _main_runs()[0]["model"] == "gpt-6-luna"
        assert _classifier_runs()[0]["extra_args"] is None

    def test_routed_event_written_under_run_root(self, tmp_path):
        FakeSession.difficulty = "medium"
        _run(_config(), tmp_path)
        spans = _routed_events(tmp_path)
        assert len(spans) == 1
        span = spans[0]
        assert span["traceId"] == TRACE_ID
        assert span["parentSpanId"] == SPAN_ID
        payload = json.loads(span["events"][0]["attributes"][0]["value"]["stringValue"])
        assert payload == {
            "event_type": "skill.routed",
            "skill_name": "test-skill",
            "ticket_key": "TEST-1",
            "harness": "claude-code",
            "backend": "podman",
            "default_model": "default-model",
            "tier": "medium",
            "model": "claude-sonnet-4-5",
            "effort": "high",
            "source": "classifier",
        }

    def test_event_export_failure_does_not_change_rc(self, tmp_path):
        with mock.patch("agentic_ci.skill.emit_event", side_effect=RuntimeError("no disk")):
            result = _run(_config(), tmp_path)
        assert result.rc == 0
        assert result.route.source == "classifier"

    def test_route_file_visible_after_run(self, tmp_path):
        FakeSession.difficulty = "low"
        _run(_config(), tmp_path)
        assert json.loads(route_path(tmp_path).read_text())["difficulty"] == "low"


class TestDefaultRunContainer:
    def test_without_router_runs_once_on_default_model(self, tmp_path):
        rc = _default_run_container(tmp_path, "prompt", tmp_path / "out.txt")
        assert rc == 0
        session = FakeSession.instances[0]
        assert session.runs == [
            {
                "prompt": "prompt",
                "model": "default-model",
                "effort": None,
                "extra_args": None,
                "output_file": tmp_path / "out.txt",
            }
        ]

    def test_explicit_model_and_effort(self, tmp_path):
        _default_run_container(tmp_path, "prompt", None, model="m", effort="high")
        assert FakeSession.instances[0].runs[0]["model"] == "m"
        assert FakeSession.instances[0].runs[0]["effort"] == "high"


class RecordingBackend:
    collector_bind_address = "127.0.0.1"

    def __init__(self):
        self.calls = []
        self.efforts = []
        self.output_file = None
        self.verdict_path = None

    def setup(self, otel_port=None):
        self.calls.append(("setup", otel_port))

    def run(self, prompt, model, otel_port=None, traceparent=None, extra_args=None, **kw):
        self.calls.append(("run", prompt, model, extra_args, self.output_file))
        self.efforts.append(kw.get("effort"))
        return 0

    def stop(self):
        self.calls.append(("stop",))


class TestAgentSession:
    """Exercises the real _AgentSession (imported above, before the autouse patch)."""

    def test_one_setup_many_runs_one_stop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODEL", "env-model")
        monkeypatch.delenv("CLAUDE_REASONING_EFFORT", raising=False)
        backend = RecordingBackend()
        harness = create_harness("claude-code")
        with (
            mock.patch("agentic_ci.skill.create_backend", return_value=backend),
            mock.patch("agentic_ci.skill.create_harness", return_value=harness),
            mock.patch.object(
                type(harness), "supports_otel", new_callable=mock.PropertyMock
            ) as otel,
        ):
            otel.return_value = False
            with _AgentSession(tmp_path, verdict_path=tmp_path / "v.json") as session:
                assert session.default_model == "env-model"
                session.run("first", model="a", effort="low", output_file=tmp_path / "1.txt")
                session.run("second", model="b", extra_args=["--x"], output_file=tmp_path / "2.txt")

        assert backend.verdict_path == tmp_path / "v.json"
        assert backend.calls == [
            ("setup", None),
            ("run", "first", "a", ["--effort", "low"], tmp_path / "1.txt"),
            # effort=None resolves to the registry default for regular runs
            ("run", "second", "b", ["--effort", "high", "--x"], tmp_path / "2.txt"),
            ("stop",),
        ]
        # The resolved effort reaches the backend, which exports it to the agent.
        assert backend.efforts == ["low", "high"]
        assert (tmp_path / "_run").is_dir()
        assert session.last_model == "b"
        assert session.last_effort == "high"
        assert session.last_subagent_effort is None
        assert session.last_rc == 0

    def test_completion_file_overrides_verdict_path_for_one_run(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_REASONING_EFFORT", raising=False)
        backend = RecordingBackend()
        seen = []
        original_run = backend.run

        def run(*args, **kwargs):
            seen.append(backend.verdict_path)
            return original_run(*args, **kwargs)

        backend.run = run
        harness = create_harness("claude-code")
        route = tmp_path / "_run" / "route.json"
        with (
            mock.patch("agentic_ci.skill.create_backend", return_value=backend),
            mock.patch("agentic_ci.skill.create_harness", return_value=harness),
            mock.patch.object(
                type(harness), "supports_otel", new_callable=mock.PropertyMock
            ) as otel,
        ):
            otel.return_value = False
            with _AgentSession(tmp_path, verdict_path=tmp_path / "v.json") as session:
                session.run("classify", model="a", completion_file=route)
                session.run("skill", model="b")

        assert seen == [route, tmp_path / "v.json"]
        assert backend.verdict_path == tmp_path / "v.json"

    def test_env_effort_override_and_root_span_attributes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEX_REASONING_EFFORT", "medium")
        monkeypatch.setenv("CODEX_SUBAGENT_REASONING_EFFORT", "low")
        backend = RecordingBackend()
        harness = create_harness("codex")
        with (
            mock.patch("agentic_ci.skill.create_backend", return_value=backend),
            mock.patch("agentic_ci.skill.create_harness", return_value=harness),
            mock.patch(
                "agentic_ci.skill.start_collector",
                return_value=(object(), 4318, tmp_path / "_run" / "claude-otel.jsonl", None),
            ),
            mock.patch("agentic_ci.skill.stop_collector"),
            mock.patch("agentic_ci.skill.inject_root_spans", return_value=1) as inject,
        ):
            with _AgentSession(tmp_path, harness_name="codex") as session:
                session.run("p", model="gpt-6-sol")

        assert backend.calls[1][3] == [
            "-c",
            "model_reasoning_effort=medium",
            "-c",
            "agents.default_subagent_reasoning_effort=low",
        ]
        attributes = inject.call_args.kwargs["attributes"]
        assert attributes["agent.model"] == "gpt-6-sol"
        assert attributes["agent.reasoning_effort"] == "medium"
        assert attributes["agent.subagent_reasoning_effort"] == "low"

    def test_setup_failure_releases_collector_and_backend(self, tmp_path):
        backend = RecordingBackend()
        backend.setup = mock.Mock(side_effect=RuntimeError("image pull failed"))
        harness = create_harness("claude-code")
        fake_proc = object()
        with (
            mock.patch("agentic_ci.skill.create_backend", return_value=backend),
            mock.patch("agentic_ci.skill.create_harness", return_value=harness),
            mock.patch(
                "agentic_ci.skill.start_collector",
                return_value=(fake_proc, 4318, tmp_path / "_run" / "claude-otel.jsonl", None),
            ),
            mock.patch("agentic_ci.skill.stop_collector") as stop_collector,
            pytest.raises(RuntimeError, match="image pull failed"),
        ):
            with _AgentSession(tmp_path):
                pass

        stop_collector.assert_called_once_with(fake_proc)
        assert backend.calls == [("stop",)]


PROFILE = SandboxProfile(resources=Resources(memory="8Gi"), env={"CGO_ENABLED": "0"})


class TestSandboxProfilePlumbing:
    """SkillConfig.sandbox_profile reaches create_backend, and only when set."""

    def _session_create_backend_kwargs(self, tmp_path, **session_kwargs):
        harness = create_harness("claude-code")
        with (
            mock.patch("agentic_ci.skill.create_backend", return_value=RecordingBackend()) as cb,
            mock.patch("agentic_ci.skill.create_harness", return_value=harness),
        ):
            _AgentSession(tmp_path, container_env={"FOO": "bar"}, **session_kwargs)
        return cb.call_args, harness

    def test_session_without_profile_calls_create_backend_as_today(self, tmp_path):
        call, harness = self._session_create_backend_kwargs(tmp_path)
        assert call.args == ("podman",)
        assert call.kwargs == {
            "harness": harness,
            "workdir": str(tmp_path),
            "image": None,
            "extra_env": {"FOO": "bar"},
        }

    def test_session_passes_profile_to_create_backend(self, tmp_path):
        call, _ = self._session_create_backend_kwargs(tmp_path, sandbox_profile=PROFILE)
        assert call.kwargs["sandbox_profile"] is PROFILE
        # The profile is never folded into the container env.
        assert call.kwargs["extra_env"] == {"FOO": "bar"}

    def test_default_runner_passes_profile_to_session(self, tmp_path):
        _default_run_container(tmp_path, "p", tmp_path / "out.txt", sandbox_profile=PROFILE)
        assert FakeSession.instances[0].kwargs["sandbox_profile"] is PROFILE

    def test_default_runner_without_profile_omits_it(self, tmp_path):
        _default_run_container(tmp_path, "p", tmp_path / "out.txt")
        assert "sandbox_profile" not in FakeSession.instances[0].kwargs

    def test_run_skill_default_runner_carries_profile(self, tmp_path):
        run_skill(
            _config(sandbox_profile=PROFILE),
            ticket_key="TEST-1",
            work_dir=tmp_path,
            config_dir=tmp_path,
        )
        assert FakeSession.instances[0].kwargs["sandbox_profile"] is PROFILE

    def test_routed_skill_carries_profile(self, tmp_path):
        _run(_config(sandbox_profile=PROFILE, backend_name="openshell"), tmp_path)
        session = FakeSession.instances[0]
        assert session.kwargs["sandbox_profile"] is PROFILE
        assert session.kwargs["backend_name"] == "openshell"

    def test_routed_skill_without_profile_omits_it(self, tmp_path):
        _run(_config(), tmp_path)
        assert "sandbox_profile" not in FakeSession.instances[0].kwargs

    def test_custom_runner_gets_profile_only_when_set(self, tmp_path):
        seen = []

        def runner(work_dir, prompt, output_file, **kwargs):
            seen.append(kwargs)
            (work_dir / "verdict.json").write_text('{"verdict": "committed"}')
            return 0

        for profile in (None, PROFILE):
            run_skill(
                _config(container_runner=runner, sandbox_profile=profile),
                ticket_key="TEST-1",
                work_dir=tmp_path,
                config_dir=tmp_path,
            )
        assert seen[0] == {"image": None}
        assert seen[1] == {"image": None, "sandbox_profile": PROFILE}

    def test_skill_config_defaults_to_no_profile(self):
        assert SkillConfig(skill_name="s").sandbox_profile is None
