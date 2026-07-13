"""Tests for project configuration loading."""

from agentic_ci.config import Config, SetupStep, load_config


def test_no_config_file_returns_empty(tmp_path):
    config = load_config(str(tmp_path))
    assert config == Config()
    assert config.setup == []


def test_empty_config_file(tmp_path):
    config_dir = tmp_path / ".agentic-ci"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("")
    config = load_config(str(tmp_path))
    assert config.setup == []


def test_config_without_setup_key(tmp_path):
    config_dir = tmp_path / ".agentic-ci"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("other: true\n")
    config = load_config(str(tmp_path))
    assert config.setup == []


def test_setup_bare_strings(tmp_path):
    config_dir = tmp_path / ".agentic-ci"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("setup:\n  - npm ci\n  - npm run build\n")
    config = load_config(str(tmp_path))
    assert len(config.setup) == 2
    assert config.setup[0] == SetupStep(name="step-0", run="npm ci")
    assert config.setup[1] == SetupStep(name="step-1", run="npm run build")


def test_setup_object_form(tmp_path):
    config_dir = tmp_path / ".agentic-ci"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text(
        "setup:\n"
        "  - name: Install dependencies\n"
        "    run: npm ci\n"
        "  - name: Build\n"
        "    run: npm run build\n"
    )
    config = load_config(str(tmp_path))
    assert len(config.setup) == 2
    assert config.setup[0] == SetupStep(name="Install dependencies", run="npm ci")
    assert config.setup[1] == SetupStep(name="Build", run="npm run build")


def test_setup_object_without_name(tmp_path):
    config_dir = tmp_path / ".agentic-ci"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("setup:\n  - run: npm ci\n")
    config = load_config(str(tmp_path))
    assert len(config.setup) == 1
    assert config.setup[0].name == "step-0"
    assert config.setup[0].run == "npm ci"


def test_setup_mixed_forms(tmp_path):
    config_dir = tmp_path / ".agentic-ci"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text(
        "setup:\n  - npm ci\n  - name: Build\n    run: npm run build\n"
    )
    config = load_config(str(tmp_path))
    assert len(config.setup) == 2
    assert config.setup[0].run == "npm ci"
    assert config.setup[1] == SetupStep(name="Build", run="npm run build")


def test_setup_invalid_entry_skipped(tmp_path):
    config_dir = tmp_path / ".agentic-ci"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text(
        "setup:\n  - npm ci\n  - name: no-run-key\n  - run: valid\n"
    )
    config = load_config(str(tmp_path))
    assert len(config.setup) == 2
    assert config.setup[0].run == "npm ci"
    assert config.setup[1].run == "valid"


def test_setup_not_a_list(tmp_path):
    config_dir = tmp_path / ".agentic-ci"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("setup: npm ci\n")
    config = load_config(str(tmp_path))
    assert config.setup == []


def test_config_not_a_dict(tmp_path):
    config_dir = tmp_path / ".agentic-ci"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("- just a list\n")
    config = load_config(str(tmp_path))
    assert config.setup == []
