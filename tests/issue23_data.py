"""Real native collector plus explicitly controlled external SQL boundary fixtures."""
from __future__ import annotations

import json

from issue22_data import Data as StorageFixture
from issue23_lab import digest, write


class Data(StorageFixture):
    def prepare(self):
        self.collect_initial()
        for ident in ("dd-002", "dd-003", "dd-013"):
            self.source(ident)
        self.source("dd-004", "https://fixture.example.org/ambiguous")
        self.source("dd-005", self.entries["dd-004"])
        self.alpha = []
        for n in range(7):
            row = self.success("dd-002", f"snapshot-{n:03}", self.envelope(f"Alpha resource {n}"))
            self.alpha.append(dict(row))
        self.attachment_bytes = b"Issue23 original attachment A\x00\xff\x01" * 64
        self.attachment_key = self.lab.put(self.attachment_bytes)
        self.attached_a = dict(self.success("dd-013", "attached-result", self.envelope(
            "Attachment Alpha", attachment_refs=[self.attachment_key])))
        write(self.lab.artifacts / "fixtures.json", dict(entries=self.entries, isolated=True,
              native_framework="Real seal collect HTTP HTML/PDF", controlled_sql="pagination/ambiguity/concurrency",
              attachment=dict(bytes=len(self.attachment_bytes), sha256=digest(self.attachment_bytes))))

    def payload(self, row):
        return json.loads(self.lab.body(row["result_ref"]))
