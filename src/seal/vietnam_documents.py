"""Resumable dd-492 enumeration and bounded native PDF collection batches."""

import argparse
import asyncio
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import load_file
from .core import SealError, atomic_write, canonical, digest
from .db import connect, decision, j
from .objects import Objects
from .pdf_contract import PDF_TEXT_CONTRACT
from .runs import stop_process
from .vietnam_discovery import Discovery


def reconcile(database, config):
    hashes = [r[0] for r in database.execute("SELECT config_hash FROM batch")]
    if not hashes:
        return
    urls = [r[0] for r in database.execute("SELECT url FROM pdf WHERE status='pending'")]
    if not urls:
        return
    # A CLI may have committed a response before the harvest process exited.
    # Relational observations, rather than a local success flag, are authoritative.
    with connect() as connection:
        rows = connection.execute(
            """SELECT DISTINCT ON(o.url) o.url,o.outcome,o.error,o.run_id,o.archive_ref,
               o.result_ref,o.version_id,o.observed_at FROM seal.observation o
               JOIN seal.run r ON r.id=o.run_id
               WHERE r.source_id=%s AND r.config_hash=ANY(%s) AND o.url=ANY(%s)
               ORDER BY o.url,o.observed_at DESC""",
            (config["id"], hashes, urls),
        ).fetchall()
    with database:
        for row in rows:
            retry = database.execute(
                "SELECT since FROM retry WHERE url=?", (row["url"],)
            ).fetchone()
            reparse = database.execute(
                "SELECT since FROM reparse WHERE url=? ORDER BY since DESC LIMIT 1", (row["url"],)
            ).fetchone()
            if reparse and (not retry or reparse[0] > retry[0]):
                retry = reparse
            if retry and row["observed_at"] < datetime.fromisoformat(retry[0]):
                continue
            if row["outcome"] in {"parsed", "unchanged"}:
                Objects().get(row["archive_ref"])
                Objects().get(row["result_ref"])
            database.execute(
                "UPDATE pdf SET status=?,evidence=? WHERE url=?",
                (
                    "parsed" if row["outcome"] in {"parsed", "unchanged"} else "failed",
                    json.dumps(row, default=str),
                    row["url"],
                ),
            )


def collect(database, config, state, output, batch_size, max_pdfs):
    reconcile(database, config)
    selected = 0
    while True:
        count = batch_size if not max_pdfs else min(batch_size, max_pdfs - selected)
        if count <= 0:
            break
        urls = [
            r[0]
            for r in database.execute(
                "SELECT url FROM pdf WHERE status='pending' ORDER BY url LIMIT ?", (count,)
            )
        ]
        if not urls:
            break
        selected += len(urls)
        batch = json.loads(json.dumps(config))
        batch["entry_urls"] = urls
        batch["crawl4ai"]["max_depth"] = 0
        identity = digest(batch)[:16]
        source = state / f"dd-492-{identity}.yaml"
        atomic_write(source, yaml.safe_dump(batch, allow_unicode=True, sort_keys=False).encode())
        with database:
            database.execute("INSERT OR IGNORE INTO batch VALUES(?)", (digest(batch),))
        command = [sys.executable, "-m", "seal.cli", "collect", str(source), "--no-progress"]
        print(json.dumps({"phase": "collect", "batch": identity, "pdfs": len(urls)}), flush=True)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, _ = process.communicate(timeout=config["budget"]["seconds"] + 45)
        finally:
            stop_process(process)
        try:
            receipt = json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            raise SealError("pdf_batch_receipt_missing") from None
        atomic_write(
            output / f"batch-{identity}.json",
            canonical(
                {
                    "command": command,
                    "exit_code": process.returncode,
                    "receipt": receipt,
                    "pdf_urls": urls,
                }
            ),
        )
        reconcile(database, config)
        pending = database.execute(
            "SELECT count(*) FROM pdf WHERE url IN ("
            + ",".join("?" for _ in urls)
            + ") AND status='pending'",
            urls,
        ).fetchone()[0]
        if pending:
            raise SealError("pdf_batch_incomplete_resume_required")


def summary(database, full_history, output, max_pages, max_pdfs):
    counts = dict(database.execute("SELECT status,count(*) FROM pdf GROUP BY status"))
    pages = database.execute("SELECT count(*) FROM page").fetchone()[0]
    details = database.execute("SELECT count(*) FROM detail").fetchone()[0]
    value = {
        "source_id": "dd-492",
        "pages": pages,
        "documents": details,
        "pdfs": sum(counts.values()),
        "counts": counts,
        "enumeration_complete": full_history,
        "selected_pages_complete": counts.get("pending", 0) == 0,
        "all_pdfs_parsed": bool(counts.get("parsed"))
        and not counts.get("failed", 0)
        and not counts.get("pending", 0),
        "limits": {"max_pages": max_pages, "max_pdfs": max_pdfs},
        "failures": [
            json.loads(r[0])
            for r in database.execute("SELECT evidence FROM pdf WHERE status='failed'")
        ],
    }
    value["failure_counts"] = dict(
        Counter(row["error"] or row["outcome"] for row in value["failures"])
    )
    value["latest_archived_pdfs"] = sum(
        bool(json.loads(row[0]).get("archive_ref"))
        for row in database.execute("SELECT evidence FROM pdf WHERE evidence IS NOT NULL")
    )
    value["all_latest_originals_archived"] = bool(value["pdfs"]) and (
        value["latest_archived_pdfs"] == value["pdfs"]
    )
    atomic_write(output / "summary.json", canonical(value))
    atomic_write(
        output / "pdf-manifest.json",
        canonical(
            [
                {
                    "url": r["url"],
                    "status": r["status"],
                    "observation": json.loads(r["evidence"]) if r["evidence"] else None,
                }
                for r in database.execute("SELECT * FROM pdf ORDER BY url")
            ]
        ),
    )
    return value


