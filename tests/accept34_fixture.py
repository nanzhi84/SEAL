"""Pure controlled-source data and declarative Source configuration."""

import yaml


def pdf(n):
    text = f"BT /F1 12 Tf 50 750 Td (Legal original document {n}. Public rules and procedures.) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream",
    ]
    body = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objs, 1):
        offsets.append(len(body))
        body += str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    start = len(body)
    body += (
        b"xref\n0 6\n0000000000 65535 f \n"
        + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets[1:])
        + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return body


def config(t, name="source", requests=100, documents=100, parser=None):
    data = {
        "id": t.SOURCE,
        "adapter": "federal_register",
        "adapter_options": {"per_page": 2, "max_pages": 3},
        "entry_urls": [t.BASE + "/api/v1"],
        "allowed_hosts": ["127.0.0.1"],
        "allowed_path_prefixes": ["/"],
        "archive_approved": True,
        "scope": "isolated issue34 bounded public API fixture",
        "robots": False,
        "recheck_seconds": 1,
        "concurrency": 1,
        "delay": 0,
        "budget": {
            "requests": requests,
            "documents": documents,
            "seconds": 90,
            "response_bytes": 1000000,
        },
        "fetch": {"mode": "http", "max_depth": 1, "page_timeout_ms": 5000, "retries": 0},
        "metadata": {
            "source_name": "Acceptance Agency",
            "document_type": "法律法规",
            "language": "en",
        },
        "ocr": {"languages": "eng", "dpi": 96},
    }
    if t.MODE["inline"]:
        data["adapter_options"]["listing_body_field"] = "body"
    if parser:
        data["parser"] = parser
    p = t.WORK / (name + ".yaml")
    p.write_text(yaml.safe_dump(data, allow_unicode=True))
    t.save(name + "-config.json", data)
    return str(p)
