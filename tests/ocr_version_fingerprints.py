"""Real OCR semantic identity acceptance supplement for Issue #28.

Run: uv run --no-sync python tests/ocr_version_fingerprints.py
Prerequisites: main issue28 runtime prerequisites, Tesseract and installed eng
traineddata. This produces an image-only PDF locally and uses the real engine.
Failure boundaries: accidentally using native extraction; omitted dpi semantics;
repeated upgrades; including timeouts/budgets in parser identity; bad historical
snapshot consumption. No mocked OCR output or production services are used.
"""

import hashlib
import io
import json
import secrets
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.fernet import Fernet
from issue28_artifact_audit import audit
from issue28_inputs import adjust
from issue28_runtime import Runtime, free_port
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader


def scanned_pdf():
    image = Image.new("RGB", (1400, 700), "white")
    canvas = ImageDraw.Draw(image)
    font_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
    font = (
        ImageFont.truetype(str(font_path), 66)
        if font_path.exists()
        else ImageFont.load_default(size=66)
    )
    for index, line in enumerate(("SCAN RECEIPT", "PUBLIC ALPHA 2026", "REAL OCR ACCEPTANCE")):
        canvas.text((70, 90 + 160 * index), line, font=font, fill="black")
    output = io.BytesIO()
    image.save(output, format="PDF", resolution=144)
    raw = output.getvalue()
    assert not any(page.extract_text().strip() for page in PdfReader(io.BytesIO(raw)).pages)
    return raw


def consume(rt, first, raw):
    overrides = rt.work / "overrides.json"
    overrides.write_text(
        json.dumps(
            {"overrides": [{"resource_id": first["resource_id"], "version_id": first["id"]}]}
        )
    )
    _, snap = rt.cli("snapshot", "create", "--version-overrides", overrides)
    sid = snap["snapshot_id"]
    rt.cli("snapshot", "publish", sid)
    mapping = rt.query("SELECT * FROM seal.snapshot_item WHERE snapshot_id=%s", (sid,))
    rt.check(
        "OCR historical version override freezes requested creator version",
        len(mapping) == 1 and mapping[0]["version_id"] == first["id"],
    )
    token, key, port = secrets.token_urlsafe(30), Fernet.generate_key().decode(), free_port()
    rt.protect(token, key)
    base = f"http://127.0.0.1:{port}"
    path = rt.work / "api.local.json"
    path.write_text(
        json.dumps(
            {
                "public_base_url": base,
                "token_key": key,
                "callers": [
                    {
                        "id": "ocr",
                        "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "sources": ["dd-032"],
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    proc = rt.spawn("serve", "--config", path, "--host", "127.0.0.1", "--port", str(port))

    def get(url):
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()

    url = (
        base
        + "/results?"
        + urllib.parse.urlencode({"source_url": rt.fixture.base + "/scanned.pdf"})
    )
    for _ in range(80):
        try:
            results = json.loads(get(url))
            break
        except OSError:
            if proc.poll() is not None:
                raise AssertionError("OCR consumer exited") from None
            time.sleep(0.25)
    rt.check(
        "real OCR text reaches historical public snapshot consumer",
        len(results) == 1
        and "SCAN RECEIPT" in results[0]["markdown"]
        and bool(results[0]["attachments"]),
    )
    attachment = get(results[0]["attachments"][0]["url"])
    rt.check("OCR consumer receives exact image-only PDF attachment", attachment == raw)
    rt.save(
        "consumption.json",
        {
            "snapshot_id": sid,
            "mapping": mapping,
            "results": results,
            "attachment_sha256": hashlib.sha256(attachment).hexdigest(),
        },
    )


def main():
    engine = shutil.which("tesseract")
    if not engine:
        raise RuntimeError("UNVERIFIED: tesseract is not installed")
    with Runtime("issue28-ocr") as rt:
        raw = scanned_pdf()
        (rt.work / "scan.pdf").write_bytes(raw)
        rt.fixture.pages["/scanned.pdf"] = (200, "application/pdf", raw)
        rt.cli("init")
        ocr = {
            "languages": "eng",
            "dpi": 144,
            "timeout_seconds": 30,
            "max_pages": 2,
            "max_page_pixels": 10000000,
            "max_result_bytes": 2000000,
        }
        config = rt.config("dd-032", ["/scanned.pdf"], ocr=ocr)
        rt.collect(config)
        first = rt.versions("dd-032")[-1]
        archive = rt.archive(first)
        rt.check(
            "image-only PDF actually uses real OCR method",
            archive["result"]["metadata"]["pdf"]["method"] == "ocr",
        )
        rt.check(
            "real engine recognizes fixture text",
            "SCAN RECEIPT" in str(archive["result"]["markdown"]),
        )
        rt.collect(config)
        rt.check("stable successful real OCR reuses V1", len(rt.versions("dd-032")) == 1)
        ocr["dpi"] = 192
        adjust(config, ocr=ocr)
        rt.collect(config)
        versions = rt.versions("dd-032")
        rt.check(
            "real OCR dpi semantic change versions identical source PDF",
            len(versions) == 2
            and versions[0]["body_hash"] == versions[1]["body_hash"]
            and versions[0]["parser_fingerprint"] != versions[1]["parser_fingerprint"],
        )
        rt.check(
            "new dpi version is genuinely OCR success",
            rt.archive(versions[-1])["result"]["metadata"]["pdf"]["method"] == "ocr",
        )
        rt.collect(config)
        rt.check(
            "stable changed dpi OCR never repeatedly versions", len(rt.versions("dd-032")) == 2
        )
        ocr.update(
            timeout_seconds=45, max_pages=3, max_page_pixels=18000000, max_result_bytes=3000000
        )
        adjust(config, ocr=ocr, budget={"requests": 70, "seconds": 90, "response_bytes": 3000000})
        rt.collect(config)
        rt.check(
            "OCR timeout and successful resource budgets do not create version",
            len(rt.versions("dd-032")) == 2,
        )
        consume(rt, first, raw)
        rt.save(
            "ocr-evidence.json",
            {
                "engine": subprocess.check_output([engine, "--version"], text=True).splitlines()[0],
                "engine_sha256": hashlib.sha256(Path(engine).read_bytes()).hexdigest(),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "command": "uv run --no-sync python tests/ocr_version_fingerprints.py",
                "fixture_sha256": hashlib.sha256(raw).hexdigest(),
                "text_layer_empty": True,
                "versions": rt.versions("dd-032"),
                "observations": rt.observations("dd-032"),
                "first_result": archive,
                "last_result": rt.archive(versions[-1]),
            },
        )
    rt.save("sensitive-evidence-audit.json", audit())


if __name__ == "__main__":
    main()
