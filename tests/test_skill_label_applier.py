"""Tests for label_applier return code propagation in run_skill()."""

from agentic_ci.skill import SkillConfig, run_skill


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
