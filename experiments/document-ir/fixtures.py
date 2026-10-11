"""Synthetic public HTTP resources for the document CLI acceptance."""

import json
from io import BytesIO

from openpyxl import Workbook
from PIL import Image


def install(h):
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    workbook = Workbook()
    workbook.active.title = "First"
    workbook.active.append(["Name", "Value"])
    workbook.active.append(["A", 1])
    workbook.create_sheet("Second").append(["B", 2])
    stream = BytesIO()
    workbook.save(stream)
    picture = BytesIO()
    Image.new("RGB", (1, 1), "white").save(picture, format="PNG")
    fixtures = {
        "/ir/index": (
            "text/html",
            b'<html><body><h1>Resources</h1><a href="/ir/article">Article</a><a href="/ir/data.csv">CSV</a><a href="/ir/data.json">JSON</a><a href="/ir/note.txt">Text</a><a href="/ir/picture.png">Image</a><a href="/ir/bad.png">Bad image</a><a href="/ir/bad.webp">Wrong format</a><a href="/ir/bad.pdf">Bad PDF</a></body></html>',
        ),
        "/ir/article": (
            "text/html",
            b'<html><article><h1>Public article</h1><p>Archived public content.</p><img src="/ir/picture.png" alt="Synthetic picture"><a href="/ir/data.csv">Download table</a><table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>1</td></tr></table></article></html>',
        ),
        "/ir/data.csv": ("text/csv", b"name,value\nA,1\nB,\n"),
        "/ir/data.json": ("application/json", b'[1,null,{"ok":true}]'),
        "/ir/note.txt": ("text/plain", b"A public note\nsecond line"),
        "/ir/picture.png": ("image/png", picture.getvalue()),
        "/ir/bad.png": ("image/png", picture.getvalue()[:24]),
        "/ir/bad.webp": ("image/webp", b"RIFF\x08\x00\x00\x00WAVEfmt "),
        "/ir/bad.pdf": ("application/pdf", b"damaged synthetic PDF"),
        "/ir/pdf": ("application/pdf", h.site.pdf),
        "/ir/docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            h.site.docx,
        ),
        "/ir/xls": ("application/vnd.ms-excel", h.site.xls),
        "/ir/xlsx": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            stream.getvalue(),
        ),
        "/ir/null": ("application/json", b"null"),
        "/ir/scalar": ("application/json", b"42"),
        "/ir/object": ("application/json", b'{"ok":true,"count":1}'),
        "/ir/fields": (
            "application/json",
            json.dumps(
                {
                    "id": "one",
                    "data": {
                        "Cơ quan ban hành:": "Public agency",
                        "Năm ban hành:": 2026,
                        "Lĩnh vực:": None,
                        "local": "kept",
                    },
                },
                ensure_ascii=False,
            ).encode(),
        ),
        "/ir/sitemap.xml": (
            "application/xml",
            b"<urlset><url><loc>https://example.org/</loc></url></urlset>",
        ),
        "/ir/nav-list": (
            "text/html",
            b'<html><body><h1>Resources</h1><nav><a href="/ir/note.txt">Only link</a></nav></body></html>',
        ),
        "/ir/base": (
            "text/html",
            b'<html><head><base href="/ir/assets/"></head><body><article><p>Base example</p><img src="picture.png"><a href="data.csv">Attachment</a></article></body></html>',
        ),
        "/ir/assets/data.csv": ("text/csv", b"name,value\nBase,1\n"),
        "/ir/error": ("text/html", b"<html><h1>Access denied</h1></html>"),
        "/ir/empty": ("text/html", b"<html><body></body></html>"),
    }
    h.fixtures = fixtures

    def get(request):
        item = fixtures.get(request.path)
        if item is None:
            return original(request)
        mime, body = item
        h.site.ledger.append({"method": "GET", "path": request.path, "status": 200})
        request.send_response(200)
        request.send_header("Content-Type", mime)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = get

    def post(request):
        if request.path != "/ir/post":
            return original(request)
        from urllib.parse import parse_qs

        body = request.rfile.read(int(request.headers["Content-Length"]))
        page = parse_qs(body.decode())["page"][0]
        data = json.dumps({"page": int(page)}).encode()
        h.site.ledger.append({"method": "POST", "path": request.path, "status": 200})
        request.send_response(200)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    handler.do_POST = post
