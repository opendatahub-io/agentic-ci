"""Parse YAML frontmatter from SKILL.md files without requiring pyyaml."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)
_KEY_VALUE_RE = re.compile(r"^([a-z][a-z0-9-]*)\s*:\s*(.*)", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s+-\s+(.*)")
_CONTINUATION_RE = re.compile(r"^\s+\S")
_NESTED_KV_RE = re.compile(r"^\s+([a-z][a-z0-9-]*)\s*:\s*(.*)", re.IGNORECASE)

# "x-" prefix follows the convention for custom extension fields (like HTTP
# headers and OpenAPI), avoiding collisions if the Agent Skills spec ever
# adds an official "artifacts" key with different semantics.
_METADATA_ARTIFACTS_KEY = "x-artifacts"


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def parse_frontmatter(text: str) -> dict[str, str | list[str] | dict[str, str]]:
    """Parse YAML frontmatter from markdown text.

    Handles the subset of YAML used in SKILL.md files: simple key-value pairs,
    folded scalars (>-), block lists (- item), and one-level nested maps.
    """
    text = text.replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}

    result: dict[str, str | list[str] | dict[str, str]] = {}
    lines = m.group(1).splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]
        kv = _KEY_VALUE_RE.match(line)
        if not kv:
            i += 1
            continue

        key = kv.group(1)
        value = _strip_quotes(kv.group(2).strip())

        if value == ">-":
            parts: list[str] = []
            i += 1
            while i < len(lines) and _CONTINUATION_RE.match(lines[i]):
                parts.append(lines[i].strip())
                i += 1
            result[key] = " ".join(parts)
        elif value == "":
            i += 1
            if i < len(lines) and _LIST_ITEM_RE.match(lines[i]):
                items: list[str] = []
                while i < len(lines):
                    item_match = _LIST_ITEM_RE.match(lines[i])
                    if item_match:
                        items.append(item_match.group(1).strip())
                        i += 1
                    else:
                        break
                result[key] = items
            elif i < len(lines) and _NESTED_KV_RE.match(lines[i]):
                nested: dict[str, str] = {}
                while i < len(lines):
                    nkv = _NESTED_KV_RE.match(lines[i])
                    if nkv:
                        nested[nkv.group(1)] = _strip_quotes(nkv.group(2).strip())
                        i += 1
                    else:
                        break
                result[key] = nested
            else:
                result[key] = []
        else:
            result[key] = value
            i += 1

    return result


@dataclass
class SkillMetadata:
    """Parsed metadata from a SKILL.md frontmatter block."""

    name: str
    description: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    user_invocable: bool = False
    metadata: dict[str, str] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)


def load_skill_metadata(skill_md_path: Path) -> SkillMetadata:
    """Read a SKILL.md file and return parsed metadata.

    Raises FileNotFoundError if the file does not exist.
    """
    text = skill_md_path.read_text(encoding="utf-8")
    data = parse_frontmatter(text)

    tools_raw = data.get("allowed-tools", "")
    if isinstance(tools_raw, str) and tools_raw:
        allowed_tools = tools_raw.split()
    else:
        allowed_tools = []

    invocable_raw = data.get("user-invocable", "")
    user_invocable = isinstance(invocable_raw, str) and invocable_raw.lower() == "true"

    raw_metadata = data.get("metadata", {})
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

    artifacts_str = metadata.get(_METADATA_ARTIFACTS_KEY, "")
    artifacts = artifacts_str.split() if artifacts_str else []

    description = data.get("description", "")
    if not isinstance(description, str):
        description = ""

    return SkillMetadata(
        name=str(data.get("name", "")),
        description=description,
        allowed_tools=allowed_tools,
        user_invocable=user_invocable,
        metadata=metadata,
        artifacts=artifacts,
    )


def collect_artifacts(*skill_md_paths: Path) -> list[str]:
    """Load each SKILL.md, extract artifacts, deduplicate, and return sorted.

    Logs warnings for missing or unreadable files but does not raise.
    """
    seen: set[str] = set()
    for path in skill_md_paths:
        try:
            meta = load_skill_metadata(path)
        except (FileNotFoundError, OSError) as exc:
            log.warning("Skipping %s: %s", path, exc)
            continue
        seen.update(meta.artifacts)
    return sorted(seen)
