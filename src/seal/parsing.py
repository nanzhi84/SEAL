"""Frozen parser semantics and deterministic identities, independent of persistence."""

import os
import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .core import SealError, digest
from .markdown import MARKDOWN_CONTRACT
from .office_runtime import OFFICE_CONTRACT, TCVN3_CONTRACT, libreoffice_identity
from .pdf_scan import INK_THRESHOLD, PDF_CONTRACT, SCAN_INK_RATIO, SCAN_TEXT_CHARS
from .quality import QUALITY_CONTRACT

MANIFEST_ALGORITHM = "seal.parser-manifest.v3"
FINGERPRINT_ALGORITHM = "seal.fingerprint.v1"
ZIP_CONTRACT = "seal.zip.v1"
ALWAYS = ("algorithm", "adapter", "fetch", "markdown")


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
                [executable, option], capture_output=True, timeout=5, check=True
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
    from .adapters import contract

    fetch = {"mode": config["fetch"]["mode"]}
    if fetch["mode"] == "rendered_dom":
        from .browser_runtime import identity

        fetch.update(wait_until=config["fetch"]["wait_until"], runtime=identity())
    ocr = config["ocr"]
    engine = {"engine": ocr["engine"], "contract": "seal.ocr.v1"}
    if ocr["engine"] == "tesseract":
        engine.update(
            languages=ocr["languages"], dpi=ocr["dpi"], runtime=ocr_runtime(ocr["languages"])
        )
    else:
        engine.update(model=ocr["model"])
    language = str(config["metadata"].get("language") or "")
    return {
        "algorithm": MANIFEST_ALGORITHM,
        "adapter": {
            "name": config["adapter"],
            "contract": contract(config["adapter"]),
            "rules": digest(
                {
                    "adapter_options": config["adapter_options"],
                    "documents": config["documents"],
                    "metadata": config["metadata"],
                }
            ),
        },
        "fetch": fetch,
        "markdown": {
            "contract": MARKDOWN_CONTRACT,
            "crawl4ai": package("crawl4ai"),
            "lxml": package("lxml"),
        },
        "office": {
            "contract": OFFICE_CONTRACT,
            "policy": config["parser"]["office_text_policy"],
            "python-docx": package("python-docx"),
            "openpyxl": package("openpyxl"),
            "xlrd": package("xlrd"),
            "libreoffice": libreoffice_identity(),
        },
        "tcvn3": {"contract": TCVN3_CONTRACT},
        "zip": {"contract": ZIP_CONTRACT},
        "pdf": {
            "contract": PDF_CONTRACT,
            "pypdfium2": package("pypdfium2"),
            "thresholds": {
                "native_text_chars_lt": SCAN_TEXT_CHARS,
                "ink_ratio_gt": SCAN_INK_RATIO,
                "ink_gray_lt": INK_THRESHOLD,
            },
        },
        "ocr": engine,
        "quality": {
            "contract": QUALITY_CONTRACT,
            "accept_routes": config["parser"]["quality_accept_routes"],
            "profile": "vi" if language.startswith("vi") else "generic",
        },
    }


def require_runtime(config, frozen):
    current = manifest(config)
    if frozen is None or frozen != current:
        raise SealError("parser_runtime_changed_requires_new_run")
    if current["fetch"]["mode"] == "rendered_dom" and current["fetch"]["runtime"]["status"] != (
        "available"
    ):
        raise SealError("browser_runtime_unavailable")


def effective(frozen, used):
    """The frozen manifest subset that actually produced this document."""
    if frozen is None:
        raise SealError("parser_manifest_required")
    return {k: frozen[k] for k in frozen if k in ALWAYS or k in set(used)}


def fingerprint(kind, value):
    return digest({"algorithm": FINGERPRINT_ALGORITHM, "kind": kind, "value": value})