def retry_network(database, config, state, output, batch_size, parser_fix=False):
    # At most two extra downloads per URL across resumes. A parser repair is an
    # explicit action, never automatic; scanned PDFs and hard HTTP errors stay put.
    urls = []
    with database:
        for row in database.execute("SELECT * FROM pdf WHERE status='failed'").fetchall():
            evidence = json.loads(row["evidence"])
            previous = database.execute(
                "SELECT attempts FROM retry WHERE url=?", (row["url"],)
            ).fetchone()
            attempts = previous[0] if previous else 0
            selected = (
                evidence["error"] == "pdf_page_parse_failed"
                if parser_fix
                else evidence["outcome"] == "network_failed"
            )
            if not selected or attempts >= 2:
                continue
            database.execute(
                "INSERT OR REPLACE INTO retry VALUES(?,?,?)",
                (row["url"], attempts + 1, datetime.now(timezone.utc).isoformat()),
            )
            database.execute("UPDATE pdf SET status='pending' WHERE url=?", (row["url"],))
            urls.append(row["url"])
    retry_config = json.loads(json.dumps(config))
    retry_config["concurrency"] = min(2, config["concurrency"])
    retry_config["delay"] = max(0.5, config["delay"])
    print(
        json.dumps(
            {"phase": "retry_parser_fix" if parser_fix else "retry_network", "pdfs": len(urls)}
        ),
        flush=True,
    )
    collect(database, retry_config, state, output, batch_size, None)


def reparse_successful(database, config, state, output, batch_size):
    """Explicit once-per-contract correction, independent of network retry caps."""
    with database:
        for row in database.execute("SELECT * FROM pdf WHERE status='parsed'").fetchall():
            previous = database.execute(
                "SELECT 1 FROM reparse WHERE url=? AND contract=?",
                (row["url"], PDF_TEXT_CONTRACT),
            ).fetchone()
            if previous:
                continue
            database.execute(
                "INSERT INTO reparse VALUES(?,?,?)",
                (row["url"], PDF_TEXT_CONTRACT, datetime.now(timezone.utc).isoformat()),
            )
            database.execute("UPDATE pdf SET status='pending' WHERE url=?", (row["url"],))
    collect(database, config, state, output, batch_size, None)


def retry_reparse_network(database, config, state, output, batch_size):
    urls = []
    with database:
        for row in database.execute("SELECT * FROM pdf WHERE status='failed'").fetchall():
            if json.loads(row["evidence"])["outcome"] != "network_failed":
                continue
            key = (row["url"], PDF_TEXT_CONTRACT)
            if not database.execute(
                "SELECT 1 FROM reparse WHERE url=? AND contract=?", key
            ).fetchone():
                continue
            previous = database.execute(
                "SELECT attempts FROM reparse_retry WHERE url=? AND contract=?", key
            ).fetchone()
            attempts = previous[0] if previous else 0
            if attempts >= 2:
                continue
            database.execute(
                "INSERT OR REPLACE INTO reparse_retry VALUES(?,?,?)", (*key, attempts + 1)
            )
            database.execute(
                "UPDATE reparse SET since=? WHERE url=? AND contract=?",
                (datetime.now(timezone.utc).isoformat(), *key),
            )
            database.execute("UPDATE pdf SET status='pending' WHERE url=?", (row["url"],))
            urls.append(row["url"])
    retry_config = json.loads(json.dumps(config))
    retry_config["concurrency"] = min(2, config["concurrency"])
    retry_config["delay"] = max(0.5, config["delay"])
    print(json.dumps({"phase": "retry_reparse_network", "pdfs": len(urls)}), flush=True)
    collect(database, retry_config, state, output, batch_size, None)


