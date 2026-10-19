"""Independent, non-sensitive PDF/HTTP input fixtures for Issue 25 acceptance."""

import io
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def native_page(writer, text=""):
    page = writer.add_blank_page(width=612, height=792)
    if text:
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
        })
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 18 Tf 60 680 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    return page


def writer_bytes(writer):
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def image_page(lines):
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    latin = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 42)
    cjk = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 42)
    y = 180
    for line in lines:
        draw.text((110, y), line, fill="black", font=cjk if "测试" in line else latin)
        y += 110
    return image


def scanned_pdf(pages):
    images = [image_page(lines) for lines in pages]
    out = io.BytesIO()
    try:
        images[0].save(out, "PDF", resolution=150, save_all=True, append_images=images[1:])
        return out.getvalue()
    finally:
        for image in images:
            image.close()


def fixtures(folder):
    native = PdfWriter()
    native_page(native, "NATIVE FIRST PAGE ALPHA")
    native_page(native)
    native_page(native, "NATIVE THIRD PAGE OMEGA")
    scan = scanned_pdf([
        ["SCANNED FIRST PAGE ALPHA", "Văn bản thử nghiệm", "测试文档"],
        [],
        ["SCANNED THIRD PAGE OMEGA", "Processing page three"],
    ])
    scan_reader = PdfReader(io.BytesIO(scan))
    mixed = PdfWriter()
    native_page(mixed, "MIXED NATIVE FIRST PAGE")
    mixed.add_page(scan_reader.pages[0])
    numbered = PdfWriter()
    numbered.add_page(scan_reader.pages[0])
    overlay = PdfWriter()
    native_page(overlay, "1")
    numbered.pages[0].merge_page(overlay.pages[0])
    encrypted = PdfWriter()
    native_page(encrypted, "ENCRYPTED TEST DOCUMENT")
    encrypted.encrypt("acceptance-only-password")
    data = {
        "native": writer_bytes(native), "scan": scan,
        "redirect_scan": scan,
        "mixed": writer_bytes(mixed), "page_number_only": writer_bytes(numbered),
        "all_empty": scanned_pdf([[], []]), "corrupt": b"%PDF-1.7\ninvalid xref\n%%EOF",
        "encrypted": writer_bytes(encrypted), "zero_pages": writer_bytes(PdfWriter()),
    }
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in data.items():
        (folder / (name + ".pdf")).write_bytes(body)
    return data


class FixtureServer:
    def __init__(self, data):
        self.data = data
        self.requests = Counter()
        self.events = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                return

            def do_GET(self):
                fixture.requests[self.path] += 1
                fixture.events.append({"path": self.path, "at": time.monotonic()})
                if self.path == "/redirect_scan.pdf":
                    body = b"non-sensitive intermediate redirect attachment"
                    self.send_response(302)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Location", "/scan.pdf")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                key = self.path.rsplit("/", 1)[-1].removesuffix(".pdf")
                body = fixture.data.get(key)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Type", "application/pdf" if body else "text/plain")
                self.send_header("Content-Length", str(len(body or b"missing")))
                self.end_headers()
                self.wfile.write(body or b"missing")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
