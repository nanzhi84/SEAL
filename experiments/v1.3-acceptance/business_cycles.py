"""Real frozen Recheck and zero-downloader Replay for accepted business runs."""

import argparse
import json
from pathlib import Path

from live import Driver, write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    driver = Driver(args.output, 3600)
    checks = []
    for directory in sorted(args.collect.iterdir()):
        if not directory.is_dir() or not (directory / "collect.json").exists():
            continue
        source = json.loads((directory / "source.json").read_text())["id"]
        original = json.loads((directory / "collect.json").read_text())
        if original["status"] != "complete":
            raise RuntimeError("Original business Collect is not complete: " + source)
        old_records = json.loads((directory / "export.json").read_text())["records"]
        expected = sorted((r["record_type"], r["record_key"]) for r in old_records)
        output = args.output / directory.name
        output.mkdir()
        for mode, command in [
            ("replay", ["replay", original["run_id"]]),
            ("recheck", ["run", source, "--recheck"]),
        ]:
            receipt = driver.cli(*command, required=False)
            write(output / (mode + "-receipt.json"), receipt)
            inspected = driver.cli("inspect", "run", receipt["run_id"])
            exported = driver.cli("export", source, "--run", receipt["run_id"])
            write(output / (mode + "-run.json"), inspected)
            write(output / (mode + "-export.json"), exported)
            actual = sorted((r["record_type"], r["record_key"]) for r in exported["records"])
            checks.append(
                {
                    "name": source + ":" + mode + ":complete",
                    "status": "PASS" if receipt["status"] == "complete" else "FAIL",
                }
            )
            checks.append(
                {
                    "name": source + ":" + mode + ":exact_business_identity_set",
                    "status": "PASS" if actual == expected else "FAIL",
                    "expected_count": len(expected),
                    "actual_count": len(actual),
                }
            )
            if mode == "replay":
                attempts = receipt["summary"]["counts"]["http_attempts"]
                checks.append(
                    {
                        "name": source + ":replay:zero_downloader_attempts",
                        "status": "PASS"
                        if attempts == 0 and inspected["observations"] == []
                        else "FAIL",
                        "attempts": attempts,
                    }
                )
            print(
                json.dumps(
                    {
                        "source": source,
                        "mode": mode,
                        "status": receipt["status"],
                        "records": len(actual),
                        "errors": receipt["errors"],
                    }
                ),
                flush=True,
            )
        write(args.output / "assertions.json", checks)
    result = {"assertions": len(checks), "failed": sum(c["status"] != "PASS" for c in checks)}
    write(args.output / "summary.json", result)
    print(json.dumps(result))
    return int(bool(result["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
