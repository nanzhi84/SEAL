"""Small HTTP API fixtures for externally visible full-pagination contracts."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

API_PATH = "/portal/front/financial/fundTrustee/findFundTrusteesPage"


def trustees(total=25):
    return [
        {
            "id": index + 1,
            "trustName": f"Fixture trustee {index:02}",
            "regAddr": f"Region {index:02}",
        }
        for index in range(total)
    ]


class PaginationSite:
    def __init__(self):
        self.ledger, self.state, self.total = [], "base", 25
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                page, size = int(query.get("pageNo", [1])[0]), int(query.get("pageSize", [10])[0])
                rows = copy.deepcopy(trustees(owner.total))
                if owner.state == "reorder":
                    rows.reverse()
                selected = rows[(page - 1) * size : page * size]
                total = owner.total
                if owner.state == "total_changed" and page == 2:
                    total += 1
                elif owner.state == "early_empty" and page == 2:
                    selected = []
                elif owner.state == "short_tail" and page == 3:
                    selected.pop()
                elif owner.state == "duplicate" and page == 2:
                    selected[0]["trustName"] = rows[0]["trustName"]
                elif owner.state == "invalid_total":
                    total = str(total)
                envelope = {
                    "code": 200,
                    "data": {"errcode": 0, "data": {"total": total, "dataList": selected}},
                }
                if owner.state == "envelope_failed":
                    envelope["data"]["errcode"] = 1
                body = json.dumps(envelope).encode()
                owner.ledger.append(
                    {
                        "method": "GET",
                        "path": self.path,
                        "page": page,
                        "size": size,
                        "status": 200,
                        "rows": len(selected),
                        "reported_total": total,
                    }
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
