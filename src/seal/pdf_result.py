"""Validate the process boundary, then reuse the SDK PDF scraper's packaging."""

import re

from crawl4ai.processors.pdf import PDFContentScrapingStrategy
from crawl4ai.processors.pdf.processor import PDFMetadata, PDFPage, PDFProcessResult
from lxml import html as lxml_html
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .core import SealError
from .parsing import OCR_TEXT_CONTRACT
from .pdf_contract import PDF_TEXT_CONTRACT


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Page(Strict):
    page_number: int = Field(ge=1)
    raw_text: str
    markdown: str
    html: str
    images: list[dict]
    links: list[str]
    layout: list[dict]


class Metadata(Strict):
    title: str | None
    author: str | None
    producer: str | None
    created: str | None
    modified: str | None
    pages: int = Field(ge=1)
    encrypted: bool
    file_size: int = Field(ge=0)


class ProcessResult(Strict):
    metadata: Metadata
    pages: list[Page] = Field(min_length=1)
    processing_time: float
    version: str


class OCR(Strict):
    engine: str
    version: str
    config: dict


class Output(Strict):
    method: str = Field(pattern="^(native|ocr)$")
    result: ProcessResult
    ocr: OCR | None


def safe_text(value):
    # Retain malformed font CMaps literally, as in the existing native contract.
    return (
        value.encode("utf-16", "surrogatepass")
        .decode("utf-16", "surrogatepass")
        .encode("utf-8", "backslashreplace")
        .decode("utf-8")
    )


def validate(value, size, pages, method, cfg):
    try:
        output = Output.model_validate(value)
        result = output.result
        count = result.metadata.pages
        if (
            result.metadata.file_size != size
            or len(result.pages) != count
            or count != pages
            or output.method != method
        ):
            raise ValueError
        if [p.page_number for p in result.pages] != list(range(1, count + 1)):
            raise ValueError
        if output.method == "ocr":
            if (
                count > cfg["max_pages"]
                or output.ocr is None
                or output.ocr.engine != "tesseract"
                or not output.ocr.version
                or output.ocr.config != {k: v for k, v in cfg.items() if k != "executable"}
            ):
                raise ValueError
        elif output.ocr is not None:
            raise ValueError
        if not any(p.raw_text.strip() for p in result.pages):
            raise SealError("pdf_no_text")
        return output
    except (ValueError, ValidationError, TypeError):
        raise SealError("pdf_output_invalid") from None


class PreparedProcessor:
    def __init__(self, output):
        result = output.result
        self.result = PDFProcessResult(
            metadata=PDFMetadata(**result.metadata.model_dump()),
            pages=[PDFPage(**p.model_dump()) for p in result.pages],
        )

    def process_batch(self, path):
        return self.result


def wrap(output, path, archive_ref, **kwargs):
    scraper = PDFContentScrapingStrategy(extract_images=False)
    scraper.pdf_processor = PreparedProcessor(output)
    result = scraper.scrap(str(path), "", **kwargs)
    result.metadata["pdf_text_contract"] = (
        PDF_TEXT_CONTRACT if output.method == "native" else OCR_TEXT_CONTRACT
    )
    result.metadata["pdf"] = {
        "method": output.method,
        "pages": [
            {"page_number": p.page_number, "text": safe_text(p.raw_text)}
            for p in output.result.pages
        ],
        "ocr": output.ocr.model_dump() if output.ocr else None,
        "original_ref": archive_ref,
    }
    lone = len(re.findall(r"[\ud800-\udfff]", result.cleaned_html))
    result.cleaned_html = safe_text(result.cleaned_html)
    if lone:
        result.metadata["pdf_text_encoding"] = {
            "unpaired_code_units": lone,
            "representation": "literal_utf16_escape",
        }
    pages = lxml_html.fromstring(result.cleaned_html).xpath('//div[@class="pdf-page"]')
    if len(pages) != output.result.metadata.pages:
        raise SealError("pdf_page_parse_failed")
    return result
