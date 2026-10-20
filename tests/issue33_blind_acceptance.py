"""Fresh issue #33 API-only acceptance.

Preconditions: caller supplies PostgreSQL/S3 connection settings with isolated database/bucket
creation rights; project dependencies are installed in this interpreter. OCR uses caller
TESSDATA_PREFIX or the engine default. SEAL_TEST_ENV_FILE optionally names a POSIX shell file.
Secrets remain in process environment/work only. No personal environment file is implicit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import boto3
import psycopg
import yaml
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
WORK = ROOT / "work/issue33-api-blind"
ART = ROOT / "artifacts/issue33-api-blind"


def load_env():
    env = dict(os.environ)
    if filename := env.get("SEAL_TEST_ENV_FILE"):
        path = Path(filename).expanduser().resolve(strict=True)
        raw = subprocess.run(
            ["/bin/sh", "-c", 'set -a; . "$1"; env -0', "seal-test-env", str(path)],
            capture_output=True,
            check=True,
            env=env,
        ).stdout
        env.update(dict(s.decode().split("=", 1) for s in raw.split(b"\0") if b"=" in s))
    required = ("SEAL_DATABASE_URL", "SEAL_S3_ENDPOINT", "SEAL_S3_ACCESS_KEY", "SEAL_S3_SECRET_KEY")
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise RuntimeError("missing_test_environment:" + ",".join(missing))
    env.update(PYTHONPATH=str(ROOT / "src"), SEAL_ALLOW_LOOPBACK="1")
    return env


def reproduction_command(script, *args):
    return shlex.join([sys.executable, str(ROOT / "tests" / script), *args])


def setup(phase):
    WORK.mkdir(parents=True, exist_ok=True)
    env = load_env()
    state = WORK / f"{phase}-state.json"
    if state.exists():
        cfg = json.loads(state.read_text())
    else:
        suffix = uuid.uuid4().hex[:12]
        cfg = {
            "database": f"seal_accept33_{phase}_{suffix}",
            "bucket": f"seal-accept33-{phase}-{suffix}",
        }
        p = urlsplit(env["SEAL_DATABASE_URL"])
        admin = urlunsplit((p.scheme, p.netloc, "/postgres", p.query, p.fragment))
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cfg["database"])))
        s3 = boto3.client(
            "s3",
            endpoint_url=env["SEAL_S3_ENDPOINT"],
            aws_access_key_id=env["SEAL_S3_ACCESS_KEY"],
            aws_secret_access_key=env["SEAL_S3_SECRET_KEY"],
            region_name=env.get("SEAL_S3_REGION", "us-east-1"),
        )
        s3.create_bucket(Bucket=cfg["bucket"])
        state.write_text(json.dumps(cfg))
    p = urlsplit(env["SEAL_DATABASE_URL"])
    env.update(
        SEAL_DATABASE_URL=urlunsplit(
            (p.scheme, p.netloc, "/" + cfg["database"], p.query, p.fragment)
        ),
        SEAL_S3_BUCKET=cfg["bucket"],
        SEAL_WORK_DIR=str(WORK / phase / "runtime"),
    )
    (ART / phase).mkdir(parents=True, exist_ok=True)
    (WORK / phase).mkdir(parents=True, exist_ok=True)
    return env


def cli(env, args, label, phase, timeout=1800):
    cmd = [str(PYTHON), "-m", "seal", *args]
    start = time.monotonic()
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    out = ART / phase / f"{label}.json"
    try:
        data = json.loads(r.stdout)
    except Exception:
        data = {"stdout": r.stdout}
    record = {
        "argv": cmd,
        "returncode": r.returncode,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "result": data,
        "stderr_tail": r.stderr[-3000:],
    }
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "label": label,
                "returncode": r.returncode,
                "status": data.get("status"),
                "error": data.get("error"),
                "run_id": data.get("run_id"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return data, r.returncode


def freeze_configs():
    for sid in ("dd-492", "dd-499"):
        cfg = yaml.safe_load((ROOT / f"sources/{sid}.yaml").read_text())
        if sid == "dd-492":
            cfg["adapter_options"]["max_pages"] = 2
            cfg["budget"].update(documents=3, requests=40, seconds=900)
            cfg["concurrency"] = 2
        else:
            cfg["adapter_options"].update(per_page=2, max_pages=1)
            cfg["budget"].update(documents=2, requests=20, seconds=600)
        dest = WORK / "real" / f"{sid}.yaml"
        dest.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
        shutil.copy2(dest, ART / "real" / f"{sid}.yaml")


def audit_sql(env, phase, label):
    queries = {
        "sources": "SELECT id FROM seal.source ORDER BY id",
        "runs": "SELECT id,source_id,status,errors FROM seal.run ORDER BY created_at",
        "resources": "SELECT id,frontier_id,identity_namespace,external_key,public_url,current_version_id FROM seal.resource ORDER BY external_key",
        "versions": "SELECT id,resource_id,result_ref,inputs_ref,input_fingerprint,parser_fingerprint,content_hash FROM seal.version ORDER BY id",
        "acquisitions": "SELECT id,run_id,http_method,request_url,actual_url,acquisition_kind,state,outcome,http_status,body_hash,archive_ref FROM seal.acquisition ORDER BY started_at",
        "frontiers": "SELECT id,source_id,checkpoint FROM seal.frontier ORDER BY id",
    }
    result = {}
    with psycopg.connect(env["SEAL_DATABASE_URL"], row_factory=dict_row) as c:
        for name, q in queries.items():
            try:
                with c.transaction():
                    result[name] = c.execute(q).fetchall()
            except Exception as e:
                result[name] = {"error": str(e).splitlines()[0]}
    (ART / phase / f"{label}-sql.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str)
    )
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "mode",
        choices=[
            "prepare",
            "real",
            "audit-real",
            "controlled",
            "consume-real",
            "consume-controlled",
        ],
    )
    p.add_argument("--fresh", action="store_true")
    args = p.parse_args()
    if args.mode == "prepare":
        from issue33_blind_api import source_hashes

        ART.mkdir(parents=True, exist_ok=True)
        (ART / "frozen-source-hashes-start.json").write_text(json.dumps(source_hashes(), indent=2))
        return
    if args.fresh:
        phase = "controlled" if args.mode in ["controlled", "consume-controlled"] else "real"
        (WORK / f"{phase}-state.json").unlink(missing_ok=True)
    if args.mode == "controlled":
        return controlled()
    if args.mode == "consume-controlled":
        return api_consumption(setup("controlled"), "controlled")
    env = setup("real")
    if args.mode == "consume-real":
        return api_consumption(env, "real")
    if args.mode == "real":
        cli(env, ["init"], "init", "real")
        freeze_configs()
        for sid in ("dd-492", "dd-499"):
            cli(
                env,
                ["collect", str(WORK / "real" / f"{sid}.yaml"), "--no-progress"],
                f"{sid}-collect",
                "real",
            )
        audit_sql(env, "real", "after-collect")
        return api_consumption(env, "real")
    else:
        audit_sql(env, "real", "after-collect")


def query(env, statement, params=()):
    with psycopg.connect(env["SEAL_DATABASE_URL"], row_factory=dict_row) as c:
        return c.execute(statement, params).fetchall()


def s3_client(env):
    return boto3.client(
        "s3",
        endpoint_url=env["SEAL_S3_ENDPOINT"],
        aws_access_key_id=env["SEAL_S3_ACCESS_KEY"],
        aws_secret_access_key=env["SEAL_S3_SECRET_KEY"],
        region_name=env.get("SEAL_S3_REGION", "us-east-1"),
    )


def object_bytes(env, key):
    body = s3_client(env).get_object(Bucket=env["SEAL_S3_BUCKET"], Key=key)["Body"].read()
    assert hashlib.sha256(body).hexdigest() == key, ("CAS_hash", key)
    return body


def resource_state(env):
    return {
        r["external_key"]: r
        for r in query(
            env,
            "SELECT r.id,r.external_key,r.public_url,r.current_version_id,count(v.id) AS versions FROM seal.resource r LEFT JOIN seal.version v ON v.resource_id=r.id GROUP BY r.id ORDER BY r.external_key",
        )
    }


def controlled():
    from issue33_blind_fixture import Fixture

    phase = "controlled"
    env = setup(phase)
    cli(env, ["init"], "init", phase)
    fixture = Fixture()
    checks = []
    try:
        configs = {}
        for sid, adapter, entry in [
            ("dd-492", "vietnam_law", fixture.base + "/vn/"),
            ("dd-499", "federal_register", fixture.base + "/api"),
        ]:
            cfg = {
                "id": sid,
                "adapter": adapter,
                "entry_urls": [entry],
                "allowed_hosts": ["127.0.0.1"],
                "allowed_path_prefixes": ["/"],
                "archive_approved": True,
                "scope": "Isolated complete legal-text acceptance fixture; not public historical coverage.",
                "robots": False,
                "recheck_seconds": 1,
                "concurrency": 2,
                "delay": 0.0,
                "budget": {
                    "requests": 50,
                    "seconds": 120,
                    "response_bytes": 10485760,
                    "documents": 10,
                },
                "fetch": {"mode": "http", "max_depth": 2, "page_timeout_ms": 30000, "retries": 0},
                "metadata": {
                    "source_name": "Independent issue33 acceptance fixture",
                    "document_type": "法律法规",
                    "language": "vi" if sid == "dd-492" else "en",
                    "jurisdiction": "VN" if sid == "dd-492" else "US",
                },
            }
            cfg["adapter_options"] = (
                {"max_pages": 2}
                if sid == "dd-492"
                else {"per_page": 2, "max_pages": 1, "listing_body_field": "body"}
            )
            if sid == "dd-492":
                cfg["documents"] = {"detail_body": "never", "attachment_title": "link_text"}
            path = WORK / phase / f"{sid}.yaml"
            path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
            shutil.copy2(path, ART / phase / f"{sid}.yaml")
            configs[sid] = path
        for sid in configs:
            data, rc = cli(
                env, ["collect", str(configs[sid]), "--no-progress"], f"{sid}-baseline", phase
            )
            checks.append(
                {
                    "check": sid + " baseline successful",
                    "passed": rc == 0,
                    "status": data.get("status"),
                }
            )
        before = resource_state(env)
        for r in query(
            env,
            "SELECT r.external_key,v.result_ref FROM seal.resource r JOIN seal.version v ON v.id=r.current_version_id WHERE r.external_key IN ('9001','9002')",
        ):
            doc = json.loads(object_bytes(env, r["result_ref"]))
            if r["external_key"] == "9001":
                ds = doc["deliverables"]
                ok = (
                    sorted(d["volume_order"] for d in ds if d["role"] == "volume") == [1, 2]
                    and sum(d["role"] == "independent_attachment" for d in ds) == 1
                )
                checks.append(
                    {"check": "ordered 1,2 volumes plus independent attachment", "passed": ok}
                )
            else:
                md = doc["content"]["markdown"]
                ok = (
                    "CHÍNH PHỦ" in md
                    and "Điều 1." in md
                    and "Ban hành quy định" in md
                    and any(
                        d.get("legacy_encoding") == "tcvn3"
                        for d in doc["extraction"]["deliverables"]
                    )
                )
                checks.append(
                    {
                        "check": "mixed TCVN3 recovery preserves Unicode header",
                        "passed": ok,
                        "markdown": md,
                    }
                )
        (ART / phase / "baseline-resources.json").write_text(
            json.dumps(before, indent=2, default=str)
        )
        for sid in configs:
            cli(env, ["collect", str(configs[sid]), "--no-progress"], f"{sid}-repeat", phase)
        repeat = resource_state(env)
        checks.append(
            {"check": "repeat preserves resource IDs and versions", "passed": before == repeat}
        )
        fixture.api_variant = 1
        cli(
            env,
            ["collect", str(configs["dd-499"]), "--no-progress"],
            "dd-499-reordered-unrelated-change",
            phase,
        )
        after = resource_state(env)
        for key in ["2026-90001", "2026-90002"]:
            ok = after[key]["id"] == before[key]["id"] and after[key]["versions"] == before[key][
                "versions"
            ] + (key == "2026-90002")
            checks.append(
                {
                    "check": "shared JSON identity/version " + key,
                    "passed": ok,
                    "before": before[key],
                    "after": after[key],
                }
            )
        checks.append(
            {
                "check": "no public_url supported",
                "passed": all(
                    before[k]["public_url"] is None for k in ["2026-90001", "2026-90002"]
                ),
            }
        )
        for mode in ["missing", "quality", "ambiguous", "structure", "out-of-scope", "challenge"]:
            fixture.mode = mode
            prior = resource_state(env)
            data, rc = cli(
                env,
                ["collect", str(configs["dd-492"]), "--no-progress"],
                "dd-492-failure-" + mode,
                phase,
            )
            current = resource_state(env)
            versions_same = all(current[k]["versions"] == v["versions"] for k, v in prior.items())
            checks.append(
                {
                    "check": "failure explicit and no partial Version: " + mode,
                    "passed": rc != 0 and versions_same,
                    "status": data.get("status"),
                    "errors": data.get("errors"),
                    "versions_same": versions_same,
                }
            )
        fixture.mode = "api-pagination"
        data, rc = cli(
            env, ["collect", str(configs["dd-499"]), "--no-progress"], "dd-499-page-limit", phase
        )
        coverage = data.get("report", {}).get("coverage", {})
        checks.append(
            {
                "check": "next_page beyond max_pages reports incomplete enumeration",
                "passed": coverage.get("selected_scope_enumeration") == "incomplete",
                "coverage": coverage,
            }
        )
        for mode in ["api-quality", "api-missing-body", "api-structure"]:
            fixture.mode = mode
            prior = resource_state(env)
            data, rc = cli(
                env,
                ["collect", str(configs["dd-499"]), "--no-progress"],
                "dd-499-failure-" + mode,
                phase,
            )
            current = resource_state(env)
            versions_same = all(current[k]["versions"] == v["versions"] for k, v in prior.items())
            checks.append(
                {
                    "check": "API failure explicit and no partial Version: " + mode,
                    "passed": rc != 0 and versions_same,
                    "status": data.get("status"),
                    "errors": data.get("errors"),
                    "versions_same": versions_same,
                }
            )
        fixture.mode = "normal"
        queued, rc = cli(
            env, ["collect", str(configs["dd-499"]), "--enqueue"], "dd-499-worker-enqueue", phase
        )
        cli(env, ["worker", "--once"], "dd-499-worker-once", phase)
        worker_run = query(env, "SELECT status FROM seal.run WHERE id=%s", (queued["run_id"],))[0]
        checks.append(
            {
                "check": "enqueued official CLI worker completes persisted run",
                "passed": worker_run["status"] == "complete",
                "status": worker_run["status"],
            }
        )
        (ART / phase / "fixture-http-events.json").write_text(
            json.dumps(fixture.events, ensure_ascii=False, indent=2)
        )
        audit_sql(env, phase, "after-controlled")
        (ART / phase / "assertions.json").write_text(
            json.dumps(checks, ensure_ascii=False, indent=2, default=str)
        )
        print(json.dumps(checks, ensure_ascii=False, default=str), flush=True)
        api_consumption(env, phase)
        assert all(c["passed"] for c in checks), "business_assertion_failed; see assertions.json"
    finally:
        fixture.close()


def api_consumption(env, phase):
    from issue33_blind_api import consume

    return consume(env, phase)


if __name__ == "__main__":
    main()
