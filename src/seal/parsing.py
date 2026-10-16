"""Frozen parser semantics and deterministic identity, independent of persistence."""

import os
import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .config import OCRConfig
from .core import SealError, digest
from .pdf_contract import PDF_TEXT_CONTRACT

# Bump when SEAL changes parsing, normalization or output semantics. Runtime-only
# scheduling and observability changes do not change this explicit revision.
ADAPTER_CONTRACT = "seal.parser.v2"
OUTPUT_CONTRACT = "seal.crawl-result.sha256-cas.v1"
OCR_TEXT_CONTRACT = "crawl4ai_0_9_5_ocr_v1"


def package(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"


def ocr_runtime(languages):
    executable = shutil.which(os.environ.get("SEAL_TESSERACT", "tesseract"))
    if not executable:
        return {"status": "unavailable"}
    try:

        def probe(option):
            result = subprocess.run(
                [executable, option], capture_output=True, timeout=3, check=True
            )
            if len(result.stdout) > 65536:
                raise ValueError
            return result.stdout.decode("utf-8")

        engine = probe("--version").splitlines()[0]
        listing = probe("--list-langs")
        match = re.search(r'List of available languages in "([^"]+)"', listing)
        root = Path(os.environ.get("TESSDATA_PREFIX") or (match[1] if match else ""))
        models = {}
        for language in languages.split("+"):
            path = root / (language + ".traineddata")
            models[language] = digest(path.read_bytes()) if path.is_file() else "unavailable"
        return {
            "status": "available",
            "version": engine,
            "executable_sha256": digest(Path(executable).read_bytes()),
            "models": models,
        }
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {"status": "unavailable"}


def manifest(config):
    crawl = config["crawl4ai"]
    result = {
        "algorithm": "seal.parser-manifest.v2",
        "adapter_contract": ADAPTER_CONTRACT,
        "output_contract": OUTPUT_CONTRACT,
        "framework_version": package("crawl4ai"),
        "html": {
            "dependencies": {name: package(name) for name in ("lxml", "beautifulsoup4", "chardet")},
            "word_count_threshold": crawl["word_count_threshold"],
            "css_selector": crawl["css_selector"],
        },
        "input_mode": crawl["mode"],
    }
    if crawl["mode"] == "rendered_dom":
        from .browser_runtime import identity

        result["dom"] = {"wait_until": crawl["wait_until"], "runtime": identity()}
    else:
        ocr = OCRConfig.model_validate(config.get("ocr", {})).model_dump()
        result["pdf"] = {
            "native_contract": PDF_TEXT_CONTRACT,
            "ocr_contract": OCR_TEXT_CONTRACT,
            "pypdf": package("pypdf"),
            "pypdfium2": package("pypdfium2"),
            "ocr": {k: ocr[k] for k in ("languages", "dpi")},
            "runtime": ocr_runtime(ocr["languages"]),
        }
    return result


def require_runtime(config, frozen):
    current = manifest(config)
    if frozen is None or frozen != current:
        raise SealError("parser_runtime_changed_requires_new_run")
    if (
        current["input_mode"] == "rendered_dom"
        and current["dom"]["runtime"]["status"] != "available"
    ):
        raise SealError("browser_runtime_unavailable")


def effective(frozen, capture, envelope=None):
    if frozen is None:
        raise SealError("parser_manifest_required")
    selected = {
        k: frozen[k]
        for k in (
            "algorithm",
            "adapter_contract",
            "output_contract",
            "framework_version",
            "input_mode",
        )
    }
    metadata = (envelope or {}).get("result", {}).get("metadata") or {}
    mime = (
        next((v for k, v in capture.get("headers", {}).items() if k.lower() == "content-type"), "")
        .split(";")[0]
        .strip()
        .lower()
    )
    if frozen["input_mode"] == "http" and (
        mime == "application/pdf" or metadata.get("pdf_text_contract")
    ):
        pdf = frozen["pdf"]
        selected["pdf"] = {
            "pypdf": pdf["pypdf"],
            "text_contract": metadata.get("pdf_text_contract", pdf["native_contract"]),
        }
        if (metadata.get("pdf") or {}).get("method") == "ocr":
            selected["pdf"].update({k: pdf[k] for k in ("pypdfium2", "ocr", "runtime")})
    else:
        selected["html"] = frozen["html"]
        if frozen["input_mode"] == "rendered_dom":
            selected["dom"] = frozen["dom"]
    return selected


def input_identity(body_hash, input_mode, envelope):
    # Preserve body_hash semantics and include independent downloaded input files.
    result = envelope["result"]
    identity = {
        "body_hash": body_hash,
        "input_mode": input_mode,
        "attachment_refs": sorted(set(envelope["attachment_refs"]) - {body_hash}),
    }
    if not (result.get("metadata") or {}).get("pdf_text_contract"):
        # Raw response bytes alone miss charset changes. This is the actual input
        # text supplied to the parser, not a digest of generated result JSON.
        identity["parser_input_hash"] = digest((result.get("html") or "").encode("utf-8"))
        identity["base_url"] = result.get("redirected_url") or result.get("url")
    return identity


def version_identity(source_input, parser_fingerprint):
    return digest(
        {
            "algorithm": "seal.version-fingerprint.v1",
            "source_input": source_input,
            "parser_fingerprint": parser_fingerprint,
        }
    )
