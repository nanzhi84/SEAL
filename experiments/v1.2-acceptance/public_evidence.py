"""Package public evidence, withholding session-bearing bytes without alteration.

Full originals remain in the ignored local acceptance archive. A public package
is explicitly insufficient to re-evaluate the affected Source's raw fields.
"""

import argparse
import gzip
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

SESSION = re.compile(rb"jsessionid=[A-Za-z0-9._-]{16,}", re.I)


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def package(directory, output):
    manifest = json.loads(directory.joinpath("manifest.json").read_text())
    if manifest["real_source_acceptance"]["status"] != "PASS":
        raise ValueError("Cannot package a failed real acceptance as PASS")
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / "real-evidence.tar.gz"
    index_path = output / "real-evidence-index.json"
    if bundle.exists() or index_path.exists():
        raise ValueError("Evidence output already exists; use a new directory")
    with tempfile.TemporaryDirectory(prefix="seal-public-evidence-") as temporary:
        public = Path(temporary) / "real"
        public.mkdir()
        withheld, hashes = {}, {}
        for name, expected in manifest["files"].items():
            source = (directory / name).resolve()
            if not source.is_relative_to(directory.resolve()):
                raise ValueError("Invalid artifact path")
            body = source.read_bytes()
            if hashlib.sha256(body).hexdigest() != expected:
                raise ValueError("Artifact changed after acceptance")
            if SESSION.search(body):
                withheld[name] = {
                    "sha256": expected,
                    "reason": "anonymous_session_marker_unverified_privilege",
                }
                continue
            target = public / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            hashes[name] = expected
        withheld_hashes = {entry["sha256"] for entry in withheld.values()}
        private = set()
        for path in directory.glob("*.json"):
            data = json.loads(path.read_text())
            if isinstance(data, dict) and "export" in data:
                if any(
                    ref["body_hash"] in withheld_hashes
                    for record in data["export"]["records"]
                    for ref in record["inputs"]
                ):
                    private.add(data["export"]["source_id"])
        manifest.update(
            files=hashes, withheld_files=withheld, local_only_source_ids=sorted(private)
        )
        manifest["disclosure"] = (
            "Original bytes were not modified. Session-bearing Sources require the full ignored local archive for raw field verification; public verification explicitly excludes them."
        )
        write(public / "manifest.json", manifest)
        with (
            bundle.open("wb") as stream,
            gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as compressed,
            tarfile.open(fileobj=compressed, mode="w") as archive,
        ):
            for path in sorted(public.rglob("*")):
                if not path.is_file():
                    continue
                info = archive.gettarinfo(str(path), str(path.relative_to(public.parent)))
                info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
                with path.open("rb") as body:
                    archive.addfile(info, body)
    samples = manifest["real_source_acceptance"]["samples"]
    counts = Counter(
        sample["class"] for sample in samples if sample["runtime_status"] == "complete"
    )
    assertions = json.loads(directory.joinpath("assertions.json").read_text())
    index = {
        "artifact_version": 1,
        "scope": "Machine-generated bounded real Source evidence; not a second human adaptation ledger",
        "base_head": manifest["git_head"],
        "environment": manifest["environment"],
        "full_local_evidence": str(directory),
        "full_local_verify": f"uv run --frozen python experiments/v1.2-acceptance/live_verify.py {directory}",
        "online_rerun": manifest["command"],
        "public_offline_verify": "tar -xzf real-evidence.tar.gz -C <new-directory>; uv run --frozen python experiments/v1.2-acceptance/live_verify.py <new-directory>/real --public",
        "assertions": {
            "passed": sum(a["status"] == "PASS" for a in assertions),
            "total": len(assertions),
        },
        "T7S": {
            "status": "PASS" if all(counts[c] >= 2 for c in "ABC") else "FAIL",
            "complete_sources_by_class": dict(counts),
            "partial_original_sources": [
                s["research_id"] for s in samples if s["runtime_status"] == "partial"
            ],
        },
        "samples": samples,
        "quality_status": "not_evaluated",
        "public_archive": {
            "path": bundle.name,
            "bytes": bundle.stat().st_size,
            "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        },
        "disclosure": {
            "withheld_raw_objects": len(withheld),
            "local_only_source_ids": sorted(private),
            "reason": "Potential anonymous session credentials remain local; no bytes were redacted or falsely hashed",
            "public_scope_limitation": "Raw field evidence of local_only_source_ids cannot be independently recomputed from this public package",
        },
    }
    write(index_path, index)
    print(json.dumps({"package": str(bundle), "index": str(index_path), "withheld": len(withheld)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package(args.directory.resolve(), args.output.resolve())
