"""Controlled external recognizer/worker processes for lifecycle fault acceptance."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def event(path, kind, **extra):
    row = {"kind": kind, "pid": os.getpid(), "ppid": os.getppid(),
           "pgid": os.getpgrp(), "at": time.monotonic(), **extra}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(row) + "\n").encode())
    finally:
        os.close(fd)


def descendant(path):
    code = (
        "import os,signal,time,json;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "fd=os.open(" + repr(str(path)) + ",os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600);"
        "os.write(fd,(json.dumps(dict(kind='descendant',pid=os.getpid(),ppid=os.getppid(),"
        "pgid=os.getpgrp(),at=time.monotonic()))+'\\n').encode());os.close(fd);time.sleep(120)"
    )
    return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def recognize(mode, path):
    if "--list-langs" in sys.argv:
        print("List of available languages (3):\neng\nchi_sim\nvie")
        return
    if "--version" in sys.argv:
        print("tesseract 5.5 controlled acceptance recognizer")
        return
    event(path, "recognize_start", has_credentials=any(k.startswith(("SEAL_DATABASE", "SEAL_S3_", "MINIO_")) for k in os.environ))
    if mode in {"hang", "descendant"}:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        descendant(path)
        while True:
            time.sleep(1)
    if mode == "slow":
        time.sleep(1.5)
    if mode == "exit_nonzero":
        raise SystemExit(37)
    if mode == "empty":
        print("\n\f")
    elif mode == "stdout_flood":
        for _ in range(2048):
            sys.stdout.write("x" * 65536)
    else:
        if mode == "stderr_flood":
            for _ in range(64):
                os.write(2, b"x" * 65536)
        print("CONTROLLED OCR PROCESSING RESULT")
    event(path, "recognize_end")


def worker(mode, path):
    from pypdf import PdfReader

    cfg = json.loads(sys.stdin.buffer.read(8192))
    count = len(PdfReader("input.pdf").pages)
    if mode != "no_preflight":
        print(json.dumps({"pages": count, "method": "ocr"}), flush=True)
    event(path, "worker_start", has_credentials=any(k.startswith(("SEAL_DATABASE", "SEAL_S3_", "MINIO_")) for k in os.environ))
    if mode == "invalid_json":
        print("{INVALID")
        return
    if mode == "wrong_error_type":
        print('{"error": []}')
        return
    if mode == "wrong_method_type":
        print('{"method": []}')
        return
    if mode == "stdout_flood":
        for _ in range(2048):
            sys.stdout.write("x" * 65536)
        return
    if mode == "exit_nonzero":
        raise SystemExit(37)
    if mode == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        descendant(path)
        while True:
            time.sleep(1)
    page_count = count - 1 if mode in {"missing_page", "selfconsistent_missing"} else count
    pages = [{"page_number": i + 1, "raw_text": "" if mode == "empty" else "PAGE " + str(i + 1),
              "markdown": "PAGE " + str(i + 1), "html": "<p>PAGE " + str(i + 1) + "</p>",
              "images": [], "links": [], "layout": []} for i in range(page_count)]
    if mode == "wrong_index":
        pages[0]["page_number"] = 2
    if mode == "partial_failed":
        pages[-1]["error"] = "recognition_failed"
    metadata = {"title": None, "author": None, "producer": None, "created": None,
                "modified": None, "pages": page_count if mode == "selfconsistent_missing" else count,
                "encrypted": False, "file_size": Path("input.pdf").stat().st_size}
    value = {"method": "ocr", "result": {"metadata": metadata, "pages": pages,
              "processing_time": 0.0, "version": "acceptance-controlled"},
             "ocr": {"engine": "tesseract", "version": "controlled", "config": {k: v for k, v in cfg.items() if k != "executable"}}}
    if mode == "exit_with_descendant":
        descendant(path)
    if mode == "result_race":
        event(path, "result_ready")
        time.sleep(0.2)
    if mode == "slow":
        time.sleep(1.5)
    event(path, "worker_output")
    print(json.dumps(value))


def install(folder, mode, *, kind="recognizer", hook_mode=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "events.jsonl"
    executable = folder / "controlled-engine"
    executable.write_text(
        "#!" + sys.executable + "\nimport sys\nsys.path.insert(0," + repr(str(Path(__file__).parent)) + ")\n"
        "from issue25_engine import " + ("recognize" if kind == "recognizer" else "worker") + "\n"
        + ("recognize" if kind == "recognizer" else "worker") + "(" + repr(mode) + "," + repr(str(log)) + ")\n"
    )
    executable.chmod(0o700)
    if kind == "recognizer" and hook_mode is None:
        return {"SEAL_TESSERACT": str(executable)}, log
    hook = folder / "hooks"
    hook.mkdir(exist_ok=True)
    # Only replace the PDF child boundary; real collection/SQL/S3/API code runs unchanged.
    source = """
import asyncio, os, time, json, shutil, subprocess
_create = subprocess.Popen
_log = LOG
def _event(kind, **extra):
    fd=os.open(_log,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    try: os.write(fd,(json.dumps(dict(kind=kind,at=time.monotonic(),**extra))+'\\n').encode())
    finally: os.close(fd)
def _launch(args, *positional, **kwargs):
    target = isinstance(args,(list,tuple)) and 'seal.pdf_worker' in args
    if target and REPLACE:
        args=[EXECUTABLE]
    process=_create(args,*positional,**kwargs)
    if target:
        _event('spawn',pid=process.pid)
        if HOOK == 'launch_delay':
            time.sleep(0.7)
    return process
subprocess.Popen=_launch
if HOOK == 'pipe_delay':
    _connect=asyncio.BaseEventLoop.connect_read_pipe
    async def _pipe(self,*args,**kwargs):
        _event('pipe_ready',pid=os.getpid(),ppid=os.getppid())
        await asyncio.sleep(0.7)
        return await _connect(self,*args,**kwargs)
    asyncio.BaseEventLoop.connect_read_pipe=_pipe
if HOOK == 'cleanup_failure':
    _remove=shutil.rmtree
    def _rmtree(path,*args,**kwargs):
        if os.path.basename(str(path)).startswith('seal-pdf-'):
            _event('cleanup_refused')
            raise OSError('controlled cleanup failure')
        return _remove(path,*args,**kwargs)
    shutil.rmtree=_rmtree
if HOOK == 'archive_block':
    from botocore.client import BaseClient
    _api_call=BaseClient._make_api_call
    def _call(self,operation,params):
        body=params.get('Body')
        if operation == 'PutObject' and isinstance(body,bytes) and body.startswith(b'{'):
            try: value=json.loads(body)
            except ValueError: value={}
            if isinstance(value.get('result'),dict):
                _event('archive_block',pid=os.getpid(),ppid=os.getppid())
                time.sleep(1.5)
        return _api_call(self,operation,params)
    BaseClient._make_api_call=_call
"""
    source = source.replace("LOG", repr(str(log))).replace("EXECUTABLE", repr(str(executable)))
    source = source.replace("REPLACE", repr(kind == "worker")).replace("HOOK", repr(hook_mode))
    (hook / "sitecustomize.py").write_text(source)
    env = {"PYTHONPATH": str(hook)}
    if kind == "recognizer":
        env["SEAL_TESSERACT"] = str(executable)
    return env, log


def read_events(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines()]
