"""Public placeholders and synthetic credential values for archival boundaries."""

SAFE_BODY = b"""<html><h1>Public notice</h1><article>Public business body</article>
<script>const login = {password:null, secret:undefined, access_token:"",
authorization:'', api_key: null, password_hint:"Public help"};
const empty = {password: "   "};</script>
<input type="password" name="password" value="">
<input id="publicPassword" type="password" value="">
<input type="text" name="username" value="Public person"></html>"""

REJECTED_BODIES = {
    "password": b'<script>const login = {password:"synthetic-private-value"};</script>',
    "token": b'<script>const login = {refresh_token:"synthetic-private-value"};</script>',
    "authorization": b"Authorization: Bearer synthetic-private-value\r\n",
    "cookie": b"Cookie: sid=synthetic-private-value\r\n",
    "input": b'<input name="password" type="password" value="synthetic-private-value">',
}


def body_for(path, state):
    name = path.rsplit("/", 1)[-1]
    if name == "baseline":
        return SAFE_BODY if state != "sensitive" else SAFE_BODY + REJECTED_BODIES["password"]
    return REJECTED_BODIES.get(name, SAFE_BODY)
