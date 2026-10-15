"""Stable PDF representation identity; runtime timestamps are not revisions."""

PDF_TEXT_CONTRACT = "crawl4ai_0_9_5_native_pages_v1"

PDF_PROCESS_ERRORS = {
    "pdf_invalid_or_encrypted",
    "pdf_zero_pages",
    "pdf_ocr_page_limit_exceeded",
    "pdf_ocr_pixel_limit_exceeded",
    "pdf_ocr_failed",
    "pdf_output_too_large",
    "pdf_no_text",
}
