"""Reproducible operator demo, invoking the current public CLI unchanged."""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import psycopg
from parsel import Selector

PROJECT = Path(__file__).resolve().parent
ROOT = PROJECT.parents[1]
RUN_NAME = os.environ.get("SEAL_DEMO_RUN", "court-10pages-20261012")
if not RUN_NAME.startswith("court-10pages-") or not all(c.isalnum() or c == "-" for c in RUN_NAME):
    raise ValueError("invalid demo run name")
OUT = PROJECT / "artifacts" / RUN_NAME
WORK = PROJECT / "work" / RUN_NAME
OUT.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)
SOURCE = RUN_NAME.replace("-", "_")
DATABASE = "seal_" + SOURCE
os.umask(0o077)
os.environ["SEAL_DATABASE_URL"] = "postgresql:///" + DATABASE
os.environ["SEAL_ARCHIVE"] = str(WORK / "archive")


def save(name, data):
    (OUT / (name + ".json")).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def read(name):
    return json.loads((OUT / (name + ".json")).read_text())


def cli(name, *args):
    command = ["uv", "run", "--frozen", "seal", *map(str, args)]
    print("\n$ " + shlex.join(command), flush=True)
    with (OUT / (name + ".stderr.log")).open("w") as error:
        process = subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.PIPE, stderr=error, text=True
        )
        while process.poll() is None:
            try:
                stdout, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                with psycopg.connect(os.environ["SEAL_DATABASE_URL"]) as connection:
                    rows = connection.execute(
                        "SELECT r.mode,r.status,jsonb_array_length(r.inputs),"
                        "(SELECT count(*) FROM seal_fetch_observation o WHERE o.run_id=r.id),"
                        "(SELECT count(*) FROM seal_record_emission e WHERE e.run_id=r.id) FROM seal_run r WHERE source_id=%s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (SOURCE,),
                    ).fetchall()
                print("  实时 SQL [mode,status,输入,已归档,Record]=" + str(rows), flush=True)
                continue
            break
        else:
            stdout, _ = process.communicate()
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError("CLI output was not JSON: " + name) from None
    save(name, result)
    with (OUT / "commands.log").open("a") as log:
        log.write(
            shlex.join(command) + " -> " + name + ".json; exit=" + str(process.returncode) + "\n"
        )
    display = {
        k: result[k]
        for k in ("status", "run_id", "binding_id", "recipe_version", "error", "errors", "summary")
        if k in result
    }
    if "record" in result:
        display = {
            "record_id": result["record"]["id"],
            "versions": len(result["versions"]),
            "results": len(result["results"]),
            "emissions": len(result["emissions"]),
        }
    if "snapshot" in result:
        display = {
            "snapshot_id": result["snapshot_id"],
            "body_hash": result["snapshot"]["body_hash"],
            "availability": result["archive"]["availability"],
        }
    if "records" in result:
        display["records"] = len(result["records"])
        display["quality_status"] = result.get("quality_status")
    if "documents" in result:
        display["documents"] = len(result["documents"])
        display["quality_status"] = result.get("quality_status")
    print(json.dumps(display or result, ensure_ascii=False, default=str), flush=True)
    print("  产物 → " + str(OUT / (name + ".json")), flush=True)
    if process.returncode:
        raise RuntimeError("CLI failed: " + name)
    return result


def setup():
    print("① Source / ② Recipe / ③ Binding：独立数据库、私有归档、冻结前10页范围", flush=True)
    config = json.loads((PROJECT / "config/source.json").read_text())
    params = json.loads((PROJECT / "config/params.json").read_text())
    # Each run receives its own Source identity; the remaining configuration is copied unchanged.
    config["id"] = SOURCE
    with psycopg.connect("postgresql:///postgres", autocommit=True) as connection:
        if not connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE,)
        ).fetchone():
            connection.execute('CREATE DATABASE "' + DATABASE + '"')
    save("source", config)
    save("params", params)
    save(
        "environment",
        {
            "git_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "database": DATABASE,
            "archive": str(WORK / "archive"),
            "scope": config["scope"],
            "engine_sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((ROOT / "src/seal").glob("*.py"))
            },
        },
    )
    cli("migrate", "db", "migrate")
    cli("source-receipt", "source", "apply", OUT / "source.json")
    version = cli("recipe", "recipe", "pack", PROJECT / "recipe")["recipe_version"]
    binding = cli(
        "binding", "binding", "create", SOURCE, "--recipe", version, "--params", OUT / "params.json"
    )["binding_id"]
    state = cli("source-before", "inspect", "source", SOURCE)
    cli(
        "selected",
        "source",
        "select",
        SOURCE,
        "--binding",
        binding,
        "--expect-generation",
        state["source"]["generation"],
    )


