"""Public placeholders and synthetic credential values for archival boundaries."""

SAFE_BODY = b"""<html><h1>Public notice</h1><article>Public business body</article>
<script>const login = {password:null, secret:undefined, access_token:"",
authorization:'', api_key: null, password_hint:"Public help"};
const empty = {password: "   ", csrfToken: "", authToken: null,
xsrf_token: undefined, CSRF_TOKEN: '', auth_token: "   "};
const hints = {csrfTokenHint: "Public help", authTokenExpiry: 3600,
tokenizer: "Public parser", token_count: 10};</script>
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

# Common credential aliases share the same JSON/assignment and HTML-name boundary.
for index, key in enumerate(
    (
        "csrfToken",
        "authToken",
        "xsrfToken",
        "CSRF_TOKEN",
        "auth-token",
        "accessToken",
        "refreshToken",
        "apiKey",
    )
):
    REJECTED_BODIES[f"aliasjson{index}"] = ('{"' + key + '":"synthetic-private-value"}').encode()
    REJECTED_BODIES[f"aliasinput{index}"] = (
        '<input name="' + key + '" value="synthetic-private-value">'
    ).encode()
REJECTED_BODIES["aliasbracket"] = b'window["csrfToken"] = "synthetic-private-value";'
REJECTED_BODIES["aliastextarea"] = b'<textarea id="authToken">synthetic-private-value</textarea>'
REJECTED_BODIES["aliasselect"] = (
    b'<select name="xsrfToken"><option value="synthetic-private-value"></option></select>'
)


def body_for(path, state):
    name = path.rsplit("/", 1)[-1]
    if name == "baseline":
        return SAFE_BODY if state != "sensitive" else SAFE_BODY + REJECTED_BODIES["password"]
    return REJECTED_BODIES.get(name, SAFE_BODY)
