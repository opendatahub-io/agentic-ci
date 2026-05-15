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
    result = text_to_adf("This is *bold* text")
    para = result["content"][0]
    texts = para["content"]
    bold_node = [t for t in texts if t.get("marks")]
    assert len(bold_node) == 1
    assert bold_node[0]["text"] == "bold"
    assert bold_node[0]["marks"] == [{"type": "strong"}]


def test_code_block():
    result = text_to_adf("{code:python}print('hi'){code}")
    code = result["content"][0]
    assert code["type"] == "codeBlock"
    assert code["attrs"]["language"] == "python"
    assert code["content"][0]["text"] == "print('hi')"


def test_heading():
    result = text_to_adf("h2. My Heading")
    heading = result["content"][0]
    assert heading["type"] == "heading"
    assert heading["attrs"]["level"] == 2


def test_bullets():
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
