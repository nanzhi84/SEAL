"""Exploratory Source Collect on retained PostgreSQL and immutable archives.

Run from the repository after configuring SEAL_DATABASE_URL and SEAL_ARCHIVE:
  uv run --frozen python experiments/v1.3-acceptance/live.py --output <new-directory>

This is a CLI driver, not another crawler. It preserves the executor's proxy and
does not start, reset, stop, or delete PostgreSQL or the archive. Source seeds
come from the original business map, not the V1.2 frozen acceptance requests.
No expected page/URL/Record counts or completeness assertions are defined.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
MAP = ROOT / "experiments/due-diligence/inputs/source-map.md"
SEEDS = {
    "dd-017": "https://www.court.gov.cn/shenpan/gengduo/77.html",
    "dd-041": "https://www.spp.gov.cn/spp/jczdal/index.shtml",
    "dd-250": "https://www.amac.org.cn/fwdt/wyc/jgcprycx/jgcx/jjtgrjjgs/",
    "dd-468": "http://www.moe.gov.cn/jyb_xxgk/s5743/s5744/A03/202110/t20211025_574874.html",
    "dd-102": "https://www.safe.gov.cn/beijing/xzcfxxgs/index.html",
    "dd-247": "https://www.amac.org.cn/fwdt/wyc/jgcprycx/jgcx/gmjjglrml/",
    "dd-357": "http://permit.mee.gov.cn/permitExt/defaults/default-index!getInformation.action",
    "dd-009": "https://find-and-update.company-information.service.gov.uk/",
}
OBSERVE = {
    "dd-017": "HTML list, pagination, detail and previously unknown templates",
    "dd-041": "long legal HTML; document versus individual-case granularity",
    "dd-250": "business HTML entry and discoverable JSON pagination",
    "dd-468": "business notice, XLS links, bytes and field lineage",
    "dd-102": "business entry to iframe table and details",
    "dd-247": "business HTML entry, JSON API and unenumerable filters",
    "dd-357": "literal ! in the business entry and discovered URL identities",
    "dd-009": "homepage, public links and unenumerable search space",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def source_config(identity, seed, args):
    host = urlsplit(seed).hostname
    return {
        "id": "v13-live-" + identity,
        "research_ids": [identity],
        "entry_urls": [seed],
        "allowed_hosts": [host],
        "allowed_path_prefixes": ["/"],
        "host_path_scopes": {host: ["/"]},
        "methods": ["GET", "HEAD"],
        "identity": "business_key",
        "output_schema": "record.v1",
        "archive_approved": True,
        "scope": (
            "Original business-map seed; discover public pages on the exact seed host, "
            "including navigation, pagination, details and attachments, subject to robots, "
            "Runtime access rules, depth/query limits and the stated request/time/byte budget. "
            "No historical two-page limit, fixed detail allowlist or invented search query. "
            "Associated hosts and cross-host attachments require a separately reviewed scope."
        ),
        "seed_role": "list",
        "budget": {
            "requests": args.requests,
            "seconds": args.seconds,
            "response_bytes": args.response_bytes,
        },
        "concurrency": 1,
        "delay": args.delay,
        "robots": True,
        "user_agent": "SEAL/1.3 (+public seeded Source experiment)",
    }


class Driver:
    def __init__(self, output, seconds):
        self.output = output
        self.timeout = seconds + 120
        self.env = dict(os.environ)
        # Real public runs may never inherit the fixture-only address exception.
        self.env.pop("SEAL_ALLOW_LOOPBACK", None)
        self.env["SEAL_USE_ENV_PROXY"] = "1"
        self.receipts = []

    def cli(self, *args, required=True):
        started = now()
        process = subprocess.run(
            [sys.executable, "-m", "seal", *map(str, args)],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        try:
            result = json.loads(process.stdout)
        except ValueError:
            result = {"error": "invalid_cli_json"}
        receipt = {
            "arguments": list(map(str, args)),
            "started_at": started,
            "completed_at": now(),
            "exit_code": process.returncode,
            "result": result,
        }
        self.receipts.append(receipt)
        write(self.output / "commands.json", self.receipts)
        if required and process.returncode != 0:
            raise RuntimeError("CLI operation failed: " + str(result.get("error")))
        return result


def summarize(run, exported):
    report = run.get("run", {}).get("report", {})
    summary = run.get("summary", {})
    discovery = run.get("discovery", {})
    observations = run.get("observations", [])
    return {
        "status": run.get("run", {}).get("status"),
        "termination": summary.get("termination"),
        "termination_reason": summary.get("terminal_reason", report.get("terminal_reason")),
        "errors": run.get("run", {}).get("errors", []),
        "counts": summary.get("counts", report.get("resource_counts", {})),
        "discovery": {k: v for k, v in discovery.items() if k != "events"},
        "observations": len(observations),
        "snapshots": len({o["snapshot_id"] for o in observations if o.get("snapshot_id")}),
        "records_exported": len(exported.get("records", [])),
        "documents_exported": len(exported.get("documents", [])),
        "unknown": (
            "Actual site totals and undiscovered pages are unknown. Queue exhaustion is "
            "only an execution result. API, search and JavaScript behavior not present in "
            "archived responses remains unverified. Records are recipe-defined projections."
        ),
    }


def report(output, manifest):
    lines = [
        "# V1.3 exploratory live Source runs",
        "",
        "These are observed execution results, with no expected real-site totals or business quality approval.",
        "The exact seed host is allowed at path `/`; robots and access controls apply. "
        "Associated hosts are not implicitly allowed. Request, depth and query limits are execution budgets.",
        "The PostgreSQL service and immutable archive were externally provisioned and were left in place.",
        "The environment proxy and TLS verification were preserved; no direct connection retry was used.",
        "Snapshot counts include archived HTTP error or robots responses. A proxy denial body "
        "is transport evidence and does not establish that upstream site content was fetched.",
        "",
        "| Source | Run | Status | Snapshots | Records | Termination |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for source in manifest["sources"]:
        value = source.get("observation", {})
        lines.append(
            "| "
            + " | ".join(
                map(
                    str,
                    [
                        source["research_id"],
                        source.get("run_id", "not executed"),
                        value.get("status", source.get("driver_error", "not executed")),
                        value.get("snapshots", "unknown"),
                        value.get("records_exported", "unknown"),
                        value.get("termination_reason", "unknown"),
                    ],
                )
            )
            + " |"
        )
    lines += [
        "",
        "Each Source directory contains the actual Source and params, Binding, Run inspection, "
        "per-run export and a fresh database inspection after Collect. Immutable snapshot/body hashes "
        "in Run/export evidence refer to the retained archive recorded in manifest.json.",
        "",
        "Source homepage search forms do not supply a finite list of all possible queries. "
        "This experiment does not invent TESCO, 易方达 or other search conditions. JSON endpoints "
        "and semantic `!` links must be discovered or supplied by reviewed Recipe rules; a proxy "
        "denial establishes neither upstream site availability nor template support.",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, default=ROOT / "recipes/seeded")
    parser.add_argument("--sources", nargs="+", choices=SEEDS, default=list(SEEDS))
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--response-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if not args.prepare_only and not all(
        os.environ.get(name) for name in ("SEAL_DATABASE_URL", "SEAL_ARCHIVE")
    ):
        parser.error("SEAL_DATABASE_URL and SEAL_ARCHIVE must name retained storage")
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "started_at": now(),
        "client_timezone": "Asia/Singapore",
        "started_at_local": datetime.now(ZoneInfo("Asia/Singapore")).isoformat(),
        "mode": "prepare_only" if args.prepare_only else "retained_pg_collect",
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "git_status_at_start": subprocess.check_output(
            ["git", "status", "--short"], cwd=ROOT, text=True
        ).splitlines(),
        "business_map": str(MAP.relative_to(ROOT)),
        "business_map_sha256": hashlib.sha256(MAP.read_bytes()).hexdigest(),
        "archive_root": os.environ.get("SEAL_ARCHIVE"),
        "database_url_recorded": False,
        "database_lifecycle": "externally provisioned; never cleaned by this driver",
        "network": "inherited proxy; SEAL_USE_ENV_PROXY=1; TLS verification retained",
        "allowed_hosts_needed": sorted({urlsplit(SEEDS[i]).hostname for i in args.sources}),
        "sources": [],
    }
    params = {
        "max_depth": 12,
        "max_query_variants": 40,
        "expand_homepage": True,
        "discover_sitemaps": True,
        "exclude_patterns": [],
        "specific_rules": [],
    }
    # Map membership detects accidental replacement by a historical frozen sample.
    map_text = MAP.read_text()
    if any(SEEDS[i] not in map_text for i in args.sources):
        raise RuntimeError("Business seed no longer appears in the source map")
    driver = Driver(args.output, args.seconds)
    recipe = None if args.prepare_only else driver.cli("recipe", "pack", args.recipe)
    for identity in args.sources:
        directory = args.output / identity
        directory.mkdir()
        source = source_config(identity, SEEDS[identity], args)
        write(directory / "source.json", source)
        write(directory / "params.json", params)
        observed = {
            "research_id": identity,
            "source_id": source["id"],
            "seed": SEEDS[identity],
            "mechanism_to_observe": OBSERVE[identity],
            "scope": source,
            "params": params,
            "recipe_version": recipe and recipe["recipe_version"],
        }
        manifest["sources"].append(observed)
        if not args.prepare_only:
            try:
                driver.cli("source", "apply", directory / "source.json")
                binding = driver.cli(
                    "binding",
                    "create",
                    source["id"],
                    "--recipe",
                    recipe["recipe_version"],
                    "--params",
                    directory / "params.json",
                )["binding_id"]
                observed["binding_id"] = binding
                write(directory / "binding.json", driver.cli("inspect", "binding", binding))
                before = driver.cli("inspect", "source", source["id"])
                driver.cli(
                    "source",
                    "select",
                    source["id"],
                    "--binding",
                    binding,
                    "--expect-generation",
                    before["source"]["generation"],
                )
                receipt = driver.cli("collect", source["id"], required=False)
                write(directory / "collect.json", receipt)
                run_id = receipt.get("run_id", receipt.get("id"))
                if not run_id:
                    raise RuntimeError(
                        "Collect did not return a Run ID: " + str(receipt.get("error"))
                    )
                observed["run_id"] = run_id
                run = driver.cli("inspect", "run", run_id)
                exported = driver.cli("export", source["id"], "--run", run_id)
                write(directory / "run.json", run)
                write(directory / "export.json", exported)
                snapshot_ids = sorted(
                    {
                        observation["snapshot_id"]
                        for observation in run.get("observations", [])
                        if observation.get("snapshot_id")
                    }
                )
                write(
                    directory / "retained-snapshots.json",
                    [driver.cli("inspect", "snapshot", key) for key in snapshot_ids],
                )
                # Separate CLI processes query PostgreSQL after Runtime has exited.
                write(
                    directory / "retained-source.json",
                    driver.cli("inspect", "source", source["id"]),
                )
                observed["observation"] = summarize(run, exported)
            except Exception as exc:
                observed["driver_error"] = str(exc)
        write(args.output / "manifest.json", manifest)
        report(args.output, manifest)
    manifest["completed_at"] = now()
    write(args.output / "manifest.json", manifest)
    report(args.output, manifest)
    print(json.dumps({"evidence": str(args.output), "sources": len(manifest["sources"])}))
    return 1 if any("driver_error" in s for s in manifest["sources"]) else 0


if __name__ == "__main__":
    sys.exit(main())
