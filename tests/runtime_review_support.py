"""Disposable deployment/browser copies for independent review acceptance.

This module invokes external CLIs and reads storage contracts. It never imports
SEAL implementation functions. Browser instrumentation records the executable
and its parent driver before exec'ing the real browser in the same directory.
"""

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from issue28_inputs import adjust
from issue28_runtime import ROOT, Runtime, pdf_bytes


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hash(root):
    value = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            value.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return value.hexdigest()


def wait_for(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("bounded external behavior wait expired")


class ReviewRuntime(Runtime):
    def __init__(self, source_root, label):
        super().__init__("runtime-review-" + label)
        self.source_root = Path(source_root).resolve()
        self.deployment = self.work / "deployment"
        shutil.copytree(self.source_root / "src", self.deployment)
        self.env["PYTHONPATH"] = str(self.deployment)
        self.env["PW_TEST_SCREENSHOT_NO_FONTS_READY"] = "1"
        self.initial_hash = tree_hash(self.deployment)
        self.trace = self.work / "browser-executions.jsonl"
        self.browser_root = self.work / "browsers"
        self.browser_root.mkdir()
        self.env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browser_root)
        self.browser_paths = []
        self.browser_installations = []
        self.test_hashes = {p.name: sha(p) for p in Path(__file__).parent.glob("runtime_review*.py")}

    def check(self, name, condition, evidence=None):
        self.checks.append({"assertion": name, "pass": bool(condition), "evidence": evidence})
        self.save("assertions.json", self.checks)

    def browser_copy(self):
        probe = "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()"
        original = Path(subprocess.check_output([sys.executable, "-c", probe], text=True).strip())
        install_root = next(p for p in original.parents if p.name.startswith("chromium-"))
        revision = install_root.name.split("-", 1)[1]
        for origin in (install_root, install_root.parent / ("chromium_headless_shell-" + revision)):
            target = self.browser_root / origin.name
            subprocess.run(["cp", "-cR", str(origin), str(target)], check=True)
        for path in self.browser_root.rglob("*"):
            if path.name in ("Google Chrome for Testing", "headless_shell", "chrome-headless-shell", "chrome") and path.is_file():
                real = path.with_name(path.name + ".acceptance-real")
                path.rename(real)
                self.browser_paths.append((path, real))
        if not self.browser_paths:
            raise AssertionError("no disposable browser launch executable found")
        self.browser_variant("A")
        self.save("browser-copy.json", {"revision": revision, "origin": str(original), "executables": [str(x[0]) for x in self.browser_paths]})

    def browser_variant(self, variant):
        for path, real in self.browser_paths:
            program = (
                "#!" + sys.executable + "\nimport hashlib,json,os,subprocess,sys\n"
                f"path={str(path)!r}\nreal={str(real)!r}\n"
                f"row={{'variant':{variant!r},'executable':path,'real_sha256':hashlib.sha256(open(real,'rb').read()).hexdigest(),'render_launch':'--remote-debugging-pipe' in sys.argv,'parent':subprocess.check_output(['ps','-p',str(os.getppid()),'-o','command='],text=True).strip()}}\n"
                f"with open({str(self.trace)!r},'a') as output: output.write(json.dumps(row)+'\\n')\n"
                f"os.execv(real,[real,*sys.argv[1:]])\n# acceptance browser installation {variant}\n"
            )
            path.write_text(program)
            path.chmod(0o755)
        self.browser_installations.append({"variant": variant, "executables": {str(path): sha(path) for path, _ in self.browser_paths}})
        self.save("browser-installations.json", self.browser_installations)

    def executions(self):
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def package_variant(self, package):
        target = self.work / (package + "-deployment")
        target.mkdir(exist_ok=True)
        origin = Path(importlib.util.find_spec(package).origin).parent
        shutil.copytree(origin, target / package)
        info = importlib.metadata.distribution(package)._path
        shutil.copytree(info, target / info.name)
        metadata = target / info.name / "METADATA"
        old = importlib.metadata.version(package)
        new = old + "+independent.review"
        metadata.write_text(metadata.read_text().replace("Version: " + old, "Version: " + new, 1))
        init = target / package / "__init__.py"
        init.write_text(init.read_text() + "\n__acceptance_distribution_build__ = 'independent.review'\n")
        env = dict(self.env, PYTHONPATH=os.pathsep.join((str(target), str(self.deployment))))
        return env, {"package": package, "original": old, "deployment": new, "sha256": tree_hash(target)}

    def interrupt_dom(self, source, env):
        a, b = "/retry/" + source + "/a", "/retry/" + source + "/b"
        self.fixture.html(a, "RETRY FIRST", script="<script>document.querySelector('h1').textContent='RETRY JAVASCRIPT FIRST';</script>")
        self.fixture.html(b, "RETRY SECOND", script="<script>document.querySelector('h1').textContent='RETRY JAVASCRIPT SECOND';</script>")
        config = self.config(source, [a, b])
        adjust(config, parser={"mode": "rendered_dom"})
        event = threading.Event()
        self.fixture.block[b] = event
        proc = self.spawn("collect", config, "--no-progress", env=env)
        wait_for(lambda: len(self.versions(source)) == 1 and b in self.fixture.requests)
        proc.send_signal(signal.SIGINT)
        proc.wait(30)
        event.set()
        del self.fixture.block[b]
        run = self.query("SELECT * FROM seal.run WHERE source_id=%s ORDER BY created_at DESC LIMIT 1", (source,))[0]
        if run["status"] != "retryable":
            raise AssertionError("interrupted real browser Run is not retryable")
        return run


def scanned_pdf():
    import io

    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PdfReader

    image = Image.new("RGB", (1400, 700), "white")
    canvas = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 66)
    for index, line in enumerate(("SCAN RECEIPT", "PUBLIC ALPHA 2026", "REAL OCR ACCEPTANCE")):
        canvas.text((70, 90 + 160 * index), line, font=font, fill="black")
    output = io.BytesIO()
    image.save(output, format="PDF", resolution=144)
    raw = output.getvalue()
    if any(page.extract_text().strip() for page in PdfReader(io.BytesIO(raw)).pages):
        raise AssertionError("OCR input unexpectedly has a text layer")
    return raw


def per_url(rt, source):
    rows = {}
    for row in rt.versions(source):
        rows.setdefault(row["url"], []).append(row)
    return rows


def sanitized_results(rt, name, sources):
    rt.save(name, {source: {"versions": rt.versions(source), "observations": rt.observations(source), "runs": rt.query("SELECT * FROM seal.run WHERE source_id=%s ORDER BY created_at", (source,))} for source in sources})


__all__ = ["ROOT", "ReviewRuntime", "adjust", "pdf_bytes", "per_url", "sanitized_results", "scanned_pdf", "sha", "tree_hash"]