def restore_entry(database, config):
    """Keep the registered channel visible after freezing PDF-seeded batch Runs."""
    hashes = {r[0] for r in database.execute("SELECT config_hash FROM batch")}
    with connect() as connection:
        source = connection.execute(
            "SELECT config,generation FROM seal.source WHERE id=%s FOR UPDATE", (config["id"],)
        ).fetchone()
        if not source or source["config"] == config:
            return
        if digest(source["config"]) not in hashes:
            raise SealError("harvest_source_configuration_changed")
        if connection.execute(
            "SELECT 1 FROM seal.run WHERE source_id=%s AND generation=%s AND status NOT IN ('complete','failed') LIMIT 1",
            (config["id"], source["generation"]),
        ).fetchone():
            raise SealError("harvest_source_still_executing")
        connection.execute(
            "UPDATE seal.source SET config=%s,generation=generation+1 WHERE id=%s",
            (j(config), config["id"]),
        )
        decision(
            connection,
            config["id"],
            "control",
            {"action": "select_configuration", "config_hash": digest(config)},
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-pdfs", type=int)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--retry-network", action="store_true")
    parser.add_argument("--retry-parser-fix", action="store_true")
    parser.add_argument("--reparse-successful", action="store_true")
    parser.add_argument("--retry-reparse-network", action="store_true")
    args = parser.parse_args()
    config = load_file(args.source).model_dump()
    if (
        config["id"] != "dd-492"
        or config["crawl4ai"]["mode"] != "http"
        or config["robots"]
        or not 1 <= args.batch_size <= 1000
        or any(v is not None and v < 1 for v in (args.max_pages, args.max_pdfs))
        or sum(
            (
                args.retry_network,
                args.retry_parser_fix,
                args.reparse_successful,
                args.retry_reparse_network,
            )
        )
        > 1
    ):
        raise SealError("invalid_vietnam_harvest_config")
    # This adapter is intentionally source-specific, not a speculative universal form engine.
    if config["entry_urls"][0] != "https://vanban.chinhphu.vn/":
        raise SealError("invalid_vietnam_harvest_entry")
    state, output = args.state.resolve(), args.artifacts.resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "harvest.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SealError("harvest_already_executing") from None
        signature = digest(
            {
                "config": config,
                "database": os.environ.get("SEAL_DATABASE_URL"),
                "bucket": os.environ.get("SEAL_S3_BUCKET"),
                "endpoint": os.environ.get("SEAL_S3_ENDPOINT"),
            }
        )
        with sqlite3.connect(state / "harvest.sqlite") as database:
            database.row_factory = sqlite3.Row
            database.executescript("""
                CREATE TABLE IF NOT EXISTS context(signature TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS page(number INTEGER PRIMARY KEY,fingerprint TEXT,evidence TEXT);
                CREATE TABLE IF NOT EXISTS detail(url TEXT PRIMARY KEY,html_ref TEXT,pdf_urls TEXT);
                CREATE TABLE IF NOT EXISTS pdf(url TEXT PRIMARY KEY,status TEXT,evidence TEXT);
                CREATE TABLE IF NOT EXISTS batch(config_hash TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS retry(url TEXT PRIMARY KEY,attempts INTEGER,since TEXT);
                CREATE TABLE IF NOT EXISTS reparse(url TEXT,contract TEXT,since TEXT,PRIMARY KEY(url,contract));
                CREATE TABLE IF NOT EXISTS reparse_retry(url TEXT,contract TEXT,attempts INTEGER,PRIMARY KEY(url,contract));
            """)
            existing = database.execute("SELECT signature FROM context").fetchone()
            if existing and existing[0] != signature:
                raise SealError("harvest_state_environment_mismatch")
            database.execute("INSERT OR IGNORE INTO context VALUES(?)", (signature,))
            database.commit()
            if (
                args.max_pages
                and database.execute("SELECT count(*) FROM page").fetchone()[0] > args.max_pages
            ):
                raise SealError("harvest_page_scope_mismatch")
            atomic_write(output / "dd-492.yaml", args.source.read_bytes())
            if (
                args.retry_network
                or args.retry_parser_fix
                or args.reparse_successful
                or args.retry_reparse_network
            ):
                pages = database.execute("SELECT evidence FROM page ORDER BY number").fetchall()
                if not pages or args.max_pages and len(pages) < args.max_pages:
                    raise SealError("retry_requires_completed_page_selection")
                full_history = json.loads(pages[-1][0])["last_page"]
                if args.retry_reparse_network:
                    retry_reparse_network(database, config, state, output, args.batch_size)
                elif args.reparse_successful:
                    reparse_successful(database, config, state, output, args.batch_size)
                else:
                    retry_network(
                        database, config, state, output, args.batch_size, args.retry_parser_fix
                    )
            else:
                full_history = asyncio.run(Discovery(config, database, output).run(args.max_pages))
                collect(database, config, state, output, args.batch_size, args.max_pdfs)
                if args.max_pdfs is None:
                    for _ in range(2):
                        network = database.execute(
                            "SELECT evidence FROM pdf WHERE status='failed'"
                        ).fetchall()
                        if not any(
                            json.loads(row[0])["outcome"] == "network_failed" for row in network
                        ):
                            break
                        retry_network(database, config, state, output, args.batch_size)
            restore_entry(database, config)
            result = summary(database, full_history, output, args.max_pages, args.max_pdfs)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0 if result["all_pdfs_parsed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SealError as exc:
        print(json.dumps({"error": exc.code}), flush=True)
        sys.exit(2)
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted_resume_same_state"}), flush=True)
        sys.exit(130)
    except Exception as exc:
        print(json.dumps({"error": "harvest_failed", "category": type(exc).__name__}), flush=True)
        sys.exit(2)
