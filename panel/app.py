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
import hashlib
import hmac
import html
import json
import multiprocessing
import queue
import shutil
import sys
import threading
import time
import uuid
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
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
import trend_radar     # noqa: E402  (panel/trend_radar.py)
import radar_collector  # noqa: E402  (panel/radar_collector.py)

STATIC_DIR = PANEL_DIR / "static"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _radar_collector.start()
    try:
        yield
    finally:
        _radar_collector.stop()


app = FastAPI(title="Print Factory Panel", version="1.0", lifespan=_lifespan)

# Джобы — фон (генерация 1..50 картинок не должна упираться в HTTP-таймаут).
# max_workers=2 — держим нагрузку на Gemini-квоту и локальный CPU в разумных
# пределах, параллельные job-ы от нескольких вкладок не устраивают очередь на
# один поток, но и не заливают провайдера пачкой запросов разом.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="panel-job")
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Внешний TikTok oEmbed выполняется отдельно от платной генерации. Один worker
# ограничивает нагрузку и сохраняет порядок; состояние каждого задания живёт в
# SQLite, поэтому повторный запрос безопасен и не дублирует сигнал.
_radar_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="radar-ingest")
_radar_store = trend_radar.TrendRadarStore(settings.RADAR_DB_PATH)
_radar_collector = radar_collector.RadarCollector(
    _radar_store,
    radar_collector.CollectorConfig(
        enabled=settings.RADAR_AUTO_ENABLED,
        interval_seconds=settings.RADAR_COLLECTION_INTERVAL,
        initial_delay_seconds=settings.RADAR_INITIAL_DELAY,
        google_geo=settings.RADAR_GOOGLE_TRENDS_GEO,
        telegram_channels=settings.RADAR_TELEGRAM_CHANNELS,
        discovery_terms_per_run=settings.RADAR_DISCOVERY_TERMS_PER_RUN,
        posts_per_term=settings.RADAR_POSTS_PER_TERM,
        comments_posts_per_run=settings.RADAR_COMMENTS_POSTS_PER_RUN,
        posts_daily_limit=settings.BRIGHTDATA_POSTS_DAILY_LIMIT,
        comments_daily_limit=settings.BRIGHTDATA_COMMENTS_DAILY_LIMIT,
        records_daily_limit=settings.BRIGHTDATA_RECORDS_DAILY_LIMIT,
        comment_max_expected=settings.BRIGHTDATA_COMMENT_MAX_EXPECTED,
        price_per_1000=settings.BRIGHTDATA_PRICE_PER_1000,
    ),
    google=radar_collector.GoogleTrendsSource(
        geo=settings.RADAR_GOOGLE_TRENDS_GEO,
        timeout=settings.RADAR_REQUEST_TIMEOUT,
    ),
    telegram=radar_collector.TelegramPublicSource(
        settings.RADAR_TELEGRAM_CHANNELS,
        timeout=settings.RADAR_REQUEST_TIMEOUT,
    ),
    tiktok=radar_collector.BrightDataClient(
        settings.BRIGHTDATA_API_TOKEN,
        timeout=settings.RADAR_REQUEST_TIMEOUT,
    ),
)

_AUTH_COOKIE = "print_factory_access"
_AUTH_TOKEN_MESSAGE = b"print-factory-panel-session-v1"
_auth_failures: dict[str, deque[float]] = {}
_auth_failures_lock = threading.Lock()


class GenerateRequest(BaseModel):
    styles: list[str] = Field(default_factory=list)
    count: int = 1
    theme: str = ""
    characters: str = ""
    free_prompt: str = Field(default="", max_length=4000)


class RadarSignalRequest(BaseModel):
    term: str = Field(min_length=2, max_length=120)
    source_type: str = Field(default="tiktok", pattern="^(tiktok|telegram|youtube)$")
    source_url: str = Field(min_length=8, max_length=2048)
    author: str = Field(default="", max_length=120)
    caption: str = Field(default="", max_length=4000)
    comments: str = Field(default="", max_length=100_000)
    published_at: datetime | None = None
    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    shares: int = Field(default=0, ge=0)
    comments_count: int = Field(default=0, ge=0)


class RadarBatchRequest(BaseModel):
    signals: list[RadarSignalRequest] = Field(min_length=1, max_length=100)


class RadarGenerateRequest(BaseModel):
    count: int = Field(default=3, ge=1, le=6)


class RadarCommentsRequest(BaseModel):
    confirmed: bool = False


def _auth_enabled() -> bool:
    return bool(settings.ACCESS_PASSWORD_SHA256)


def _session_token() -> str:
    """Непарольный cookie-token, детерминированный для текущего password hash."""
    if not _auth_enabled():
        return ""
    key = bytes.fromhex(settings.ACCESS_PASSWORD_SHA256)
    return hmac.new(key, _AUTH_TOKEN_MESSAGE, hashlib.sha256).hexdigest()


