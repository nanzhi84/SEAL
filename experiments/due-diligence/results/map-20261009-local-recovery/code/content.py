"""Offline extraction of the explicitly reviewed public samples."""

from io import BytesIO

from inventory import ROOT, clean_url
from pypdf import PdfReader
from scrapy.http import HtmlResponse, Response


def response_for(row):
    path = (ROOT / row["raw_file"]).resolve()
    if not path.is_relative_to(ROOT / "experiments/due-diligence"):
        raise ValueError("archive_path_outside_experiment")
    body = path.read_bytes()
    args = {"encoding": row["encoding"]} if row["mime"] != "application/pdf" else {}
    cls = HtmlResponse if args else Response
    return cls(row["final_url"], body=body, headers={"Content-Type": row["mime"]}, **args)


def normalize(value):
    return " ".join(value.split())


def compact(value):
    return "".join(value.split())


def parse_content(response, spec):
    if spec["kind"] == "pdf":
        pages = [page.extract_text() or "" for page in PdfReader(BytesIO(response.body)).pages]
        return {"pages": pages}
    if spec["kind"] == "records":
        records = []
        for node in response.xpath(spec["record_xpath"]):
            row = {
                key: normalize(" ".join(node.xpath(xpath).getall()))
                for key, xpath in spec["fields"].items()
            }
            for key in spec.get("url_fields", []):
                if row[key]:
                    row[key] = clean_url(response.urljoin(row[key]))
            records.append(row)
        return {"records": records}
    return {
        "title": normalize(" ".join(response.xpath(spec["title_xpath"]).getall())),
        "body": normalize(" ".join(response.xpath(spec["body_xpath"]).getall())),
        "attachments": list(
            dict.fromkeys(
                clean_url(response.urljoin(url))
                for url in response.xpath(spec["attachment_xpath"]).getall()
            )
        ),
    }
