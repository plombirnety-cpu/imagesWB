# -*- coding: utf-8 -*-
"""test_app.py — сквозной мок-тест FastAPI-эндпоинтов панели (job -> прогресс ->
превью -> ZIP), engine-вызовы (art_director.make_ideas/batch_print.render_design)
монкипатчатся — НИ ОДНОГО платного вызова."""
import io
import time
import zipfile

from fastapi.testclient import TestClient
from PIL import Image

import app as panel_app
import orchestrator


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
        if job["status"] in ("done", "error"):
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

    # превью реально отдаётся
    thumb_res = client.get(job["items"][0]["thumb_url"])
    assert thumb_res.status_code == 200
    assert thumb_res.headers["content-type"] == "image/png"

    # ZIP содержит все 3 готовых PNG
    zip_res = client.get(f"/api/download/{job_id}")
    assert zip_res.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(zip_res.content))
    names = zf.namelist()
    assert len(names) == 3
    assert all(n.endswith(".png") for n in names)


def test_job_with_partial_failures(monkeypatch):
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: _fake_design())

    calls = {"n": 0}

    def flaky_render_design(design, tag, outdir, **kw):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
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


def test_unknown_job_404():
    client = TestClient(panel_app.app)
    assert client.get("/api/job/doesnotexist").status_code == 404
    assert client.get("/api/download/doesnotexist").status_code == 404
