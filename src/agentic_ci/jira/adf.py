"""Atlassian Document Format (ADF) conversion utilities.

Converts between plain text (with wiki-style markup) and ADF, the JSON
document format used by Jira Cloud REST API v3 for rich-text fields.
"""

from __future__ import annotations

import re


def text_to_adf(text: str) -> dict:
    """Convert plain text with wiki markup to Atlassian Document Format.

    Handles:
    - {code}...{code} blocks -> codeBlock nodes
    - h1.-h6. headings -> heading nodes
    - * bullets -> bulletList nodes
    - *bold* and _italic_ inline markup
    - URLs -> inlineCard nodes
    - Double newlines split paragraphs, single newlines become hardBreak
    """
    if not text:
        return {"type": "doc", "version": 1, "content": []}

    content: list[dict] = []
    code_pattern = re.compile(r"\{code(?::([^}]*))?\}(.*?)\{code\}", re.DOTALL)

    last_end = 0
    for match in code_pattern.finditer(text):
        before = text[last_end : match.start()]
        if before.strip():
            content.extend(_wiki_text_to_adf_blocks(before))
        code_text = match.group(2)
        if code_text.startswith("\n"):
            code_text = code_text[1:]
        if code_text.endswith("\n"):
            code_text = code_text[:-1]
        lang = match.group(1) or ""
        node: dict = {"type": "text", "text": code_text}
        block: dict = {"type": "codeBlock", "content": [node]}
        if lang:
            block["attrs"] = {"language": lang}
        content.append(block)
        last_end = match.end()

    remaining = text[last_end:]
    if remaining.strip():
        content.extend(_wiki_text_to_adf_blocks(remaining))

    if not content:
        content.append({"type": "paragraph", "content": [{"type": "text", "text": ""}]})

    return {"type": "doc", "version": 1, "content": content}


def _wiki_text_to_adf_blocks(text: str) -> list[dict]:
    """Convert non-code wiki text into ADF block nodes."""
    blocks: list[dict] = []
    heading_re = re.compile(r"^h([1-6])\.\s+(.+)$")
    bullet_re = re.compile(r"^\*\s+(.+)$")

    paragraphs = text.split("\n\n")
    for para in paragraphs:
        if not para.strip():
            continue
        lines = para.split("\n")
        pending_lines: list[str] = []
        pending_bullets: list[str] = []

        for line in lines:
            stripped = line.strip()
            m_h = heading_re.match(stripped)
            m_b = bullet_re.match(stripped)

            if m_h:
                if pending_bullets:
                    blocks.append(_bullets_to_list(pending_bullets))
                    pending_bullets = []
                if pending_lines:
                    blocks.append(_lines_to_paragraph(pending_lines))
                    pending_lines = []
                blocks.append(
                    {
                        "type": "heading",
                        "attrs": {"level": int(m_h.group(1))},
                        "content": _parse_inline_markup(m_h.group(2)),
                    }
                )
            elif m_b:
                if pending_lines:
                    blocks.append(_lines_to_paragraph(pending_lines))
                    pending_lines = []
                pending_bullets.append(m_b.group(1))
            else:
                if pending_bullets:
                    blocks.append(_bullets_to_list(pending_bullets))
                    pending_bullets = []
                pending_lines.append(line)

        if pending_bullets:
            blocks.append(_bullets_to_list(pending_bullets))
        if pending_lines:
            blocks.append(_lines_to_paragraph(pending_lines))

    return blocks


def _parse_inline_markup(text: str) -> list[dict]:
    """Parse *bold*, _italic_, and URLs into ADF inline nodes."""
    pattern = re.compile(
        r"(https?://\S+)" r"|(?<!\w)\*([^*\n]+)\*(?!\w)" r"|(?<!\w)_([^_\n]+)_(?!\w)"
    )
    nodes: list[dict] = []
    last_end = 0
    for match in pattern.finditer(text):
        before = text[last_end : match.start()]
        if before:
            nodes.append({"type": "text", "text": before})
        if match.group(1) is not None:
            nodes.append({"type": "inlineCard", "attrs": {"url": match.group(1)}})
        elif match.group(2) is not None:
            nodes.append({"type": "text", "text": match.group(2), "marks": [{"type": "strong"}]})
        else:
            nodes.append({"type": "text", "text": match.group(3), "marks": [{"type": "em"}]})
        last_end = match.end()
    remaining = text[last_end:]
    if remaining:
        nodes.append({"type": "text", "text": remaining})
    if not nodes:
        nodes.append({"type": "text", "text": text})
    return nodes


def _bullets_to_list(items: list[str]) -> dict:
    """Convert bullet item texts into an ADF bulletList node."""
    return {
        "type": "bulletList",
        "content": [
            {
                "type": "listItem",
                "content": [{"type": "paragraph", "content": _parse_inline_markup(item)}],
            }
            for item in items
        ],
    }


def _lines_to_paragraph(lines: list[str]) -> dict:
    """Convert text lines into an ADF paragraph with hardBreaks."""
    para_content: list[dict] = []
    for i, line in enumerate(lines):
        if i > 0:
            para_content.append({"type": "hardBreak"})
        para_content.extend(_parse_inline_markup(line))
    return {"type": "paragraph", "content": para_content}


def adf_to_text(adf: dict) -> str:
    """Extract plain text from an ADF document."""
    if not adf or not isinstance(adf, dict):
        return ""

    def extract_node(node: dict) -> str:
        node_type = node.get("type", "")
        if node_type == "text":
            text = node.get("text", "")
            for mark in node.get("marks", []):
                if mark.get("type") == "link":
                    href = mark.get("attrs", {}).get("href", "")
                    if href and href != text:
                        text = f"{text} {href}"
            return text
        elif node_type == "hardBreak":
            return "\n"
        elif node_type == "paragraph":
            return extract_children(node) + "\n"
        elif node_type == "heading":
            return extract_children(node) + "\n"
        elif node_type == "codeBlock":
            return extract_children(node) + "\n"
        elif node_type in ("bulletList", "orderedList"):
            return extract_children(node)
        elif node_type == "listItem":
            return "- " + extract_children(node)
        elif node_type in ("inlineCard", "blockCard"):
            return node.get("attrs", {}).get("url", "")
        elif node_type == "blockquote":
            lines = extract_children(node).rstrip("\n").split("\n")
            return "\n".join(f"> {line}" for line in lines) + "\n"
        elif node_type == "doc":
            return extract_children(node)
        else:
            return extract_children(node)

    def extract_children(node: dict) -> str:
        return "".join(extract_node(child) for child in node.get("content", []))

    result = extract_node(adf)
    while result.endswith("\n\n\n"):
        result = result[:-1]
    return result.rstrip("\n")
