"""Tests for label_applier return code propagation in run_skill()."""

import logging

from agentic_ci.skill import SkillConfig, run_skill

# A secret-looking value planted in the verdict loader's exception text.
SECRET = "glpat-PLANTEDsecretTOKEN1234567890"


class TestLabelApplierReturnCode:
    def test_returns_zero_when_label_applier_returns_none(self, tmp_path):
        applier_calls = []

        def _applier(**kw):
            applier_calls.append(kw)
            return None

        config = SkillConfig(
            skill_name="test-skill",
            label_applier=_applier,
        )
        rc = run_skill(
            config,
            ticket_key="TEST-1",
            work_dir=tmp_path,
            config_dir=tmp_path,
            dry_run=True,
        )
        assert rc == 0
        assert len(applier_calls) == 1

    def test_returns_zero_when_label_applier_returns_zero(self, tmp_path):
        config = SkillConfig(
            skill_name="test-skill",
            label_applier=lambda **kw: 0,
        )
        rc = run_skill(
            config,
            ticket_key="TEST-1",
            work_dir=tmp_path,
            config_dir=tmp_path,
            dry_run=True,
        )
        assert rc == 0

    def test_propagates_nonzero_return_code(self, tmp_path):
        config = SkillConfig(
            skill_name="test-skill",
            label_applier=lambda **kw: 1,
        )
        rc = run_skill(
            config,
            ticket_key="TEST-1",
            work_dir=tmp_path,
            config_dir=tmp_path,
            dry_run=True,
        )
        assert rc == 1

    def test_propagates_custom_exit_code(self, tmp_path):
        config = SkillConfig(
            skill_name="test-skill",
            label_applier=lambda **kw: 42,
        )
        rc = run_skill(
            config,
            ticket_key="TEST-1",
            work_dir=tmp_path,
            config_dir=tmp_path,
            dry_run=True,
        )
        assert rc == 42

    def test_label_applier_receives_verdict(self, tmp_path):
        received = {}

        def _capture(**kw):
            received.update(kw)
            return 0

        config = SkillConfig(
            skill_name="test-skill",
            label_applier=_capture,
        )
        run_skill(
            config,
            ticket_key="TEST-1",
            work_dir=tmp_path,
            config_dir=tmp_path,
            dry_run=True,
        )
        assert received["ticket_key"] == "TEST-1"
        assert "verdict" in received

    def test_noop_label_applier_returns_zero(self, tmp_path):
        config = SkillConfig(skill_name="test-skill")
        rc = run_skill(
            config,
            ticket_key="TEST-1",
            work_dir=tmp_path,
            config_dir=tmp_path,
            dry_run=True,
        )
        assert rc == 0


class TestVerdictLoadErrorText:
    """gate_errors for a failed verdict load name only the exception class."""

    @staticmethod
    def _raise_loader(work_dir):
        raise ValueError(f"verdict.json has token {SECRET}")

    def test_dry_run_loader_error_text_stays_in_log(self, tmp_path, caplog):
        calls = []
        config = SkillConfig(
            skill_name="test-skill",
            verdict_loader=self._raise_loader,
            label_applier=lambda **kw: calls.append(kw),
        )
        with caplog.at_level(logging.ERROR, logger="agentic_ci.skill"):
            rc = run_skill(
                config,
                ticket_key="TEST-1",
                work_dir=tmp_path,
                config_dir=tmp_path,
                dry_run=True,
            )
        assert rc == 1
        assert len(calls) == 1
        assert calls[0]["verdict"] is None
        assert calls[0]["gate_errors"] == [
            "Verdict could not be loaded (ValueError); see the CI job log"
        ]
        assert SECRET in caplog.text

    def test_retry_loader_error_text_stays_in_log(self, tmp_path, caplog):
        calls = []
        runs = []

        def _runner(work_dir, prompt, output_file, **kw):
            runs.append(1)
            return 0

        config = SkillConfig(
            skill_name="test-skill",
            container_runner=_runner,
            verdict_loader=self._raise_loader,
            label_applier=lambda **kw: calls.append(kw),
        )
        with caplog.at_level(logging.ERROR, logger="agentic_ci.skill"):
            rc = run_skill(
                config,
                ticket_key="TEST-1",
                work_dir=tmp_path,
                config_dir=tmp_path,
                mode="resolve",
            )
        assert rc == 1
        assert len(runs) == 2
        assert len(calls) == 1
        assert calls[0]["gate_errors"] == [
            "Verdict could not be loaded (ValueError); see the CI job log"
        ]
        assert SECRET in caplog.text

    def test_retry_loader_returning_none(self, tmp_path):
        calls = []
        loads = []

        def _loader(work_dir):
            loads.append(1)
            if len(loads) == 1:
                raise FileNotFoundError("verdict.json")
            return None

        config = SkillConfig(
            skill_name="test-skill",
            container_runner=lambda work_dir, prompt, output_file, **kw: 0,
            verdict_loader=_loader,
            label_applier=lambda **kw: calls.append(kw),
        )
        rc = run_skill(
            config,
            ticket_key="TEST-1",
            work_dir=tmp_path,
            config_dir=tmp_path,
            mode="resolve",
        )
        assert rc == 1
        assert calls[0]["gate_errors"] == [
            "Verdict could not be loaded (loader returned no verdict); see the CI job log"
        ]
