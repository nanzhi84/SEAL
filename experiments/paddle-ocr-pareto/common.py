"""Frozen experiment paths and file integrity; no product dependencies."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config"
SETTINGS = json.loads((CONFIG / "benchmark.json").read_text())
RUN = SETTINGS["run"]
WORK = ROOT / "work" / RUN
OUT = ROOT / "artifacts" / RUN


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def relative(path):
    return str(Path(path).resolve().relative_to(ROOT))


def inventory(directory):
    return {relative(p): sha(p) for p in sorted(Path(directory).rglob("*")) if p.is_file()}
