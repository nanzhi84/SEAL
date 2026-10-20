"""Per-page PDF scan detection, dual-engine OCR and the quality gate.

Refactored from docs/reference/production_document_parse.py on pypdfium2: a whole-file
``any(page.extract_text())`` test misses scans whose only native text is a digital
signature and mixed native/scanned documents, so every page is measured separately.
"""

import base64
import json
import math
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path

from .core import SealError
from .markdown import normalized, text_to_markdown
from .quality import quality_check

PDF_CONTRACT = "seal.pdf-scan.v1"
SCAN_TEXT_CHARS = 30
SCAN_INK_RATIO = 0.003
INK_THRESHOLD = 210
ANALYSIS_SCALE = 0.5


def open_document(path):
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(str(path))
        if len(document) < 1:
            document.close()
            raise SealError("pdf_zero_pages")
        return document
    except SealError:
        raise
    except Exception:
        raise SealError("pdf_invalid_or_encrypted") from None


def page_text(page):
    textpage = page.get_textpage()
    try:
        return textpage.get_text_bounded().replace("\r\n", "\n").replace("\r", "\n")
    finally:
        textpage.close()


def ink_ratio(page):
    with closing(page.render(scale=ANALYSIS_SCALE, grayscale=True)) as bitmap:
        image = bitmap.to_pil().convert("L")
        histogram = image.histogram()
        total = sum(histogram)
        return sum(histogram[:INK_THRESHOLD]) / total if total else 0.0


def embedded_images(page):
    import pypdfium2.raw as raw

    return sum(1 for _ in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=3))


def analyze(path):
    """native_text_chars, embedded_images and 0.5x grayscale ink_ratio for every page."""
    pages, texts = [], []
    document = open_document(path)
    try:
        for index in range(len(document)):
            with closing(document[index]) as page:
                text = page_text(page)
                native = len(text.strip())
                ink = ink_ratio(page)
                pages.append(
                    {
                        "page": index + 1,
                        "native_text_chars": native,
                        "embedded_images": embedded_images(page),
                        "ink_ratio": round(ink, 5),
                        "suspected_scan": native < SCAN_TEXT_CHARS and ink > SCAN_INK_RATIO,
                    }
                )
                texts.append(text)
    except SealError:
        raise
    except Exception:
        raise SealError("pdf_invalid_or_encrypted") from None
    finally:
        document.close()
    return {
        "contract": PDF_CONTRACT,
        "page_count": len(pages),
        "native_text_chars": sum(p["native_text_chars"] for p in pages),
        "suspected_scan_pages": [p["page"] for p in pages if p["suspected_scan"]],
        "pages": pages,
    }, texts


def command(args, limit, timeout):
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise SealError("pdf_ocr_timeout") from None
    if len(output) > limit:
        raise SealError("pdf_output_too_large")
    if process.returncode != 0:
        raise SealError("pdf_ocr_failed")
    return output.decode("utf-8", errors="strict")


def tesseract_pages(path, numbers, cfg, deadline):
    import pypdfium2 as pdfium

    executable = shutil.which(os.environ.get("SEAL_TESSERACT", "tesseract"))
    if not executable:
        raise SealError("ocr_engine_unavailable")
    available = command([executable, "--list-langs"], 65536, 10).splitlines()[1:]
    if not set(cfg["languages"].split("+")).issubset(available):
        raise SealError("ocr_language_unavailable")
    version = command([executable, "--version"], 65536, 10).splitlines()[0]
    if len(numbers) > cfg["max_pages"]:
        raise SealError("pdf_ocr_page_limit_exceeded")
    result, size = {}, 0
    with pdfium.PdfDocument(str(path)) as document:
        for number in numbers:
            if time.monotonic() >= deadline:
                raise SealError("pdf_ocr_timeout")
            with closing(document[number - 1]) as page:
                width, height = page.get_size()
                scale = cfg["dpi"] / 72
                pixels = math.ceil(width * scale) * math.ceil(height * scale)
                if pixels > cfg["max_page_pixels"] or pixels <= 0:
                    raise SealError("pdf_ocr_pixel_limit_exceeded")
                image = str(Path(path).with_name(f"page-{number}.png"))
                with closing(page.render(scale=scale)) as bitmap:
                    bitmap.to_pil().save(image)
            try:
                text = command(
                    [
                        executable,
                        image,
                        "stdout",
                        "-l",
                        cfg["languages"],
                        "--dpi",
                        str(cfg["dpi"]),
                        "--psm",
                        "3",
                    ],
                    cfg["max_result_bytes"] - size,
                    max(1, deadline - time.monotonic()),
                )
            finally:
                if os.path.exists(image):
                    os.unlink(image)
            size += len(text.encode("utf-8"))
            result[number] = text
    return result, {"engine": "tesseract", "version": version, "languages": cfg["languages"]}


