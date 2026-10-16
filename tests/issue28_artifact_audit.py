"""Redact historical Issue #28 evidence and verify it contains no capabilities.

Run independently: uv run --no-sync python tests/issue28_artifact_audit.py
Only artifacts/issue28* text evidence is rewritten. Private synthetic credential
registries, if present, are read exclusively from work/issue28*/.
"""

import json
import re

from issue28_runtime import ROOT, redact


def audit():
    known = []
    for path in (ROOT / "work").glob("issue28*/.secrets.local.json"):
        known.extend(json.loads(path.read_text()))
    paths = [
        path
        for folder in (ROOT / "artifacts").glob("issue28*")
        if folder.is_dir()
        for path in folder.rglob("*")
        if path.is_file()
    ]
    changed = []
    for path in paths:
        original = path.read_text()
        cleaned = redact(original)
        for secret in known:
            cleaned = cleaned.replace(secret, "[REDACTED_SECRET]")
        if cleaned != original:
            path.write_text(cleaned)
            changed.append(str(path.relative_to(ROOT)))
    patterns = {
        "fernet_capabilities": r"gAAAA[A-Za-z0-9_=%-]{50,}",
        "attachment_capabilities": r"/attachments/[A-Za-z0-9_=%-]{40,}",
        "cursor_capabilities": r"[?&]cursor=[A-Za-z0-9_=%-]{40,}",
        "bearer_credentials": r"Bearer\s+[A-Za-z0-9._-]{20,}",
        "database_credentials": r"postgres(?:ql)?://[^\s/\"\\]+:[^\s@\"\\]+@",
        "synthetic_access_keys": r"issue28[0-9a-f]{10}",
        "labelled_secrets": r"(?:MINIO_ROOT_PASSWORD|SEAL_S3_SECRET_KEY|token_key)[\"\\\s:=]+[A-Za-z0-9_=-]{16,}",
    }
    matches = {name: 0 for name in patterns}
    matches["known_exact_random_credentials"] = 0
    for path in paths:
        payload = path.read_text()
        for name, pattern in patterns.items():
            matches[name] += len(re.findall(pattern, payload))
        matches["known_exact_random_credentials"] += sum(payload.count(secret) for secret in known)
    report = {
        "pass": not any(matches.values()),
        "files_scanned": len(paths),
        "known_private_credential_values": len(known),
        "matches": matches,
        "redacted_files": changed,
    }
    if not report["pass"]:
        raise AssertionError("sensitive evidence audit failed: " + json.dumps(matches))
    return report


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2))
