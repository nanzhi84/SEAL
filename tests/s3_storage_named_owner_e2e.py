"""失败边界：数据库用户名与业务 schema 同名时，重复 init 不得重复建队列表。"""
import json
from pathlib import Path

from s3_storage_e2e import Lab, digest, write


def execute(lab):
    lab.sql("CREATE ROLE seal LOGIN")
    lab.sql("ALTER DATABASE seal_e2e OWNER TO seal")
    lab.dsn = lab.dsn.replace("postgresql://postgres@", "postgresql://seal@")
    lab.env["SEAL_DATABASE_URL"] = lab.dsn
    _, first = lab.cli("init")
    _, second = lab.cli("init")
    lab.check("same-name database owner initializes twice", first.get("status") == "initialized"
              and second.get("status") == "initialized", first=first, second=second)
    lab.check("second initialization applies no migrations", second.get("applied") == [], second=second)
    queues = lab.sql("SELECT schemaname,tablename FROM pg_tables WHERE tablename='procrastinate_jobs'")
    lab.check("actual queue table exists with one schema identity", len(queues) == 1, queues=queues)
    write(lab.artifacts / "queue-identity.json", queues)
    path = lab.source()
    lab.cli("collect", path, "--enqueue")
    lab.cli("worker", "--once")
    lab.check("same-name owner queue worker produces successful content versions", len(lab.versions()) >= 3)


if __name__ == "__main__":
    lab = Lab()
    try:
        lab.start()
        environment = json.loads((lab.artifacts / "environment.json").read_text())
        environment.update(command="uv run --no-sync python tests/s3_storage_named_owner_e2e.py",
                           extension_sha256=digest(Path(__file__).read_bytes()), database_owner="seal")
        write(lab.artifacts / "environment.json", environment)
        execute(lab)
    except Exception as exc:
        lab.check("named owner extension completes", False, category=type(exc).__name__, detail=lab.clean(str(exc)))
    finally:
        lab.close()
        summary = json.loads((lab.artifacts / "summary.json").read_text())
        summary["rerun"] = "uv run --no-sync python tests/s3_storage_named_owner_e2e.py"
        write(lab.artifacts / "summary.json", summary)
    raise SystemExit(1 if any(c["status"] == "FAIL" for c in lab.checks) else 0)
