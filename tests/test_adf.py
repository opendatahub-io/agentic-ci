"""Tests for ADF conversion utilities."""

from agentic_ci.jira.adf import adf_to_text, text_to_adf


def test_empty_text_to_adf():
    result = text_to_adf("")
    assert result == {"type": "doc", "version": 1, "content": []}


def test_plain_text_to_adf():
    result = text_to_adf("Hello world")
    assert result["type"] == "doc"
    assert result["version"] == 1
    assert len(result["content"]) == 1
    para = result["content"][0]
    assert para["type"] == "paragraph"
    assert para["content"][0]["text"] == "Hello world"


def test_bold_markup():
    result = text_to_adf("This is **bold** text")
    para = result["content"][0]
    texts = para["content"]
    bold_node = [t for t in texts if t.get("marks")]
    assert len(bold_node) == 1
    assert bold_node[0]["text"] == "bold"
    assert bold_node[0]["marks"] == [{"type": "strong"}]


def test_italic_markup():
    result = text_to_adf("This is *italic* text")
    para = result["content"][0]
    texts = para["content"]
    italic_node = [t for t in texts if t.get("marks")]
    assert len(italic_node) == 1
    assert italic_node[0]["text"] == "italic"
    assert italic_node[0]["marks"] == [{"type": "em"}]


def test_code_block():
    result = text_to_adf("```python\nprint('hi')\n```")
    code = result["content"][0]
    assert code["type"] == "codeBlock"
    assert code["attrs"]["language"] == "python"
    assert code["content"][0]["text"] == "print('hi')"


def test_code_block_no_lang():
    result = text_to_adf("```\nsome code\n```")
    code = result["content"][0]
    assert code["type"] == "codeBlock"
    assert "attrs" not in code
    assert code["content"][0]["text"] == "some code"


def test_heading():
    result = text_to_adf("## My Heading")
    heading = result["content"][0]
    assert heading["type"] == "heading"
    assert heading["attrs"]["level"] == 2


def test_heading_levels():
    for level in range(1, 7):
        hashes = "#" * level
        result = text_to_adf(f"{hashes} Heading {level}")
        heading = result["content"][0]
        assert heading["attrs"]["level"] == level


def test_bullets_dash():
    result = text_to_adf("- item one\n- item two")
    bullet_list = result["content"][0]
    assert bullet_list["type"] == "bulletList"
    assert len(bullet_list["content"]) == 2


def test_bullets_asterisk():
    result = text_to_adf("* item one\n* item two")
    bullet_list = result["content"][0]
    assert bullet_list["type"] == "bulletList"
    assert len(bullet_list["content"]) == 2


def test_url_inline_card():
    result = text_to_adf("See https://example.com for details")
    para = result["content"][0]
    cards = [n for n in para["content"] if n["type"] == "inlineCard"]
    assert len(cards) == 1
    assert cards[0]["attrs"]["url"] == "https://example.com"


def test_adf_to_text_paragraph():
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Hello world"}],
            }
        ],
    }
    assert adf_to_text(adf) == "Hello world"


def test_adf_to_text_bullet_list():
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "first"}],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "second"}],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    result = adf_to_text(adf)
    assert "- first" in result
    assert "- second" in result


def test_adf_to_text_empty():
    assert adf_to_text({}) == ""
    assert adf_to_text(None) == ""


def test_roundtrip_plain():
    text = "Simple paragraph"
    adf = text_to_adf(text)
    recovered = adf_to_text(adf)
    assert recovered == text
