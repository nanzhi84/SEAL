"""Private public-contract configuration validation and environment entry path."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import time

from issue22_contract import results
from issue22_lab import ROOT, free_port, write


def configuration(lab, data):
    lab.start_service(via_environment=True)
    status, _, values, _, _ = lab.request(results(data, "dd-003"))
    lab.check("SEAL_API_CONFIG env entry runs same authorized public API", status == 200 and values == [])
    base = copy.deepcopy(lab.service_config)
    lab.stop_service()
    cases = [("unknown field", {"unrecognized": True}), ("invalid Fernet key", {"token_key": "invalid"}),
             ("workers below1", {"workers": 0}), ("workers above8", {"workers": 9}),
             ("workers bool", {"workers": True}), ("timeout below1", {"request_timeout_seconds": 0}),
             ("timeout above60", {"request_timeout_seconds": 61}),
             ("result below1024", {"result_max_bytes": 1023}),
             ("result above67108864", {"result_max_bytes": 67108865}),
             ("attachment below1024", {"attachment_max_bytes": 1023}),
             ("attachment above52428800", {"attachment_max_bytes": 52428801}),
             ("page below1024", {"page_max_bytes": 1023}),
             ("page above134217728", {"page_max_bytes": 134217729}),
             ("wildcard Source", {"callers": [{**base["callers"][0], "sources": ["*"]}]}),
             ("invalid key digest", {"callers": [{**base["callers"][0], "key_sha256": "bad"}]}),
             ("caller extra field", {"callers": [{**base["callers"][0], "unrecognized": True}]}),
             ("public base has secret URL credentials", {"public_base_url": "https://name:private@example.org"})]
    for number, (label, overrides) in enumerate(cases):
        config = {**copy.deepcopy(base), **overrides}
        path = lab.work / f"invalid-config-{number}.private.json"
        write(path, config)
        path.chmod(0o600)
        command = [sys.executable, "-m", "seal.cli", "serve", "--config", str(path),
                   "--port", str(free_port())]
        started = time.monotonic()
        try:
            p = subprocess.run(command, env=lab.env, cwd=ROOT, capture_output=True, text=True, timeout=5)
            parsed = json.loads(p.stdout)
            lab.check("strict private config rejection " + label,
                      p.returncode != 0 and isinstance(parsed, dict) and isinstance(parsed.get("error"), str)
                      and not any(secret in p.stdout + p.stderr for secret in lab.secrets)
                      and "Traceback" not in p.stdout + p.stderr,
                      exit=p.returncode, body=parsed, elapsed=time.monotonic() - started)
        except subprocess.TimeoutExpired:
            lab.check("strict private config rejection " + label, False, reason="Invalid config server kept running")
    lab.start_service()
