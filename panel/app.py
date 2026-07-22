# -*- coding: utf-8 -*-
"""app.py — веб-панель генерации принтов поверх движка print-factory-nb.

Тонкая FastAPI-обёртка: чекбоксы стилей + тема/персонажи/количество -> фоновый
job -> прогресс + превью по мере готовности + ZIP. Вся генерация — существующий
движок (art_director/franchise_scout/batch_print), панель ничего не меняет в
логике генерации — см. panel/PLAN.md и panel/orchestrator.py.

Запуск локально (из panel/):
    uvicorn app:app --host 0.0.0.0 --port 8040
"""
from __future__ import annotations

import io
import json
import multiprocessing
import queue
import shutil
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

PANEL_DIR = Path(__file__).resolve().parent
if str(PANEL_DIR) not in sys.path:
    sys.path.insert(0, str(PANEL_DIR))
ENGINE_ROOT = PANEL_DIR.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

import settings        # noqa: E402  (panel/settings.py)
import orchestrator    # noqa: E402  (panel/orchestrator.py)

STATIC_DIR = PANEL_DIR / "static"

app = FastAPI(title="Print Factory Panel", version="1.0")

# Джобы — фон (генерация 1..50 картинок не должна упираться в HTTP-таймаут).
# max_workers=2 — держим нагрузку на Gemini-квоту и локальный CPU в разумных
# пределах, параллельные job-ы от нескольких вкладок не устраивают очередь на
# один поток, но и не заливают провайдера пачкой запросов разом.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="panel-job")
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


class GenerateRequest(BaseModel):
    styles: list[str] = Field(default_factory=list)
    count: int = 1
    theme: str = ""
    characters: str = ""


def _style_bank() -> list[dict]:
    try:
        data = json.loads(settings.STYLE_BANK_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.error(f"не смог прочитать {settings.STYLE_BANK_PATH}: {e}")
        return []
    return [
        {"id": s["id"], "name_ru": s.get("name_ru", s["id"])}
        for s in data.get("styles", [])
        if s.get("id")
    ]


@app.get("/health")
def health():
    return {"status": "ok", "service": "print-factory-panel"}


@app.get("/api/styles")
def api_styles():
    return _style_bank()


@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    if req.count < 1:
        raise HTTPException(status_code=400, detail="count должен быть не меньше 1")
    if req.count > settings.MAX_COUNT:
        raise HTTPException(status_code=400, detail=f"максимум {settings.MAX_COUNT} за один запуск")
    if not (req.theme or "").strip() and not (req.characters or "").strip():
        raise HTTPException(status_code=400, detail="укажи тему или персонажей")

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "queued", "done": 0, "total": req.count,
            "items": [], "paths": {}, "outdir": None, "error": None,
            "cancel_event": threading.Event(),
            "created": time.time(),
        }
        _prune_old_jobs_locked()

    _executor.submit(_run_job, job_id, list(req.styles), req.count, req.theme, req.characters)
    return {"job_id": job_id}


def _prune_old_jobs_locked() -> None:
    """Держит не больше settings.JOB_HISTORY_LIMIT завершённых job-ов в памяти
    и на диске (панель может работать неделями без рестарта). Вызывать ТОЛЬКО
    под _jobs_lock."""
    finished = [
        (jid, j)
        for jid, j in _jobs.items()
        if j["status"] in ("done", "error", "cancelled")
    ]
    if len(finished) <= settings.JOB_HISTORY_LIMIT:
        return
    finished.sort(key=lambda kv: kv[1]["created"])
    for jid, j in finished[: len(finished) - settings.JOB_HISTORY_LIMIT]:
        outdir = j.get("outdir")
        if outdir:
            shutil.rmtree(outdir, ignore_errors=True)
        _jobs.pop(jid, None)


def _job_process_context():
    """Отдельный процесс можно действительно остановить, в отличие от Python-потока."""
    return multiprocessing.get_context("spawn")


def _job_process_entry(
    styles: list[str],
    count: int,
    theme: str,
    characters: str,
    outdir_text: str,
    events,
) -> None:
    """Планирует и рендерит job, отправляя родителю только простые события."""
    try:
        tasks = orchestrator.plan_tasks(styles, count, theme, characters)
    except Exception as e:  # noqa: BLE001
        events.put({"type": "error", "error": f"план не построился: {e}"})
        return

    outdir = Path(outdir_text)
    outdir.mkdir(parents=True, exist_ok=True)
    events.put({"type": "planned", "total": len(tasks)})

    for task in tasks:
        try:
            result = orchestrator.render_task(task, outdir)
        except Exception as e:  # noqa: BLE001 — один дизайн не должен ронять весь job
            logger.error(f"job worker/{task.tag}: {e}")
            result = {"tag": task.tag, "ok": False, "path": None, "error": str(e)}
        events.put(
            {
                "type": "item",
                "result": {
                    "tag": result["tag"],
                    "ok": bool(result["ok"]),
                    "path": str(result["path"]) if result.get("path") else None,
                    "error": result.get("error"),
                },
            }
        )

    events.put({"type": "finished"})


