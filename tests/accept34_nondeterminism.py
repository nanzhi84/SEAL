#!/usr/bin/env python3
"""Real scanned-PDF CLI flow with a stable OCR executable and changing business output."""

import hashlib
import shutil
import subprocess
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import accept34_e2e as t
import yaml
from PIL import Image, ImageDraw, ImageFont


def main():
    t.dbsetup()
    image = Image.new("RGB", (1400, 1800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 34)
    for i in range(20):
        draw.text(
            (70, 80 + i * 70),
            "All authorities shall follow public procedures and legal duties.",
            font=font,
            fill="black",
        )
    p = t.WORK / "scan.pdf"
    image.save(p, "PDF", resolution=120)
    data = p.read_bytes()
    state = t.WORK / "ocr-state.txt"
    state.write_text("alpha")
    real = shutil.which("tesseract")
    wrapper = t.WORK / "controlled-tesseract"
    log = t.WORK / "ocr-argv.jsonl"
    wrapper.write_text(f"""#!{sys.executable}
import json,pathlib,subprocess,sys
with pathlib.Path({str(log)!r}).open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
p=subprocess.run([{real!r},*sys.argv[1:]],capture_output=True)
replacement=b'procedures '+pathlib.Path({str(state)!r}).read_bytes().strip()
if len(sys.argv)>2 and not sys.argv[1].startswith('-'):
    base=pathlib.Path(sys.argv[2])
    for suffix in ('.txt','.hocr','.tsv','.alto.xml'):
        q=pathlib.Path(str(base)+suffix)
        if q.exists():q.write_bytes(q.read_bytes().replace(b'procedures',replacement))
    out=p.stdout.replace(b'procedures',replacement)
else:out=p.stdout
sys.stdout.buffer.write(out);sys.stderr.buffer.write(p.stderr);sys.exit(p.returncode)
""")
    wrapper.chmod(0o700)
    t.ENV["SEAL_TESSERACT"] = str(wrapper)

    class Origin(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    t.BASE = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        t.cmd("init", expected=[0])
        source = Path(t.config("scan"))
        cfg = yaml.safe_load(source.read_text())
        cfg.update(adapter="web", adapter_options={}, entry_urls=[t.BASE + "/scan.pdf"])
        source.write_text(yaml.safe_dump(cfg, allow_unicode=True))
        t.save("scan-source-config.json", cfg)
        first, rc = t.cmd("collect", str(source), "--no-progress")
        t.check(
            "scanned PDF baseline succeeds",
            rc == 0 and first.get("report", {}).get("observations", {}).get("created") == 1,
            first.get("report"),
        )
        before = t.query("SELECT * FROM seal.version ORDER BY id")
        t.save("before-versions.json", before)
        t.check("one baseline immutable version", len(before) == 1)
        state.write_text("beta")
        second, rc = t.cmd("collect", str(source), "--no-progress")
        t.save("second-run.json", second)
        conflicts = t.query("SELECT * FROM seal.delivery_conflict")
        t.save("conflicts.json", conflicts)
        t.save("observations.json", t.query("SELECT * FROM seal.observation"))
        t.check(
            "same input and parser different output conflicts",
            rc != 0 and any(c["reason"] == "nondeterministic_parser_output" for c in conflicts),
            {"returncode": rc, "conflicts": conflicts, "report": second.get("report")},
        )
        t.check(
            "nondeterminism does not overwrite success",
            t.query("SELECT * FROM seal.version ORDER BY id") == before,
        )
        t.check(
            "OCR parser manifest frozen",
            first.get("parser_manifest") == second.get("parser_manifest"),
        )
    except Exception as exc:
        t.save("exception.json", {"exception": str(exc), "traceback": traceback.format_exc()})
        t.check("nondeterminism harness completes", False, str(exc))
        traceback.print_exc()
    finally:
        if log.exists():
            shutil.copyfile(log, t.ART / "ocr-argv.jsonl")
        t.save(
            "provenance.json",
            {
                "head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=t.ROOT, text=True
                ).strip(),
                "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "support_sha256": hashlib.sha256(
                    (t.ROOT / "tests/accept34_e2e.py").read_bytes()
                ).hexdigest(),
                "source_hashes": t.SOURCE_AT_START,
                "tests_start_sha256": t.TESTS_AT_START,
                "source_end_hashes": {
                    str(p.relative_to(t.ROOT / "src")): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((t.ROOT / "src").rglob("*"))
                    if p.is_file() and p.suffix in (".py", ".sql")
                },
                "artifact": str(t.ART),
            },
        )
        t.save("assertions.json", t.ASSERTIONS)
        print("ARTIFACT", t.ART, flush=True)


if __name__ == "__main__":
    main()
    sys.exit(any(not a["passed"] for a in t.ASSERTIONS))