def _has_access(request: Request) -> bool:
    if not _auth_enabled():
        return True
    supplied = request.cookies.get(_AUTH_COOKIE, "")
    return bool(supplied) and hmac.compare_digest(supplied, _session_token())


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _failed_login_is_limited(client_key: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    cutoff = now - settings.AUTH_FAILURE_WINDOW
    with _auth_failures_lock:
        attempts = _auth_failures.setdefault(client_key, deque())
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        return len(attempts) >= settings.AUTH_FAILURE_LIMIT


def _record_failed_login(client_key: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _auth_failures_lock:
        _auth_failures.setdefault(client_key, deque()).append(now)


def _clear_failed_logins(client_key: str) -> None:
    with _auth_failures_lock:
        _auth_failures.pop(client_key, None)


def _safe_next(value: str) -> str:
    value = (value or "/").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def _login_html(next_path: str = "/", error: str = "") -> str:
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход — Print Factory</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;display:grid;place-items:center;
background:#1e1f24;color:#e8e8ec;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif}}
.card{{width:min(390px,calc(100vw - 32px));background:#26272e;border:1px solid #3a3b45;
border-radius:14px;padding:28px;box-shadow:0 18px 60px #0006}} h1{{font-size:24px;margin:0 0 6px}}
h1 span{{color:#0ba34d}} p{{color:#9a9ba6;margin:0 0 22px;font-size:14px}}
label{{display:block;color:#b9bac3;font-size:13px;margin-bottom:7px}}
input{{width:100%;border:1px solid #444650;border-radius:9px;padding:12px;background:#2e2f37;
color:#fff;font:inherit;outline:none}} input:focus{{border-color:#0ba34d}}
button{{width:100%;margin-top:14px;border:0;border-radius:9px;padding:12px;background:#0ba34d;
color:#fff;font:inherit;font-weight:650;cursor:pointer}} .error{{margin:0 0 14px;padding:10px;
border-radius:8px;background:#4a2226;color:#ffb3b8;font-size:13px}}
.note{{margin-top:16px;color:#777985;font-size:11px;text-align:center}}
</style></head><body><main class="card"><h1>Print <span>Factory</span></h1>
<p>Введите пароль для доступа к панели генерации.</p>{error_html}
<form method="post" action="/login">
<input type="hidden" name="next" value="{html.escape(_safe_next(next_path), quote=True)}">
<label for="password">Пароль</label>
<input id="password" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Войти</button></form>
<div class="note">Доступ ограничен владельцем сервера</div></main></body></html>"""


@app.middleware("http")
async def require_panel_access(request: Request, call_next):
    if not _auth_enabled() or request.url.path in {"/health", "/login"}:
        return await call_next(request)
    if _has_access(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "требуется вход"}, status_code=401)
    next_path = request.url.path
    if request.url.query:
        next_path += "?" + request.url.query
    return RedirectResponse(url=f"/login?next={quote(next_path, safe='/')}", status_code=303)


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    if not _auth_enabled() or _has_access(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return HTMLResponse(_login_html(next))


@app.post("/login")
async def login(request: Request):
    if not _auth_enabled():
        return RedirectResponse(url="/", status_code=303)
    client_key = _client_key(request)
    if _failed_login_is_limited(client_key):
        return HTMLResponse(
            _login_html("/", "Слишком много попыток. Повторите через несколько минут."),
            status_code=429,
        )
    if int(request.headers.get("content-length", "0") or 0) > 4096:
        raise HTTPException(status_code=413, detail="слишком большой запрос")
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    password = form.get("password", [""])[0]
    next_path = _safe_next(form.get("next", ["/"])[0])
    supplied_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(supplied_hash, settings.ACCESS_PASSWORD_SHA256):
        _record_failed_login(client_key)
        return HTMLResponse(_login_html(next_path, "Неверный пароль"), status_code=401)

    _clear_failed_logins(client_key)
    response = RedirectResponse(url=next_path, status_code=303)
    response.set_cookie(
        _AUTH_COOKIE,
        _session_token(),
        max_age=settings.AUTH_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(_AUTH_COOKIE, path="/")
    return response


def _style_bank() -> list[dict]:
    try:
        data = json.loads(settings.STYLE_BANK_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.error(f"не смог прочитать {settings.STYLE_BANK_PATH}: {e}")
        return []
    return [
        {
            "id": s["id"],
            "name_ru": s.get("name_ru", s["id"]),
            "theme_optional": bool(s.get("theme_optional", False)),
        }
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
    if (
        not (req.theme or "").strip()
        and not (req.characters or "").strip()
        and not (req.free_prompt or "").strip()
        and not orchestrator.allows_theme_free(list(req.styles))
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "укажи тему, персонажей, свободный запрос или выбери автономный стиль"
            ),
        )

    return _enqueue_generation(
        list(req.styles),
        req.count,
        req.theme,
        req.characters,
        req.free_prompt,
    )


def _enqueue_generation(
    styles: list[str],
    count: int,
    theme: str,
    characters: str,
    free_prompt: str,
) -> dict:
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "queued", "done": 0, "total": count,
            "items": [], "paths": {}, "outdir": None, "error": None,
            "cancel_event": threading.Event(),
            "created": time.time(),
        }
        _prune_old_jobs_locked()

    _executor.submit(
        _run_job,
        job_id,
        list(styles),
        count,
        theme,
        characters,
        free_prompt,
    )
    return {"job_id": job_id}


def _ingest_radar_request(req: RadarSignalRequest) -> dict:
    result = _radar_store.ingest_signal(
        trend_radar.SignalInput(
            term=req.term,
            source_type=req.source_type,
            source_url=req.source_url,
            author=req.author,
            caption=req.caption,
            comments=req.comments,
            published_at=req.published_at,
            views=req.views,
            likes=req.likes,
            shares=req.shares,
            comments_count=req.comments_count,
        )
    )
    if not result["duplicate"] and result["job_status"] == "pending":
        _radar_executor.submit(_radar_store.run_ingest_job, result["job_id"])
    return result


@app.post("/api/radar/signals", status_code=202)
def api_radar_signal(req: RadarSignalRequest):
    try:
        result = _ingest_radar_request(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result["trend"] = _radar_store.get_trend(result["trend_id"])
    return result


@app.post("/api/radar/signals/batch", status_code=202)
def api_radar_signals_batch(req: RadarBatchRequest):
    results = []
    errors = []
    for index, signal in enumerate(req.signals):
        try:
            results.append(_ingest_radar_request(signal))
        except ValueError as exc:
            errors.append({"index": index, "error": str(exc)})
    return {
        "received": len(req.signals),
        "created": sum(not item["duplicate"] for item in results),
        "updated": sum(item["duplicate"] and item["updated"] for item in results),
        "unchanged": sum(item["duplicate"] and not item["updated"] for item in results),
        "errors": errors,
        "results": results,
    }


@app.get("/api/radar/collector/status")
def api_radar_collector_status():
    return _radar_collector.status()


@app.post("/api/radar/collector/run", status_code=202)
def api_radar_collector_run():
    result = _radar_collector.queue_run("owner")
    if not result["queued"]:
        return JSONResponse(result, status_code=409)
    return result


@app.post("/api/radar/collector/stop")
def api_radar_collector_stop():
    return _radar_collector.cancel_run()


@app.post("/api/radar/trends/{trend_id}/comments", status_code=202)
def api_radar_comments(trend_id: str, req: RadarCommentsRequest):
    try:
        return _radar_collector.queue_comments(
            trend_id, confirmed=req.confirmed,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="тренд не найден") from exc
    except trend_radar.BrightDataBudgetExceeded as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/radar/comments/jobs/{job_id}")
def api_radar_comments_job(job_id: str):
    job = _radar_collector.comment_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="задание комментариев не найдено")
    return job


@app.get("/api/radar/seeds")
def api_radar_seeds(limit: int = 30):
    return _radar_store.list_seeds(limit=limit)


@app.get("/api/radar/jobs/{job_id}")
def api_radar_job(job_id: str):
    job = _radar_store.job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="задание радара не найдено")
    return job


@app.get("/api/radar/trends")
def api_radar_trends(limit: int = 50):
    return _radar_store.list_trends(limit=limit)


@app.get("/api/radar/opportunities")
def api_radar_opportunities(limit: int = 30):
    return _radar_store.list_opportunities(limit=limit)


@app.get("/api/radar/trends/{trend_id}")
def api_radar_trend(trend_id: str):
    trend = _radar_store.get_trend(trend_id)
    if trend is None:
        raise HTTPException(status_code=404, detail="тренд не найден")
    return trend


@app.post("/api/radar/trends/{trend_id}/approve")
def api_radar_approve(trend_id: str):
    try:
        return _radar_store.set_decision(trend_id, "approve")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="тренд не найден") from exc


@app.post("/api/radar/trends/{trend_id}/reject")
def api_radar_reject(trend_id: str):
    try:
        return _radar_store.set_decision(trend_id, "reject")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="тренд не найден") from exc


@app.post("/api/radar/trends/{trend_id}/generate")
def api_radar_generate(trend_id: str, req: RadarGenerateRequest):
    try:
        prompt = _radar_store.generation_prompt(trend_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="тренд не найден") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _enqueue_generation([], req.count, "", "", prompt)


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
    free_prompt: str,
    outdir_text: str,
    events,
) -> None:
    """Планирует и рендерит job, отправляя родителю только простые события."""
    try:
        tasks = orchestrator.plan_tasks(styles, count, theme, characters, free_prompt)
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


def _run_job(
    job_id: str,
    styles: list[str],
    count: int,
    theme: str,
    characters: str,
    free_prompt: str,
) -> None:
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
        args=(styles, count, theme, characters, free_prompt, str(outdir), events),
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
