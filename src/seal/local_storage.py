"""Provision this checkout's isolated local services; never reset existing data."""

import json
import os
import secrets
import shlex
import subprocess
import sys
from pathlib import Path


def main():
    root = Path.cwd()
    if not (root / "compose.yaml").is_file():
        print(json.dumps({"error": "run_from_checkout_root"}))
        return 1
    config = root / ".env.local"
    if not config.exists():
        values = {
            "MINIO_ROOT_USER": "seal-admin",
            "MINIO_ROOT_PASSWORD": secrets.token_hex(24),
            "SEAL_S3_ENDPOINT": "http://127.0.0.1:19000",
            "SEAL_S3_BUCKET": "seal-archive",
            "SEAL_S3_ACCESS_KEY": "seal-archive-writer",
            "SEAL_S3_SECRET_KEY": secrets.token_hex(24),
            "SEAL_S3_REGION": "us-east-1",
            "SEAL_POSTGRES_PASSWORD": secrets.token_hex(24),
            "SEAL_WORK_DIR": str(root / "work" / "seal"),
        }
        values["SEAL_DATABASE_URL"] = (
            "postgresql://seal:" + values["SEAL_POSTGRES_PASSWORD"] + "@127.0.0.1:15432/seal"
        )
        with os.fdopen(os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            file.write(
                "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n"
            )
    command = ["docker", "compose", "--env-file", str(config)]
    # Compose enforces required settings. Keep its output out of receipts: it may
    # contain operator-supplied URLs or configuration details on failure.
    try:
        subprocess.run(
            [*command, "up", "-d", "--build", "--wait", "minio", "postgres"],
            cwd=root,
            check=True,
            timeout=900,
        )
        subprocess.run(
            [*command, "run", "--rm", "minio-init"],
            cwd=root,
            check=True,
            timeout=90,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print(json.dumps({"error": "local_storage_setup_failed"}))
        return 1
    print(
        json.dumps(
            {"status": "ready", "environment_file": str(config), "database_initialized": False}
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
