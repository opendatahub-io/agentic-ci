"""Project-level configuration for agentic-ci.

Loads ``.agentic-ci/config.yml`` from the target repository's workdir.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import yaml

log = logging.getLogger(__name__)

REPO_CONFIG_PATH = ".agentic-ci/config.yml"


@dataclass
class SetupStep:
    """A single setup command to run before the agent starts."""

    name: str
    run: str


@dataclass
class Config:
    """Parsed project configuration."""

    setup: list[SetupStep] = field(default_factory=list)


def _parse_setup_steps(raw: list) -> list[SetupStep]:
    """Parse a list of setup step entries.

    Accepts both short-form (bare string) and long-form (object with
    ``name`` and ``run`` keys), matching the GitHub Actions convention.
    """
    steps: list[SetupStep] = []
    for i, entry in enumerate(raw):
        if isinstance(entry, str):
            steps.append(SetupStep(name=f"step-{i}", run=entry))
        elif isinstance(entry, dict) and isinstance(entry.get("run"), str):
            steps.append(SetupStep(name=entry.get("name", f"step-{i}"), run=entry["run"]))
        else:
            log.warning("setup[%d]: skipping invalid entry (expected string or {run: ...})", i)
    return steps


def load_config(workdir: str = ".") -> Config:
    """Load project config from ``.agentic-ci/config.yml`` in *workdir*.

    Returns a default (empty) Config if the file does not exist.
    """
    path = os.path.join(workdir, REPO_CONFIG_PATH)
    if not os.path.isfile(path):
        return Config()

    with open(path) as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError:
            log.warning("Failed to parse %s, skipping", path)
            return Config()

    if not isinstance(data, dict):
        return Config()

    setup_raw = data.get("setup", [])
    if not isinstance(setup_raw, list):
        return Config()

    return Config(setup=_parse_setup_steps(setup_raw))
