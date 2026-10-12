"""Independent saved-evidence oracle: restore SQL, read bytes, no SEAL imports."""

import argparse
import hashlib
import json
import subprocess

from harness import Harness


def sha(body):
    return hashlib.sha256(body).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def verify(output):
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "PASS" or (
        summary["status"] == "PARTIAL" and summary.get("source_outcomes")
    ), "acceptance_failed"
    assert all(r["status"] == "PASS" for r in json.loads((output / "assertions.json").read_text()))
    for name, key in summary["files"].items():
        assert sha((output / name).read_bytes()) == key, "artifact_hash_mismatch"
    work = __import__("pathlib").Path(summary["work"])
    archive = work / "archive/objects"

    def read(key):
        body = (archive / key[:2] / key[2:]).read_bytes()
        assert sha(body) == key, "archive_hash_mismatch"
        return body

    h = Harness("oracle-" + output.name + "-" + __import__("uuid").uuid4().hex[:8])
    error = None
    try:
        h.start()
        subprocess.run(
            [
                "pg_restore",
                "--dbname",
                h.env["SEAL_DATABASE_URL"],
                "--no-owner",
                str(work / "final.dump"),
            ],
            capture_output=True,
            check=True,
        )
        observations = {r["id"]: r for r in h.sql("SELECT * FROM seal.fetch_observation")}
        versions = {r["id"]: r for r in h.sql("SELECT * FROM seal.document_version")}
        documents = {r["id"]: r for r in h.sql("SELECT * FROM seal.document")}
        runs = {r["id"]: r for r in h.sql("SELECT * FROM seal.run")}
        emissions = h.sql("SELECT * FROM seal.document_emission")
        nodes = set()
        for obs in observations.values():
            if not obs["snapshot_id"]:
                continue
            snap = json.loads(read(obs["snapshot_id"]))
            body = read(obs["body_hash"])
            h.check(
                "observation_archive",
                (snap["body_hash"], snap["body_size"]),
                (obs["body_hash"], len(body)),
            )
        for version in versions.values():
            h.check(
                "version_content_hash", sha(canonical(version["content"])), version["content_hash"]
            )
            h.check(
                "version_body_hash",
                json.loads(read(version["snapshot_id"]))["body_hash"],
                version["body_hash"],
            )
            h.check(
                "version_original_observation",
                observations[version["observation_id"]]["snapshot_id"],
                version["snapshot_id"],
            )
            nodes.update(n["type"] for n in version["content"]["children"])
            if summary.get("source_outcomes"):
                body = read(version["body_hash"])
                if version["source_format"] == "json":
                    h.check(
                        "live_json_full_raw_value",
                        version["content"]["children"][0]["value"],
                        json.loads(body),
                    )
                if version["source_format"] == "xls":
                    import xlrd

                    header = xlrd.open_workbook(file_contents=body).sheet_by_index(0).row_values(0)
                    table = next(n for n in version["content"]["children"] if n["type"] == "table")
                    h.check("live_xls_original_header", table["rows"][0], header)
        for emission in emissions:
            obs = observations[emission["observation_id"]]
            version = versions[emission["document_version_id"]]
            h.check(
                "emission_attempt",
                (obs["run_id"], obs["attempt_epoch"], obs["source_id"]),
                (
                    emission["run_id"],
                    emission["attempt_epoch"],
                    documents[emission["document_id"]]["source_id"],
                ),
            )
            h.check("emission_version_identity", version["document_id"], emission["document_id"])
            h.check(
                "emission_actual_input",
                (obs["snapshot_id"], obs["body_hash"]),
                (emission["snapshot_id"], version["body_hash"]),
            )
        for doc in documents.values():
            current = doc["latest_document_version"]
            if current:
                h.check("current_version_identity", versions[current]["document_id"], doc["id"])
                h.check(
                    "current_has_completed_emission",
                    any(
                        e["document_version_id"] == current
                        and runs[e["run_id"]]["status"] == "complete"
                        for e in emissions
                    ),
                )
        for ref in h.sql("SELECT * FROM seal.document_reference"):
            node = versions[ref["document_version_id"]]["content"]["children"][ref["ordinal"]]
            h.check(
                "reference_matches_json",
                (node["document_id"], node["url"]),
                (ref["target_document_id"], ref["url"]),
            )
            h.check(
                "reference_source_boundary",
                documents[ref["target_document_id"]]["source_id"],
                documents[versions[ref["document_version_id"]]["document_id"]]["source_id"],
            )
        for run in runs.values():
            if not run["report"].get("manifest_id"):
                continue
            manifest = json.loads(read(run["report"]["manifest_id"]))
            h.check("manifest_run", manifest["run_id"], run["id"])
        h.check("five_node_types", nodes, {"article", "image", "table", "json", "document_ref"})
        h.check(
            "no_cross_attempt_rows",
            h.sql("""SELECT count(*) AS n FROM seal.document_emission e
            JOIN seal.fetch_observation o ON o.id=e.observation_id
            WHERE e.run_id<>o.run_id OR e.attempt_epoch<>o.attempt_epoch""")[0]["n"],
            0,
        )
        report = {
            "status": "PASS",
            "assertions": len(h.checks),
            "observations": len(observations),
            "versions": len(versions),
            "emissions": len(emissions),
            "method": "Independent pg_restore, SQL lineage, archive and canonical content hashes; no Runtime parsers",
            "dump_sha256": sha((work / "final.dump").read_bytes()),
            "scope": summary["scope"],
            "source_outcomes": summary.get("source_outcomes", []),
        }
        (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        h.capture(
            "oracle",
            {"status": "FAIL" if error else "PASS", "error": error, "assertions": len(h.checks)},
        )
        h.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=__import__("pathlib").Path)
    print(json.dumps(verify(parser.parse_args().output.resolve())))
