"""Tests for pipeline YAML generation."""

import pytest
import yaml

from agentic_ci.pipeline import distribute_slot, generate_child_pipeline, noop_pipeline


class TestDistributeSlot:
    def test_deterministic(self):
        s1 = distribute_slot("PROJ-1", 3)
        s2 = distribute_slot("PROJ-1", 3)
        assert s1 == s2

    def test_distributes_across_slots(self):
        slots = {distribute_slot(f"PROJ-{i}", 3) for i in range(100)}
        assert len(slots) == 3

    def test_custom_prefix(self):
        slot = distribute_slot("PROJ-1", 3, prefix="my-slot")
        assert slot.startswith("my-slot-")

    def test_zero_max_concurrency_raises(self):
        with pytest.raises(ValueError, match="must be a positive integer"):
            distribute_slot("PROJ-1", 0)

    def test_negative_max_concurrency_raises(self):
        with pytest.raises(ValueError, match="must be a positive integer"):
            distribute_slot("PROJ-1", -1)


class TestNoopPipeline:
    def test_contains_message(self):
        result = noop_pipeline("Nothing to do")
        assert "Nothing to do" in result
        assert "no-tickets:" in result

    def test_message_with_quotes(self):
        result = noop_pipeline('He said "hello"')
        parsed = yaml.safe_load(result)
        assert "no-tickets" in parsed

    def test_message_with_newline(self):
        result = noop_pipeline("line1\nline2")
        parsed = yaml.safe_load(result)
        assert "no-tickets" in parsed


class TestGenerateChildPipeline:
    def test_empty_items(self):
        result = generate_child_pipeline([], noop_message="Empty")
        assert "no-tickets:" in result
        assert "Empty" in result

    def test_generates_jobs(self):
        items = [{"key": "T-1"}, {"key": "T-2"}]

        def job_body(item, slot):
            return f"  extends: .default\n  resource_group: {slot}\n"

        result = generate_child_pipeline(
            items,
            job_body_fn=job_body,
        )
        assert "T-1:" in result
        assert "T-2:" in result
        assert "extends: .default" in result

    def test_default_job_yaml_prepended(self):
        items = [{"key": "T-1"}]

        result = generate_child_pipeline(
            items,
            job_body_fn=lambda item, slot: "  script: echo hi\n",
            default_job_yaml=".default-job:\n  image: alpine\n",
        )
        assert result.startswith(".default-job:")

    def test_custom_job_name(self):
        items = [{"key": "T-1"}]

        result = generate_child_pipeline(
            items,
            job_name_fn=lambda item: f"custom-{item['key']}",
            job_body_fn=lambda item, slot: "  script: echo hi\n",
        )
        assert "custom-T-1:" in result

    def test_job_body_fn_required(self):
        with pytest.raises(ValueError, match="job_body_fn is required"):
            generate_child_pipeline([{"key": "T-1"}])

    def test_bad_job_name_raises(self):
        items = [{"key": "T-1"}]
        with pytest.raises(ValueError, match="invalid characters"):
            generate_child_pipeline(
                items,
                job_name_fn=lambda item: "bad:name",
                job_body_fn=lambda item, slot: "  script: echo hi\n",
            )
