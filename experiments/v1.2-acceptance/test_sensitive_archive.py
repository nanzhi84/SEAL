"""Archival contract failures and boundaries, specified before the Runtime fix.

False rejection: public null/undefined/empty fields and empty login inputs.
False acceptance: populated credential fields, HTTP headers or login inputs.
Boundary ambiguity: quoted null, null prefixes, escaping and truncated quotes.
Nested sensitive containers and reassigned input variables must not bypass the
guard; empty placeholders followed by operators are not proven empty values.
JavaScript getters and wrappers cannot prove an empty value because script
setters or shadowing can supply credentials. All dynamic expressions are rejected.
Every rejection must happen before writing a body/snapshot or SQL observation;
every accepted response must retain its exact bytes and SHA-256 identity.
"""

import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scrapy import Request
from scrapy.http import HtmlResponse
from sensitive_fixtures import REJECTED_BODIES, SAFE_BODY

from seal.archive import archive_response
from seal.core import Objects, SealError

SAFE = [
    SAFE_BODY,
    b'{"password":null,"access_token":"","api-key": null}',
    b"const login={password:undefined, secret:'', authorization: ''};",
    b"password=\r\n",
    b"password:'   '",
    b"<input name='password' type='password'>",
    b"<input type='password' value=''>",
    b"<input type='text' name='token' value=''>",
    b"password_hint:'Public login help'; public_token_count:10",
    b"<p>The public login field is empty.</p>",
    b'<input type="password" value="" value="">',
    b'<input id="ordinary" value=""><input id="other" value="Public name">',
    b'<textarea id="password"></textarea>',
]
REJECTED = [
    *REJECTED_BODIES.values(),
    # A getter exception was attempted and abandoned after independent review
    # found script setters/shadowing leaks. Its old green/red logs are retained.
    b'<script>var password = $("#emptyPassword").val(); send({password:password});</script>',
    b'<script>password: $.trim($("#emptyPassword").val())</script>',
    b'<script>var password = document.getElementById("emptyPassword").value; send({password:password});</script>',
    b'<script>var password = document.querySelector("#emptyPassword").value;</script>',
    b'<input id="ordinary" value=""><input id="other" value="Public name"><script>var password=$("#ordinary").val();</script>',
    b'<textarea id="ordinary"></textarea><script>password:document.getElementById("ordinary").value</script>',
    b'{"password":"null"}',
    b"password:nullish",
    b"password:null-private",
    b"password:0",
    b"password:false",
    b"password:' synthetic-private-value '",
    b'password:"synthetic\\"private-value"',
    b"PASSWORD = 'synthetic-private-value'",
    b"userPassword: 'synthetic-private-value'",
    b"client_secret=synthetic-private-value",
    b"api-key = synthetic-private-value",
    b"token='synthetic-private-value'",
    b"Set-Cookie: sid=synthetic-private-value; Secure",
    b"Proxy-Authorization: Basic synthetic-private-value",
    b"password:null\x00synthetic-private-value",
    b'password:"synthetic-private-value',
    b'password:"',
    b'<input name="PASSWORD" value="synthetic-private-value">',
    b'<input type="password" value="&#115;ynthetic-private-value">',
    b'<script>window["Token"]="synthetic-private-value";</script>',
    b'<script>window["Password"]="synthetic-private-value";</script>',
    b'<script>window["Authorization"]="synthetic-private-value";</script>',
    b"JSESSIONID=synthetic-private-value",
    b"<script>token=abc;</script>",
    b"<script>password:password;</script>",
    b'<script>var password=encrypt("synthetic-private-value");</script>',
    b'<script>var password=encrypt($("#emptyPassword").val(),"synthetic-private-value");</script>',
    b'<script>var password=$("#emptyPassword").val();password="synthetic-private-value";</script>',
    b'<script>var password=$("#emptyPassword").val();send({password:password});</script><input id="emptyPassword" type="password" value="synthetic-private-value">',
    b"authorization:{type:'Bearer',value:'synthetic-private-value'}",
    b"password:['synthetic-private-value']",
    b'<input type="hidden" id="token" value="synthetic-private-value">',
    b'<script>var pass=$("#emptyPassword").val();pass="synthetic-private-value";send({password:pass});</script>',
    b'<script>var pass=$("#emptyPassword").val();pass+="synthetic-private-value";send({password:pass});</script>',
    b'<script>password="" + "synthetic-private-value";</script>',
    b'<script>password=null || "synthetic-private-value";</script>',
    b'<script>password=undefined ?? "synthetic-private-value";</script>',
    b'<input type="password" value="synthetic-private-value" value="">',
    b'<input type="password" value="" value="synthetic-private-value">',
    b'<input type="password" type="text" value="synthetic-private-value">',
    b'<input id="ordinary" value="synthetic-private-value"><script>var password=document.getElementById("ordinary").value;</script>',
    b'<input class="ordinary" value="synthetic-private-value"><script>var password=$(".ordinary").val();</script>',
    b'<textarea id="ordinary">synthetic-private-value</textarea><script>password:document.getElementById("ordinary").value</script>',
    b'<select id="ordinary"><option value="synthetic-private-value">Public label</option></select><script>password:document.getElementById("ordinary").value</script>',
    b'<script>function disguise(v){return "synthetic-private-value";} var password=disguise($("#ordinary").val());</script>',
    b'<script>var password=Base.DesUtil.encrypt($.trim($("#ordinary").val()));</script>',
    b'<input id="ordinary" value="synthetic-private-value"><script>var password=$("#ordinary:focus").val();</script>',
    b'<input class="ordinary other" value="synthetic-private-value"><script>var password=$(".ordinary.other").val();</script>',
    b'<script>var pass=$("#ordinary").val();pass||="synthetic-private-value";send({password:pass});</script>',
    b'<script>var pass=$("#ordinary").val();pass??="synthetic-private-value";send({password:pass});</script>',
    b'<input id="ordinary" value=""><script>$("#ordinary").val("synthetic-private-value");var password=$("#ordinary").val();send({password:password});</script>',
    b'<input id="ordinary" value=""><script>document.getElementById("ordinary").value="synthetic-private-value";var password=document.getElementById("ordinary").value;send({password:password});</script>',
    b'<script>String=function(v){return "synthetic-private-value";};var password=String($("#ordinary").val());</script>',
]


class ArchiveSensitiveBehavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="seal-sensitive-archive-")
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"SEAL_ARCHIVE": str(self.archive)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.writes = []

        @contextmanager
        def connect():
            yield self

        self.database = patch("seal.archive.connect", connect)
        self.database.start()
        self.addCleanup(self.database.stop)
        stamp = datetime.now(timezone.utc).isoformat()
        self.request = Request(
            "https://example.org/public",
            meta={
                "seal_request_id": "observation",
                "seal_requested_at": stamp,
                "seal_received_at": stamp,
                "seal_chain_id": "chain",
            },
        )
        self.context = {"source_id": "source", "id": "run", "attempt_epoch": 1}

    def execute(self, sql, params):
        self.writes.append((sql, params))

    def response(self, body):
        return HtmlResponse(self.request.url, body=body, request=self.request, encoding="utf-8")

    def test_public_placeholders_preserve_raw_bytes_and_digest(self):
        for index, body in enumerate(SAFE):
            with self.subTest(case=index):
                snapshot_id, observation = archive_response(
                    self.context, self.request, self.response(body)
                )
                snapshot = Objects().json(snapshot_id)
                self.assertEqual(Objects().get(snapshot["body_hash"]), body)
                self.assertEqual(snapshot["body_hash"], hashlib.sha256(body).hexdigest())
                self.assertEqual(observation, "observation")

    def test_sensitive_values_are_rejected_before_any_archive_or_sql_write(self):
        for index, body in enumerate(REJECTED):
            with self.subTest(case=index):
                before_files = list(self.archive.rglob("*"))
                before_writes = list(self.writes)
                with self.assertRaisesRegex(SealError, "^sensitive_body_rejected$"):
                    archive_response(self.context, self.request, self.response(body))
                self.assertEqual(list(self.archive.rglob("*")), before_files)
                self.assertEqual(self.writes, before_writes)


if __name__ == "__main__":
    unittest.main()
