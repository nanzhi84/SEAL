"""Credential-free PDF text acquisition; stdout is a bounded JSON protocol."""

import json
import math
import os
import signal
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from crawl4ai.processors.pdf import NaivePDFProcessorStrategy
from crawl4ai.processors.pdf.processor import PDFPage, PDFProcessResult
from crawl4ai.processors.pdf.utils import clean_pdf_text, clean_pdf_text_to_html
from pypdf import PdfReader

from seal.core import SealError
from seal.pdf_contract import PDF_PROCESS_ERRORS


def command(args, limit):
    # stderr may contain input text or local paths: never forward it to the parent.
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            output = process.stdout.read(limit + 1)
            if len(output) > limit:
                raise SealError("pdf_output_too_large")
            if process.wait() != 0:
                raise SealError("pdf_ocr_failed")
            return output.decode("utf-8", errors="strict")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def recognize(path, count, cfg):
    import pypdfium2 as pdfium

    executable = cfg["executable"]
    available = command([executable, "--list-langs"], 65536).splitlines()[1:]
    if not set(cfg["languages"].split("+")).issubset(available):
        raise SealError("pdf_ocr_failed")
    version = command([executable, "--version"], 65536).splitlines()[0]
    pages = []
    size = 0
    with pdfium.PdfDocument(path) as document:
        if len(document) != count:
            raise SealError("pdf_ocr_failed")
        for index in range(count):
            with closing(document[index]) as page:
                width, height = page.get_size()
                scale = cfg["dpi"] / 72
                pixels = math.ceil(width * scale) * math.ceil(height * scale)
                if pixels > cfg["max_page_pixels"] or pixels <= 0:
                    raise SealError("pdf_ocr_pixel_limit_exceeded")
                image_path = Path("page.png")
                with closing(page.render(scale=scale)) as bitmap:
                    with bitmap.to_pil() as image:
                        image.save(image_path)
                try:
                    text = command(
                        [
                            executable,
                            str(image_path),
                            "stdout",
                            "-l",
                            cfg["languages"],
                            "--dpi",
                            str(cfg["dpi"]),
                            "--psm",
                            "3",
                        ],
                        cfg["max_result_bytes"] - size,
                    )
                finally:
                    image_path.unlink(missing_ok=True)
            size += len(text.encode("utf-8"))
            pages.append(PDFPage(page_number=index + 1, raw_text=text))
    return pages, {
        "engine": "tesseract",
        "version": version,
        "config": {k: v for k, v in cfg.items() if k != "executable"},
    }


def process(path, cfg):
    try:
        reader = PdfReader(path)
        count = len(reader.pages)
        if not count:
            raise SealError("pdf_zero_pages")
        native = any(page.extract_text().strip() for page in reader.pages)
    except SealError:
        raise
    except Exception:
        raise SealError("pdf_invalid_or_encrypted") from None

    # Freeze the acquisition count/method before either processor can lose pages.
    sys.stdout.write(json.dumps({"pages": count, "method": "native" if native else "ocr"}) + "\n")
    sys.stdout.flush()
    processor = NaivePDFProcessorStrategy(extract_images=False, max_pages=count)
    result = PDFProcessResult(metadata=processor._extract_metadata(Path(path), reader), pages=[])
    ocr = None
    if native:
        # Preserve the upstream native batching, page extraction and conversion.
        try:
            result = processor.process_batch(Path(path))
        except Exception:
            raise SealError("pdf_invalid_or_encrypted") from None
    else:
        if count > cfg["max_pages"]:
            raise SealError("pdf_ocr_page_limit_exceeded")
        result.pages, ocr = recognize(path, count, cfg)
        for page in result.pages:
            page.markdown = clean_pdf_text(page.page_number, page.raw_text)
            page.html = clean_pdf_text_to_html(page.page_number, page.raw_text)
    if not any(page.raw_text.strip() for page in result.pages):
        raise SealError("pdf_no_text")
    return {"method": "native" if native else "ocr", "result": asdict(result), "ocr": ocr}


def stop(signum, frame):
    # Unwind Popen contexts so a normal group TERM also reaps the active engine.
    raise SystemExit(128 + signum)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cfg = json.loads(sys.stdin.buffer.read(8192))
    try:
        payload = json.dumps(process("input.pdf", cfg), ensure_ascii=True, default=str).encode()
        if len(payload) > cfg["max_result_bytes"]:
            raise SealError("pdf_output_too_large")
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, SealError) and exc.code in PDF_PROCESS_ERRORS
            else "pdf_ocr_failed"
        )
        payload = json.dumps({"error": code}).encode()
    sys.stdout.buffer.write(payload + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    # No inherited DB/S3 configuration or network acquisition is needed here.
    os.umask(0o077)
    main()