def cycle(mode):
    print(
        {
            "collect": "④ Collect → ⑤ Archive → ⑥ SQL → ⑦ inspect/export",
            "recheck": "⑧ Recheck：冻结已知详情，重新联网核查",
            "replay": "⑨ Replay：读取历史归档、重新解析，禁止联网补齐",
        }[mode],
        flush=True,
    )
    args = (
        ["replay", read("collect")["run_id"]]
        if mode == "replay"
        else ["collect", SOURCE] + (["--recheck"] if mode == "recheck" else [])
    )
    receipt = cli(mode, *args)
    inspected = cli(mode + "-run", "inspect", "run", receipt["run_id"])
    export = cli(mode + "-export", "export", SOURCE, "--run", receipt["run_id"])
    evidence = OUT / ("evidence-" + mode)
    evidence.mkdir(exist_ok=True)
    (evidence / "run.json").write_text(json.dumps(inspected, ensure_ascii=False, indent=2))
    (evidence / "export.json").write_text(json.dumps(export, ensure_ascii=False, indent=2))
    save(
        mode + "-flow",
        [
            {
                k: d.get(k)
                for k in (
                    "record_id",
                    "record_version_id",
                    "record_key",
                    "inputs",
                    "binding_id",
                    "recipe_version",
                )
            }
            for d in export["records"]
        ],
    )
    if mode == "collect":
        first = export["records"][0]
        cli("example-snapshot", "inspect", "snapshot", first["primary_snapshot_id"])
        cli("example-record", "inspect", "record", first["record_id"])
        print("  示例结果：" + first["data"]["title"], flush=True)


