"""Bounded live CLI acceptance; retain failures and unknown coverage explicitly."""

import argparse
import json
import subprocess
import sys

import yaml
from harness import BASE, Harness, write


def main(name):
    h = Harness(name)
    h.scope = "Bounded live public sources, isolated PostgreSQL and private archive; full-site coverage UNKNOWN"
    h.command = f"uv run --frozen python experiments/document-only/live.py --name {name}"
    error = None
    results = []
    try:
        h.start()
        h.cli("init")
        for source in yaml.safe_load((BASE / "config/live.yaml").read_text())["sources"]:
            source["recipe"]["path"] = str((BASE / "config" / source["recipe"]["path"]).resolve())
            config = h.output / "config" / f"{source['id']}.yaml"
            write(config, source)
            # A source can fail at HTTP/policy/format; retain its actual outcome.
            process = subprocess.run(
                [sys.executable, "-m", "seal", "collect", str(config)],
                env=h.env,
                capture_output=True,
                text=True,
                timeout=280,
            )
            result = json.loads(process.stdout)
            h.commands.append(
                {
                    "command": ["seal", "collect", str(config)],
                    "exit": process.returncode,
                    "result": result,
                    "stderr": process.stderr,
                }
            )
            docs = h.sql(
                """SELECT d.url,v.content,v.source_format,v.body_hash,v.snapshot_id,v.observation_id FROM seal.document d
                JOIN seal.document_version v ON v.document_id=d.id WHERE d.source_id=%s ORDER BY d.url""",
                (source["id"],),
            )
            h.check(source["id"] + "_stdout_json", isinstance(result, dict))
            results.append(
                {
                    "source": source["id"],
                    "status": result.get("status", "UNKNOWN"),
                    "errors": result.get("errors", [result.get("error")]),
                    "documents": len(docs),
                    "scope": source["scope"],
                    "coverage": "UNKNOWN",
                }
            )
            h.capture(source["id"], {"receipt": result, "documents": docs})
            if result.get("status") == "complete":
                recheck = h.cli("recheck", config)
                results[-1]["recheck_status"] = recheck["status"]
        h.capture("live-results", results)
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        h.save(error)
        summary = json.loads((h.output / "summary.json").read_text())
        summary["source_outcomes"] = results
        if not error and any(r["status"] != "complete" for r in results):
            summary["status"] = "PARTIAL"
        write(h.output / "summary.json", summary)
        h.close()
    print(json.dumps({"artifacts": str(h.output), "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    main(parser.parse_args().name)