def _terminate_job_process(process) -> None:
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=3)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)


def _run_job(job_id: str, styles: list[str], count: int, theme: str, characters: str) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        if job["cancel_event"].is_set():
            job["status"] = "cancelled"
            return
        outdir = settings.OUTPUT_DIR / job_id
        outdir.mkdir(parents=True, exist_ok=True)
        job["status"] = "running"
        job["outdir"] = outdir

    context = _job_process_context()
    events = context.Queue()
    process = context.Process(
        target=_job_process_entry,
        args=(styles, count, theme, characters, str(outdir), events),
        name=f"print-job-{job_id}",
    )
    try:
        process.start()
    except Exception as e:  # noqa: BLE001
        logger.exception(f"job {job_id}: процесс не запустился")
        with _jobs_lock:
            job["status"] = "error"
            job["error"] = str(e)
        return

    dead_without_event_checks = 0

    def record_progress_event(event: dict) -> None:
        event_type = event.get("type")
        if event_type == "planned":
            with _jobs_lock:
                job["total"] = int(event["total"])
        elif event_type == "item":
            result = event["result"]
            with _jobs_lock:
                item = {
                    "tag": result["tag"],
                    "ok": bool(result["ok"]),
                    "error": result.get("error"),
                }
                job["items"].append(item)
                if result["ok"] and result.get("path"):
                    job["paths"][result["tag"]] = Path(result["path"])
                job["done"] += 1

    try:
        while True:
            if job["cancel_event"].is_set():
                _terminate_job_process(process)
                # Не теряем результаты, которые worker уже успел положить в очередь.
                while True:
                    try:
                        pending_event = events.get(timeout=0.05)
                    except queue.Empty:
                        break
                    record_progress_event(pending_event)
                with _jobs_lock:
                    job["status"] = "cancelled"
                    job["error"] = None
                logger.info(f"job {job_id}: остановлен на {job['done']}/{job['total']}")
                return

            try:
                event = events.get(timeout=0.2)
                dead_without_event_checks = 0
            except queue.Empty:
                if process.is_alive():
                    continue
                # Queue может доставить последнее событие чуть позже завершения процесса.
                dead_without_event_checks += 1
                if dead_without_event_checks < 5:
                    continue
                with _jobs_lock:
                    job["status"] = "error"
                    job["error"] = f"процесс генерации завершился с кодом {process.exitcode}"
                return

            event_type = event.get("type")
            if event_type in ("planned", "item"):
                record_progress_event(event)
            elif event_type == "error":
                with _jobs_lock:
                    job["status"] = "error"
                    job["error"] = event["error"]
                return
            elif event_type == "finished":
                with _jobs_lock:
                    job["status"] = "done"
                logger.info(f"job {job_id}: готово {job['done']}/{job['total']}")
                return
    finally:
        process.join(timeout=2)
        _terminate_job_process(process)
        try:
            events.cancel_join_thread()
            events.close()
        except (AttributeError, OSError, ValueError):
            pass


@app.post("/api/job/{job_id}/cancel")
def api_cancel_job(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job не найден")
    with _jobs_lock:
        accepted = job["status"] in ("queued", "running", "cancelling")
        if accepted:
            job["status"] = "cancelling"
            job["cancel_event"].set()
        return {"accepted": accepted, "status": job["status"]}


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job не найден")
    with _jobs_lock:
        items = [
            {
                "tag": it["tag"],
                "ok": it["ok"],
                "error": it["error"],
                "thumb_url": f"/api/thumb/{job_id}/{it['tag']}" if it["ok"] else None,
                "file_url": f"/api/file/{job_id}/{it['tag']}" if it["ok"] else None,
            }
            for it in job["items"]
        ]
        return {
            "status": job["status"], "done": job["done"], "total": job["total"],
            "items": items, "error": job["error"],
            "can_cancel": job["status"] in ("queued", "running", "cancelling"),
        }


@app.get("/api/thumb/{job_id}/{tag}")
def api_thumb(job_id: str, tag: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job не найден")
    path = job["paths"].get(tag)
    if path is None or not Path(path).exists():
        raise HTTPException(status_code=404, detail="файл не найден")
    return FileResponse(path, media_type="image/png")


@app.get("/api/file/{job_id}/{tag}")
def api_file(job_id: str, tag: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job не найден")
    path = job["paths"].get(tag)
    if path is None or not Path(path).exists():
        raise HTTPException(status_code=404, detail="файл не найден")
    return FileResponse(path, media_type="image/png", filename=f"{tag}.png")


@app.get("/api/download/{job_id}")
def api_download(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job не найден")
    if not job["paths"]:
        raise HTTPException(status_code=404, detail="нет готовых файлов")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for tag, path in job["paths"].items():
            p = Path(path)
            if p.exists():
                zf.write(p, arcname=f"{tag}.png")
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="prints_{job_id}.zip"'},
    )


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# статика — монтируем в конце, чтобы не перехватывать / и /api (как в GreenKey/web/app.py)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
