"""Run-owned, cancellable PDF processes with bounded output and group cleanup."""

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

from .config import OCRConfig
from .core import SealError
from .objects import work_directory
from .pdf_contract import PDF_PROCESS_ERRORS
from .pdf_result import validate
from .pdf_slots import PDFSlots

GRACE_SECONDS = 0.5
KILL_SECONDS = 1.0


async def uninterrupted(task):
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


def group_exists(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def stop_group(process):
    # A leader that has already exited can still have live descendants.
    for sig, seconds in ((signal.SIGTERM, GRACE_SECONDS), (signal.SIGKILL, KILL_SECONDS)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if process.returncode is not None and not group_exists(process.pid):
                await process.wait()
                if process.stdout:
                    # Drain only fixed-size chunks after all group writers exited;
                    # EOF closes the pipe transport without retaining late output.
                    async with asyncio.timeout(KILL_SECONDS):
                        while await process.stdout.read(65536):
                            pass
                return
            await asyncio.sleep(0.02)
    raise SealError("pdf_cleanup_failed")


class PDFRuntime:
    def __init__(self, state):
        self.state = state
        self.slots = PDFSlots()
        self.tasks = set()
        self.closed = False
        self.failed = False

    def check(self):
        if self.failed:
            raise SealError("pdf_cleanup_failed")
        self.slots.check()
        if self.closed or self.state.cancelled:
            raise SealError("interrupted")
        if time.monotonic() >= self.budget_deadline:
            raise SealError("crawl_time_budget_exceeded")

    @property
    def budget_deadline(self):
        return self.state.started + self.state.config["budget"]["seconds"]

    async def prepare(self, capture):
        task = asyncio.current_task()
        self.tasks.add(task)
        slot, folder, spawn = None, None, None
        cancelled = False
        try:
            self.check()
            async with asyncio.timeout_at(self.budget_deadline):
                slot = await self.slots.acquire(self.check)
            self.check()
            # Pending/retryable snapshots from before OCR have no key. Resolve
            # documented defaults without rewriting the original frozen config/hash.
            cfg = OCRConfig.model_validate(self.state.config.get("ocr", {})).model_dump()
            cfg["executable"] = (
                shutil.which(os.environ.get("SEAL_TESSERACT", "tesseract")) or "tesseract"
            )
            execution_deadline = time.monotonic() + cfg["timeout_seconds"]
            deadline = min(execution_deadline, self.budget_deadline)
            folder = Path(tempfile.mkdtemp(prefix="seal-pdf-", dir=work_directory()))
            (folder / "input.pdf").write_bytes(capture["body"])
            env = {
                k: os.environ[k]
                for k in ("PATH", "LANG", "LC_ALL", "TESSDATA_PREFIX")
                if k in os.environ
            }
            env["OMP_THREAD_LIMIT"] = "1"
            async with asyncio.timeout_at(deadline):
                self.check()
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "seal.pdf_worker",
                        cwd=folder,
                        env=env,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                    )
                )
                process = await asyncio.shield(spawn)
                process.stdin.write(json.dumps(cfg).encode())
                await process.stdin.drain()
                process.stdin.close()
                try:
                    header = await process.stdout.readuntil(b"\n")
                    if len(header) > 1024:
                        raise ValueError
                    header = json.loads(header)
                    if (
                        isinstance(header, dict)
                        and isinstance(header.get("error"), str)
                        and header["error"] in PDF_PROCESS_ERRORS
                    ):
                        raise SealError(header["error"])
                    if (
                        not isinstance(header, dict)
                        or set(header) != {"pages", "method"}
                        or type(header["pages"]) is not int
                        or header["pages"] < 1
                        or not isinstance(header["method"], str)
                        or header["method"] not in {"native", "ocr"}
                    ):
                        raise ValueError
                except asyncio.IncompleteReadError:
                    code = "pdf_ocr_failed" if await process.wait() != 0 else "pdf_output_invalid"
                    raise SealError(code) from None
                except (
                    ValueError,
                    UnicodeError,
                    asyncio.LimitOverrunError,
                ):
                    raise SealError("pdf_output_invalid") from None
                output = bytearray()
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > cfg["max_result_bytes"]:
                        raise SealError("pdf_output_too_large")
                if await process.wait() != 0:
                    raise SealError("pdf_ocr_failed")
                try:
                    value = json.loads(output)
                except (ValueError, UnicodeError):
                    raise SealError("pdf_output_invalid") from None
                if (
                    isinstance(value, dict)
                    and isinstance(value.get("error"), str)
                    and value["error"] in PDF_PROCESS_ERRORS
                ):
                    raise SealError(value["error"])
                result = validate(
                    value, len(capture["body"]), header["pages"], header["method"], cfg
                )
                self.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError
                return result
        except TimeoutError:
            code = (
                "crawl_time_budget_exceeded"
                if time.monotonic() >= self.budget_deadline
                else "pdf_ocr_timeout"
            )
            capture["error"] = code
            raise SealError(code) from None
        except SealError as exc:
            capture["error"] = exc.code
            raise
        except asyncio.CancelledError:
            cancelled = True
            capture["error"] = "interrupted"
            raise
        except (OSError, ConnectionError):
            capture["error"] = "pdf_ocr_failed"
            raise SealError("pdf_ocr_failed") from None
        finally:

            async def cleanup():
                if spawn is not None:
                    try:
                        process = await spawn
                    except OSError:
                        process = None
                    if process is not None:
                        await stop_group(process)
                        if process.stdin:
                            process.stdin.close()
                            try:
                                await process.stdin.wait_closed()
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                if folder is not None:
                    shutil.rmtree(folder)

            try:
                _, interrupted = await uninterrupted(asyncio.create_task(cleanup()))
                cancelled |= interrupted
            except Exception:
                self.failed = True
                self.slots.block()
                capture["error"] = "pdf_cleanup_failed"
                self.state.add_error("pdf_cleanup_failed")
                raise SealError("pdf_cleanup_failed") from None
            finally:
                self.tasks.discard(task)
                if slot is not None and not self.failed:
                    os.close(slot)
            if cancelled:
                raise asyncio.CancelledError

    def cancel(self):
        self.closed = True
        for task in tuple(self.tasks):
            task.cancel()

    async def close(self):
        self.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.failed:
            raise SealError("pdf_cleanup_failed")