def br_request(url, payload=None, timeout=60):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(64 * 1024 * 1024 + 1)
    except (urllib.error.URLError, OSError, TimeoutError):
        raise SealError("br_ocr_unavailable") from None
    if len(body) > 64 * 1024 * 1024:
        raise SealError("pdf_output_too_large")
    try:
        value = json.loads(body)
    except ValueError:
        raise SealError("br_ocr_invalid_response") from None
    if (
        not isinstance(value, dict)
        or value.get("code") != 200
        or not isinstance(value.get("data"), dict)
    ):
        raise SealError("br_ocr_failed")
    return value["data"]


def br_ocr(path, cfg, deadline, page_count=0):
    """br-ocr-v1: /file_parse submit, /status/{uuid} poll, /result/{uuid} fetch."""
    if page_count > cfg["max_pages"]:
        raise SealError("pdf_ocr_page_limit_exceeded")
    base = (os.environ.get("SEAL_BR_OCR_URL") or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise SealError("br_ocr_url_required")
    with open(path, "rb") as stream:
        content = base64.b64encode(stream.read()).decode("ascii")
    task = str(uuid.uuid4())
    submitted = br_request(
        base + "/file_parse",
        {
            "task_id": task,
            "task_uuid": task,
            "task_type": "parse",
            "file_name": "input.pdf",
            "original_file_name": "input.pdf",
            "file_base64": content,
            "file_dir": None,
            "file_suffix": ".pdf",
            "model_name": cfg["model"],
            "split_setting": None,
            "extra": None,
            "priority": 1,
        },
        timeout=max(1.0, min(60.0, deadline - time.monotonic())),
    )
    task_uuid = submitted.get("task_uuid")
    if not isinstance(task_uuid, str) or not task_uuid or "/" in task_uuid:
        raise SealError("br_ocr_invalid_response")
    while time.monotonic() < deadline:
        time.sleep(cfg["poll_seconds"])
        status = br_request(
            base + "/status/" + task_uuid,
            timeout=max(1.0, min(60.0, deadline - time.monotonic())),
        )
        state = status.get("task_status")
        if state == "SUCCESS":
            result = br_request(
                base + "/result/" + task_uuid,
                timeout=max(1.0, min(60.0, deadline - time.monotonic())),
            )
            if not isinstance(result.get("whole_text_md"), str):
                raise SealError("br_ocr_invalid_response")
            if len(result["whole_text_md"].encode("utf-8")) > cfg["max_result_bytes"]:
                raise SealError("pdf_output_too_large")
            return result
        if state in {"FAILED", "ERROR", "CANCELLED", "CANCELED"}:
            raise SealError("br_ocr_failed")
    raise SealError("pdf_ocr_timeout")


def page_blocks(texts):
    return [
        {"type": "text", "page_idx": number, "text": text}
        for number, text in sorted(texts.items())
        if text.strip()
    ]


def extract_pdf(path, ocr, profile):
    """Native pages keep their text layer; suspected scan pages are OCR'd and gated."""
    started = time.monotonic()
    deadline = started + ocr["timeout_seconds"]
    analysis, native = analyze(path)
    scans = analysis["suspected_scan_pages"]
    info = {"kind": "pdf", "analysis": analysis, "method": "native", "ocr": None}
    parsers = ["pdf"]
    if not scans:
        texts = {i + 1: t for i, t in enumerate(native)}
        markdown = "\n".join(text_to_markdown(t) for t in native if t.strip())
        report = quality_check(
            analysis, {"page_parse_json": page_blocks(texts), "whole_text_md": markdown}, profile
        )
    elif ocr["engine"] == "tesseract":
        recognized, engine = tesseract_pages(path, scans, ocr, deadline)
        texts = {
            i + 1: (recognized.get(i + 1) if i + 1 in recognized else t)
            for i, t in enumerate(native)
        }
        markdown = "\n".join(text_to_markdown(texts[n]) for n in sorted(texts) if texts[n].strip())
        info.update(method="hybrid_ocr" if len(scans) < len(native) else "ocr", ocr=engine)
        report = quality_check(
            analysis, {"page_parse_json": page_blocks(texts), "whole_text_md": markdown}, profile
        )
        parsers.append("ocr")
    else:
        result = br_ocr(path, ocr, deadline, page_count=len(native))
        markdown = normalized(result["whole_text_md"])
        info.update(method="ocr", ocr={"engine": "br-ocr-v1", "model": ocr["model"]})
        report = quality_check(analysis, result, profile)
        parsers.append("ocr")
    if not markdown.strip():
        raise SealError("pdf_no_text")
    parsers.append("quality")
    return normalized(markdown), info, report, parsers
