# -*- coding: utf-8 -*-
import io
import queue
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

import app as panel_app
import virtual_studio


def _png_bytes(size=(256, 256)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, (255, 20, 20, 220)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_model_catalog_has_three_women_and_two_men():
    models = virtual_studio.public_models()
    assert len(models) == 5
    assert sum(model["gender"] == "female" for model in models) == 3
    assert sum(model["gender"] == "male" for model in models) == 2
    assert all(model["available"] for model in models)
    assert all(model["image_url"].startswith("/static/studio/models/") for model in models)


def test_render_mockup_uses_identity_and_artwork(monkeypatch, tmp_path):
    artwork_path = tmp_path / "print.png"
    Image.new("RGBA", (300, 500), (255, 30, 30, 220)).save(artwork_path)
    captured = {}

    def fake_generate(prompt, references, model=None):
        captured["prompt"] = prompt
        captured["references"] = references
        captured["model"] = model
        return Image.new("RGB", (512, 768), (230, 230, 230))

    monkeypatch.setattr(virtual_studio.providers, "generate_image_with_references", fake_generate)
    output = virtual_studio.render_mockup(
        model_id="alisa",
        artwork_path=artwork_path,
        shirt_color="black",
        placement="front",
        pose_index=1,
        output_path=tmp_path / "result.png",
        quality="standard",
    )

    assert output.is_file()
    assert len(captured["references"]) == 2
    assert captured["references"][0].mode == "RGB"
    assert captured["references"][1].mode == "RGBA"
    assert "same facial identity" in captured["prompt"]
    assert "exact print artwork" in captured["prompt"]
    assert "front chest" in captured["prompt"]


def test_studio_process_entry_emits_one_item_per_pose(monkeypatch, tmp_path):
    artwork = tmp_path / "print.png"
    Image.new("RGBA", (256, 256), (10, 20, 30, 255)).save(artwork)

    def fake_render(**kwargs):
        path = Path(kwargs["output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 48), (120, 120, 120)).save(path)
        return path

    monkeypatch.setattr(virtual_studio, "render_mockup", fake_render)
    events = queue.Queue()
    panel_app._studio_process_entry(
        "alisa",
        [("print.png", str(artwork))],
        "white",
        "back",
        3,
        "standard",
        str(tmp_path / "job"),
        events,
    )
    received = [events.get_nowait() for _ in range(4)]
    assert [event["type"] for event in received] == ["item", "item", "item", "finished"]
    assert all(event["result"]["ok"] for event in received[:3])


def test_studio_api_accepts_upload_and_queues_job(monkeypatch):
    submitted = []
    monkeypatch.setattr(panel_app._studio_executor, "submit", lambda *args, **kwargs: submitted.append((args, kwargs)))
    client = TestClient(panel_app.app)
    response = client.post(
        "/api/studio/render",
        data={
            "model_id": "artem",
            "shirt_color": "black",
            "placement": "front",
            "pose_count": "2",
            "quality": "standard",
        },
        files=[("prints", ("sample.png", _png_bytes(), "image/png"))],
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["total"] == 2
    assert submitted
    job_id = payload["job_id"]
    job = panel_app._studio_jobs[job_id]
    assert job["status"] == "queued"
    assert list((Path(job["outdir"]) / "uploads").glob("*.png"))
    shutil.rmtree(job["outdir"], ignore_errors=True)
    panel_app._studio_jobs.pop(job_id, None)


def test_studio_api_validates_batch_cost_before_queueing():
    client = TestClient(panel_app.app)
    files = [("prints", (f"p{index}.png", _png_bytes(), "image/png")) for index in range(4)]
    response = client.post(
        "/api/studio/render",
        data={
            "model_id": "maya",
            "shirt_color": "white",
            "placement": "back",
            "pose_count": "4",
            "quality": "premium",
        },
        files=files,
    )
    assert response.status_code == 400
    assert "12" in response.json()["detail"]


def test_studio_frontend_controls_are_present():
    client = TestClient(panel_app.app)
    html = client.get("/").text
    assert 'id="studioTab"' in html
    assert 'id="studioView"' in html
    assert 'id="studioModels"' in html
    assert 'id="studioFiles"' in html
    assert "/api/studio/render" in html
    assert "loadStudioModels" in html
