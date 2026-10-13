"""存储切换的独立重启与真实 rendered_dom 扩展；不运行历史整套回归。"""
import json
import time

import boto3
import yaml
from botocore.config import Config
from pathlib import Path

from s3_storage_e2e import Lab, digest, write


def execute(lab):
    lab.cli("init")
    original = b'''<!doctype html><html><head><title>DOM acceptance</title></head><body>
<main><h1>Authorized browser fixture</h1><p id="result">Initial placeholder</p></main>
<script>document.getElementById('result').textContent='RENDERED_DYNAMIC_MARKER_20261013';</script>
</body></html>'''
    lab.fixture.set("/dynamic", body=original)
    path = lab.source(paths=("/dynamic",))
    config = yaml.safe_load(path.read_text())
    config["crawl4ai"].update(mode="rendered_dom", max_depth=0, wait_until="load")
    path.write_text(yaml.safe_dump(config))
    (lab.artifacts / "dd-017.yaml").write_text(path.read_text())
    _, receipt = lab.cli("collect", path, "--no-progress")
    rows = lab.versions()
    lab.check("real rendered_dom creates one native result version", len(rows) == 1, receipt=receipt)
    if not rows:
        return
    row = rows[0]
    lab.check("SQL evidence records rendered DOM input mode", row["input_mode"] == "rendered_dom")
    captured = lab.body(row["body_hash"])
    text = captured.decode()
    # Marker must appear in actual visible DOM, not just script source.
    lab.check("S3 captured post-JavaScript DOM", '<p id="result">RENDERED_DYNAMIC_MARKER_20261013</p>' in text
              and captured != original and digest(captured) == row["body_hash"])
    observations = lab.sql("SELECT * FROM seal.observation WHERE version_id=%s", (row["id"],))
    lab.check("SQL observation/version points to same durable DOM", len(observations) == 1
              and observations[0]["archive_ref"] == row["body_hash"]
              and observations[0]["body_hash"] == row["body_hash"])
    (lab.artifacts / "captured-dom.html").write_bytes(captured)
    write(lab.artifacts / "dom-version.json", row)
    _, reconstructed = lab.reconstruct()
    native = reconstructed.get("results", [])
    lab.check("new process reconstructs native DOM result", len(native) == 1
              and '<p id="result">RENDERED_DYNAMIC_MARKER_20261013</p>' in native[0]["html"]
              and "RENDERED_DYNAMIC_MARKER_20261013" in native[0]["markdown"])
    keys = lab.objects()
    hashes = {key: digest(lab.body(key)) for key in keys}
    before = lab.versions()
    lab.docker("restart", "--time", "2", lab.minio)
    # Docker's dynamically allocated published port may change on restart.
    endpoint = "http://127.0.0.1:" + lab.docker("port", lab.minio, "9000").split(":")[-1]
    lab.env["SEAL_S3_ENDPOINT"] = endpoint
    lab.s3.close()
    lab.s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=lab.access,
                          aws_secret_access_key=lab.secret, region_name="us-east-1",
                          config=Config(retries={"max_attempts": 0}, connect_timeout=2,
                                        read_timeout=3, s3={"addressing_style": "path"}))
    deadline = time.monotonic() + 40
    while True:
        try:
            lab.s3.head_bucket(Bucket=lab.bucket)
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(.2)
    lab.check("MinIO restart preserves complete objects and hashes", lab.objects() == keys
              and {key: digest(lab.body(key)) for key in keys} == hashes)
    lab.check("MinIO restart preserves SQL versions and references", lab.versions() == before)
    _, restored = lab.reconstruct()
    lab.check("fresh downstream process restores result after MinIO restart", restored == reconstructed)


if __name__ == "__main__":
    lab = Lab()
    try:
        lab.start()
        environment = json.loads((lab.artifacts / "environment.json").read_text())
        environment.update(command="uv run --no-sync python tests/s3_storage_restart_dom_e2e.py",
                           extension_sha256=digest(Path(__file__).read_bytes()),
                           browser_precondition="Installed Playwright Chromium; actual browser CLI execution asserted")
        write(lab.artifacts / "environment.json", environment)
        execute(lab)
    except Exception as exc:
        lab.check("restart DOM extension completes", False, category=type(exc).__name__, detail=lab.clean(str(exc)))
    finally:
        lab.close()
        summary = json.loads((lab.artifacts / "summary.json").read_text())
        summary["rerun"] = "uv run --no-sync python tests/s3_storage_restart_dom_e2e.py"
        write(lab.artifacts / "summary.json", summary)
    raise SystemExit(1 if any(c["status"] == "FAIL" for c in lab.checks) else 0)
