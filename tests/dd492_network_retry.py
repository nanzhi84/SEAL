"""One bounded official-URL retry for the fixed dd-492 network-failed sample."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import yaml
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    work, out = ROOT / "work" / args.name, ROOT / "artifacts" / args.name
    receipt_path = out / "network-retry.json"
    if receipt_path.exists():
        raise SystemExit("This run's single network retry was already attempted")
    manifest = json.loads((work / "selection.json").read_text())
    target = manifest["selected"][-1]
    env = dict(os.environ, **json.loads((work / "runtime.local.json").read_text()))
    with psycopg.connect(env["SEAL_DATABASE_URL"], row_factory=dict_row) as conn:
        row = conn.execute("SELECT url,outcome,error,version_id FROM seal.observation WHERE url=%s ORDER BY observed_at DESC LIMIT 1", (target["url"],)).fetchone()
    if row is None or row["error"] != "download_failed" or row["version_id"] is not None:
        raise SystemExit("Bounded retry requires this fixed sample's actual download_failed Observation")
    shutil.copyfile(out / "report.json", out / "first-attempt-report.json")
    config = yaml.safe_load((work / "dd-492.yaml").read_text())
    config.update(entry_urls=[target["url"]], concurrency=1)
    path = work / "dd-492-retry.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    (out / path.name).write_bytes(path.read_bytes())
    evidence = {"target": target["url"], "reason": "Only first-attempt download_failed; no OCR failure or sample replacement", "original_observation": row,
                "command": "uv run --no-sync python tests/dd492_network_retry.py --name " + args.name,
                "status": "started"}
    receipt_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps(evidence, ensure_ascii=False), flush=True)
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-m", "seal", "collect", str(path), "--no-progress"],
                          cwd=ROOT, env=env, capture_output=True, timeout=3750)
    (work / "retry.stdout.log").write_bytes(proc.stdout)
    (work / "retry.stderr.log").write_bytes(proc.stderr)
    receipts = []
    for line in proc.stdout.decode(errors="replace").splitlines():
        try:
            receipts.append(json.loads(line))
        except ValueError:
            pass
    evidence.update(status="finished", exit=proc.returncode, seconds=round(time.monotonic() - started, 3),
                    receipt=receipts[-1] if receipts else None)
    receipt_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps(evidence, ensure_ascii=False), flush=True)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
