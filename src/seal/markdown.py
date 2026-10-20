"""Fixed Markdown conversion contracts shared by every adapter and extractor."""

import re
import unicodedata

MARKDOWN_CONTRACT = "seal.markdown.v1"
_ESCAPE = re.compile(r"([\\`*_\[\]<>|])")
_LINE_START = re.compile(r"^(\s*)([#>+\-=]|\d+[.)])(?=\s|$)", re.M)


def compact(value):
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return re.sub(r"[ \t]+", " ", value).strip()


def text_to_markdown(value):
    """Keep words, line breaks and paragraphs; escape presentation metacharacters only.

    No heading or list structure is guessed from plain text.
    """
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    value = value.replace("\x00", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = _ESCAPE.sub(r"\\\1", text)
    text = _LINE_START.sub(lambda m: m[1] + "\\" + m[2], text)
    # Hard line breaks inside a paragraph are retained explicitly.
    text = re.sub(r"(?<!\n)\n(?!\n)", "  \n", text)
    return text + "\n" if text else ""


def html_to_markdown(html, base_url):
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    result = DefaultMarkdownGenerator().generate_markdown(
        input_html=html, base_url=base_url, citations=False
    )
    text = unicodedata.normalize("NFC", result.raw_markdown or "")
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    return text + "\n" if text else ""


def normalized(markdown):
    text = unicodedata.normalize("NFC", markdown.replace("\r\n", "\n").replace("\r", "\n"))
    text = text.replace("\x00", "").strip()
    return text + "\n" if text else ""
