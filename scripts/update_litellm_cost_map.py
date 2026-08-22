#!/usr/bin/env python3
"""Generate agentic-ci's bundled OpenAI pricing snapshot from LiteLLM."""

import argparse
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "src" / "agentic_ci" / "data" / "litellm_openai_cost_map.json"
LITELLM_REPOSITORY = "BerriAI/litellm"
LITELLM_COST_MAP_PATH = "model_prices_and_context_window.json"
GITHUB_API_ROOT = "https://api.github.com"
RAW_GITHUB_ROOT = "https://raw.githubusercontent.com"
MINIMUM_SOURCE_MODELS = 1_000
MINIMUM_OPENAI_MODELS = 100
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def _request_json(url: str) -> Any:
    """Fetch and decode a JSON document."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "agentic-ci-cost-map-generator/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(GITHUB_API_ROOT):
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def resolve_commit(ref: str) -> str:
    """Resolve a LiteLLM branch, tag, or SHA to an immutable commit SHA."""
    encoded_ref = urllib.parse.quote(ref, safe="")
    document = _request_json(f"{GITHUB_API_ROOT}/repos/{LITELLM_REPOSITORY}/commits/{encoded_ref}")
    commit = document.get("sha") if isinstance(document, dict) else None
    if not isinstance(commit, str) or SHA_PATTERN.fullmatch(commit) is None:
        raise ValueError(f"GitHub returned an invalid commit SHA for LiteLLM ref {ref!r}")
    return commit


def fetch_cost_map(commit: str) -> dict[str, Any]:
    """Fetch LiteLLM's cost map from an immutable commit."""
    if SHA_PATTERN.fullmatch(commit) is None:
        raise ValueError(f"Invalid LiteLLM commit SHA: {commit!r}")
    document = _request_json(
        f"{RAW_GITHUB_ROOT}/{LITELLM_REPOSITORY}/{commit}/{LITELLM_COST_MAP_PATH}"
    )
    if not isinstance(document, dict) or len(document) < MINIMUM_SOURCE_MODELS:
        raise ValueError("LiteLLM cost map is missing or unexpectedly small")
    return document


def build_snapshot(source_map: dict[str, Any], commit: str) -> dict[str, Any]:
    """Build a compact snapshot containing every OpenAI pricing entry."""
    models: dict[str, dict[str, Any]] = {}
    aliases: dict[str, dict[str, Any]] = {}

    for model, raw_entry in source_map.items():
        if not isinstance(raw_entry, dict) or raw_entry.get("litellm_provider") != "openai":
            continue

        entry = {
            key: value
            for key, value in raw_entry.items()
            if "cost" in key or key in {"aliases", "litellm_provider"}
        }
        if not any(key.endswith("cost_per_token") or "cost_per_token_" in key for key in entry):
            continue
        models[model] = entry

        raw_aliases = raw_entry.get("aliases", [])
        if isinstance(raw_aliases, list):
            for alias in raw_aliases:
                if isinstance(alias, str) and alias not in source_map:
                    aliases.setdefault(alias, entry)

    for alias, entry in aliases.items():
        models.setdefault(alias, entry)

    if len(models) < MINIMUM_OPENAI_MODELS:
        raise ValueError("LiteLLM cost map contains unexpectedly few OpenAI pricing entries")

    return {
        "source": {
            "repository": f"https://github.com/{LITELLM_REPOSITORY}",
            "path": LITELLM_COST_MAP_PATH,
            "commit": commit,
            "url": (f"{RAW_GITHUB_ROOT}/{LITELLM_REPOSITORY}/{commit}/{LITELLM_COST_MAP_PATH}"),
        },
        "models": models,
    }


def write_snapshot(snapshot: dict[str, Any], output: Path) -> None:
    """Write the generated snapshot deterministically."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def display_output_path(output: Path) -> Path:
    """Return a concise output path without rejecting external destinations."""
    try:
        return output.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return output


def main() -> None:
    """Generate the bundled cost map."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main", help="LiteLLM branch, tag, or commit")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    commit = resolve_commit(args.ref)
    snapshot = build_snapshot(fetch_cost_map(commit), commit)
    write_snapshot(snapshot, args.output)
    print(
        f"Updated {display_output_path(args.output)} from LiteLLM {commit} "
        f"({len(snapshot['models'])} OpenAI models)"
    )


if __name__ == "__main__":
    main()
