# -*- coding: utf-8 -*-
"""test_app.py — сквозной мок-тест FastAPI-эндпоинтов панели (job -> прогресс ->
превью -> ZIP), engine-вызовы (art_director.make_ideas/batch_print.render_design)
монкипатчатся — НИ ОДНОГО платного вызова."""
import io
import multiprocessing
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as panel_app
import orchestrator


@pytest.fixture(autouse=True)
def _fork_job_processes(monkeypatch):
    """Fork сохраняет monkeypatch-моки внутри тестового worker-процесса Linux."""
    if "fork" in multiprocessing.get_all_start_methods():
        monkeypatch.setattr(
            panel_app,
            "_job_process_context",
            lambda: multiprocessing.get_context("fork"),
        )


def _fake_design():
    return [{"prompt": "test", "chroma": "green", "style_id": "01_baroque_frame"}]


def _fake_render_design(design, tag, outdir, **kw):
    p = outdir / f"{tag}.png"
    Image.new("RGB", (4, 4), (0, 200, 0)).save(p)
    return {"ok": True, "green": str(p), "error": None}


def _wait_job_done(client, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/job/{job_id}").json()
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} не завершился за {timeout}s")


def test_health():
    client = TestClient(panel_app.app)
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_api_styles_reads_real_bank():
    client = TestClient(panel_app.app)
    res = client.get("/api/styles")
    assert res.status_code == 200
    styles = res.json()
    assert isinstance(styles, list) and len(styles) > 0
    assert all({"id", "name_ru"} <= set(s.keys()) for s in styles)
    assert any(s["id"] == "34_anime_magazine_cover" for s in styles)


def test_generate_rejects_empty_theme_and_characters():
    client = TestClient(panel_app.app)
    res = client.post("/api/generate", json={"styles": [], "count": 2, "theme": "", "characters": ""})
    assert res.status_code == 400


def test_generate_rejects_count_over_limit():
    client = TestClient(panel_app.app)
    res = client.post("/api/generate", json={
        "styles": [], "count": panel_app.settings.MAX_COUNT + 1, "theme": "тачки", "characters": "",
    })
    assert res.status_code == 400


def test_full_job_progress_thumbs_and_zip(monkeypatch):
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: _fake_design())
    monkeypatch.setattr(orchestrator.batch_print, "render_design", _fake_render_design)

    client = TestClient(panel_app.app)
    res = client.post("/api/generate", json={
        "styles": ["01_baroque_frame"], "count": 3, "theme": "тачки", "characters": "",
    })
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    job = _wait_job_done(client, job_id)
    assert job["status"] == "done"
    assert job["total"] == 3
    assert job["done"] == 3
    assert len(job["items"]) == 3
    assert all(it["ok"] for it in job["items"])
    assert all(it["thumb_url"] for it in job["items"])
    assert all(it["file_url"] for it in job["items"])
    assert job["can_cancel"] is False

    # превью реально отдаётся
    thumb_res = client.get(job["items"][0]["thumb_url"])
    assert thumb_res.status_code == 200
    assert thumb_res.headers["content-type"] == "image/png"

    # Оригинал каждой позиции можно скачать отдельно.
    file_res = client.get(job["items"][0]["file_url"])
    assert file_res.status_code == 200
    assert file_res.headers["content-type"] == "image/png"
    assert "attachment" in file_res.headers["content-disposition"]

    # ZIP содержит все 3 готовых PNG
    zip_res = client.get(f"/api/download/{job_id}")
    assert zip_res.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(zip_res.content))
    names = zf.namelist()
    assert len(names) == 3
    assert all(n.endswith(".png") for n in names)


def test_job_with_partial_failures(monkeypatch):
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: _fake_design())

    def flaky_render_design(design, tag, outdir, **kw):
        # Детерминированно ПО СЛОТУ (номер в начале тега): чётные слоты ВСЕГДА
        # падают — ретрай их не спасает, проверяем именно устойчивый частичный
        # сбой. (Раньше мок падал «каждый 2-й вызов» — с авто-ретраем render_task
        # такой слот вытягивался на повторе, ломая проверку числа сбоев.)
        idx = int(tag.split("_", 1)[0])
        if idx % 2 == 0:
            return {"ok": False, "error": "border coverage low"}
        return _fake_render_design(design, tag, outdir, **kw)
    monkeypatch.setattr(orchestrator.batch_print, "render_design", flaky_render_design)

    client = TestClient(panel_app.app)
    res = client.post("/api/generate", json={
        "styles": [], "count": 4, "theme": "тачки", "characters": "",
    })
    job_id = res.json()["job_id"]
    job = _wait_job_done(client, job_id)

    assert job["status"] == "done"
    ok_items = [it for it in job["items"] if it["ok"]]
    err_items = [it for it in job["items"] if not it["ok"]]
    assert len(ok_items) == 2
    assert len(err_items) == 2
    assert all(it["error"] for it in err_items)

    # ZIP собирается только из успешных
    zip_res = client.get(f"/api/download/{job_id}")
    zf = zipfile.ZipFile(io.BytesIO(zip_res.content))
    assert len(zf.namelist()) == 2


def test_running_job_can_be_force_cancelled_and_keeps_completed_files(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: _fake_design())
    marker = tmp_path / "second-render-started"

    def blocking_render_task(task, outdir):
        slot = int(task.tag.split("_", 1)[0])
        if slot == 2:
            marker.touch()
            time.sleep(30)
        path = outdir / f"{task.tag}.png"
        Image.new("RGB", (4, 4), (0, 200, 0)).save(path)
        return {"tag": task.tag, "ok": True, "path": path, "error": None}

    monkeypatch.setattr(orchestrator, "render_task", blocking_render_task)
    client = TestClient(panel_app.app)
    res = client.post("/api/generate", json={
        "styles": ["01_baroque_frame"], "count": 3, "theme": "тачки", "characters": "",
    })
    job_id = res.json()["job_id"]

    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists(), "worker не дошёл до зависшего второго рендера"

    started = time.monotonic()
    cancel_res = client.post(f"/api/job/{job_id}/cancel")
    assert cancel_res.status_code == 200
    assert cancel_res.json()["accepted"] is True
    job = _wait_job_done(client, job_id, timeout=5)

    assert time.monotonic() - started < 5
    assert job["status"] == "cancelled"
    assert job["done"] == 1
    assert job["total"] == 3
    assert len(job["items"]) == 1
    assert job["items"][0]["ok"] is True
    assert job["can_cancel"] is False

    zip_res = client.get(f"/api/download/{job_id}")
    assert zip_res.status_code == 200
    assert len(zipfile.ZipFile(io.BytesIO(zip_res.content)).namelist()) == 1

    # Повторный stop терминального задания безопасен и ничего не меняет.
    again = client.post(f"/api/job/{job_id}/cancel")
    assert again.status_code == 200
    assert again.json() == {"accepted": False, "status": "cancelled"}


def test_frontend_contains_cancel_and_individual_preview_controls():
    client = TestClient(panel_app.app)
    html = client.get("/").text
    assert 'id="stopBtn"' in html
    assert 'id="previewModal"' in html
    assert "openPreview" in html
    assert "/cancel" in html


def test_unknown_job_404():
    client = TestClient(panel_app.app)
    assert client.get("/api/job/doesnotexist").status_code == 404
    assert client.get("/api/download/doesnotexist").status_code == 404
    assert client.get("/api/file/doesnotexist/file").status_code == 404
    assert client.post("/api/job/doesnotexist/cancel").status_code == 404