def verify():
    print("⑩ 独立验收：从原始字节复算列表、字段、哈希和SQL血缘", flush=True)
    checks = []

    def check(name, actual, expected):
        checks.append(
            {
                "name": name,
                "actual": actual,
                "expected": expected,
                "status": "PASS" if actual == expected else "FAIL",
            }
        )

    def raw(identity):
        value = (WORK / "archive/objects" / identity[:2] / identity[2:]).read_bytes()
        check("archive:" + identity, hashlib.sha256(value).hexdigest(), identity)
        return value

    initial = read("collect-export")["records"]
    expected_lists = {
        "https://www.court.gov.cn/shenpan/gengduo/77.html",
        *[f"https://www.court.gov.cn/shenpan/gengduo/77_{i}.html" for i in range(2, 11)],
    }
    actual_lists, detail_urls = set(), set()
    for entry in read("collect-run")["run"]["inputs"]:
        snapshot = json.loads(raw(entry["snapshot_id"]))
        body = raw(snapshot["body_hash"])
        if entry["role"] == "list":
            actual_lists.add(snapshot["url"])
            selector = Selector(text=body.decode(snapshot.get("encoding") or "utf-8"))
            from urllib.parse import urljoin

            detail_urls.update(
                urljoin(snapshot["url"], href)
                for href in selector.css(
                    '.sec_list a[href*="/shenpan/xiangqing/"]::attr(href)'
                ).getall()
            )
    check("exact_first_10_lists", sorted(actual_lists), sorted(expected_lists))
    check(
        "all_listed_details_exported", sorted(d["record_key"] for d in initial), sorted(detail_urls)
    )
    for mode in ("collect", "recheck", "replay"):
        run = read(mode + "-run")
        records = read(mode + "-export")["records"]
        check(mode + ":complete", run["run"]["status"], "complete")
        check(
            mode + ":same_keys",
            sorted(d["record_key"] for d in records),
            sorted(d["record_key"] for d in initial),
        )
        if mode == "replay":
            check("replay:zero_http_attempts", run["summary"]["counts"]["http_attempts"], 0)
            check("replay:no_network_observations", run["observations"], [])

            def projection(rows):
                return sorted(
                    (d["record_key"], json.dumps(d["data"], sort_keys=True, ensure_ascii=False))
                    for d in rows
                )

            check("replay:exact_content", projection(records) == projection(initial), True)
    oracle_out = OUT / "independent"
    if oracle_out.exists():
        oracle_out = OUT / ("independent-" + str(time.time_ns()))
    command = [
        "uv",
        "run",
        "--frozen",
        "python",
        "experiments/v1.3-acceptance/business_verify.py",
        "--evidence",
        *[str(OUT / ("evidence-" + m)) for m in ("collect", "recheck", "replay")],
        "--output",
        str(oracle_out),
    ]
    print("$ " + shlex.join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT)
    check("independent_archive_locator_sql_oracle", result.returncode, 0)
    save("assertions", checks)
    report = {
        "lists": len(actual_lists),
        "details": len(initial),
        "assertions": len(checks),
        "failed": sum(c["status"] != "PASS" for c in checks),
        "quality_status": "not_evaluated",
        "whole_site_complete": "UNKNOWN",
    }
    save("acceptance", report)
    flow = [
        "SEAL 有界闭环演示：前10页列表及全部关联详情",
        "",
        "Source(source.json) + RecipeVersion(recipe.json) + Params(params.json)",
        "  -> Binding(binding.json): 冻结范围、规则与运行环境",
        "  -> Collect: robots -> 列表分页 -> 详情下载",
        "  -> Archive: 原始bytes -> body SHA-256 -> Snapshot -> Observation",
        "  -> SQL: Record -> RecordVersion -> ProcessingResult / Emission",
        "  -> inspect / export: 可追溯结构化结果",
        "  -> Recheck: 冻结已知详情目标 -> 重新联网 -> 新Observation / 版本比较",
        "  -> Replay: Collect历史输入 -> 归档读取 -> 同规则解析 -> 隔离结果",
        "  -> 验收: 10页、全部详情、归档哈希、字段Locator、SQL血缘、零网络",
        "",
        "命令与节点（前缀均为 uv run --frozen seal；实际精确参数见 commands.log）：",
        "① db migrate; source apply source.json -> 数据库表 / Source",
        "② recipe pack bounded-business -> 不可变RecipeVersion",
        "③ binding create SOURCE --recipe VERSION --params params.json; source select -> Binding",
        "④ collect SOURCE -> collect.json",
        "⑤/⑥ 随④自动完成归档和SQL持久化 -> archive/objects + 数据库",
        "⑦ inspect run RUN; inspect record ID; inspect snapshot HASH; export SOURCE --run RUN -> collect-run.json / example-record.json / example-snapshot.json / collect-export.json",
        "⑧ collect SOURCE --recheck -> recheck.json / recheck-run.json / recheck-export.json",
        "⑨ replay COLLECT_RUN -> replay.json / replay-run.json / replay-export.json",
        "⑩ demo.py verify -> assertions.json / independent/ / acceptance.json",
        "",
    ]
    for mode in ("collect", "recheck", "replay"):
        receipt = read(mode)
        flow.append(
            mode
            + ": "
            + json.dumps(
                {
                    "run_id": receipt["run_id"],
                    "status": receipt["status"],
                    "errors": receipt["errors"],
                    "counts": receipt["summary"]["counts"],
                },
                ensure_ascii=False,
            )
        )
    flow += [
        "",
        "有界验收：" + json.dumps(report, ensure_ascii=False),
        "独立oracle：" + (oracle_out / "summary.json").read_text(),
        "",
        "环境前提：uv、锁定Python3.12.13与依赖、运行中的本地PostgreSQL17；本地账号需可建独立数据库；源站需可访问。",
        "复跑（每次新数据库和归档）：bash " + str(PROJECT / "rerun.sh"),
        "原文仅保存在私有归档；quality_status=not_evaluated，不声称全栏目/全站或法律语义质量完成。",
        "本次Recipe在现有法院适配器的私有副本中增加last_page上限；原项目Runtime与recipes目录未修改。",
    ]
    (OUT / "walkthrough.txt").write_text("\n".join(flow) + "\n")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["setup", "collect", "recheck", "replay", "verify"])
    stage = parser.parse_args().stage
    if stage == "setup":
        setup()
    elif stage == "verify":
        verify()
    else:
        cycle(stage)
