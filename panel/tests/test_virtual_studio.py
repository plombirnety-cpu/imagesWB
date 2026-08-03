# -*- coding: utf-8 -*-
import io
import queue
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

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


def test_load_artwork_removes_border_connected_white_background(tmp_path):
    artwork_path = tmp_path / "opaque-white.png"
    source = Image.new("RGB", (240, 240), "white")
    draw = ImageDraw.Draw(source)
    draw.ellipse((40, 40, 200, 200), fill=(220, 20, 20))
    draw.rectangle((105, 105, 135, 135), fill="white")
    source.save(artwork_path)

    artwork = virtual_studio._load_artwork(artwork_path)

    assert artwork.mode == "RGBA"
    assert artwork.size[0] < source.size[0]
    assert artwork.size[1] < source.size[1]
    assert artwork.getchannel("A").getextrema() == (0, 255)
    center = (artwork.width // 2, artwork.height // 2)
    assert artwork.getpixel(center) == (255, 255, 255, 255)


def test_render_mockup_uses_blank_shirt_then_fabric_transfer(monkeypatch, tmp_path):
    artwork_path = tmp_path / "print.png"
    Image.new("RGBA", (300, 500), (255, 30, 30, 220)).save(artwork_path)
    calls = []
    blank_shirt = Image.new("RGB", (512, 768), (210, 210, 210))
    finished_mockup = Image.new("RGB", (512, 768), (180, 180, 180))

    def fake_generate(prompt, references, model=None):
        calls.append({"prompt": prompt, "references": references, "model": model})
        return blank_shirt if len(calls) == 1 else finished_mockup

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
    assert len(calls) == 2
    assert len(calls[0]["references"]) == 1
    assert calls[0]["references"][0].mode == "RGB"
    assert "clean blank T-shirt" in calls[0]["prompt"]
    assert "same facial identity" in calls[0]["prompt"]
    assert "Print Factory Signature lighting" in calls[0]["prompt"]

    assert len(calls[1]["references"]) == 2
    assert calls[1]["references"][0] is blank_shirt
    assert calls[1]["references"][1].mode == "RGBA"
    assert "exact print artwork" in calls[1]["prompt"]
    assert "edit only the T-shirt surface" in calls[1]["prompt"]
    assert "existing fabric luminance as a displacement and shading map" in calls[1]["prompt"]
    assert "physically absorbed into the cotton" in calls[1]["prompt"]
    assert "front chest" in calls[1]["prompt"]


def test_front_transfer_prompt_limits_artwork_to_center_chest_zone():
    prompt = virtual_studio._transfer_prompt("white", "front")
    lower_prompt = prompt.lower()

    assert "normalized front print zone" in prompt
    assert "18% to 82%" in prompt
    assert "12% to 70%" in prompt
    assert "at least 30% of the visible shirt length blank below" in prompt
    assert "scale the artwork down uniformly" in lower_prompt
    assert "never enlarge it beyond this zone" in lower_prompt


def test_back_transfer_prompt_does_not_use_front_size_zone():
    prompt = virtual_studio._transfer_prompt("black", "back")

    assert "normalized front print zone" not in prompt


def test_back_mockup_prompt_uses_back_surface_curvature(monkeypatch, tmp_path):
    artwork_path = tmp_path / "back-print.png"
    Image.new("RGBA", (300, 500), (255, 255, 255, 255)).save(artwork_path)
    prompts = []

    def fake_generate(prompt, references, model=None):
        prompts.append(prompt)
        return Image.new("RGB", (512, 768), (230, 230, 230))

    monkeypatch.setattr(virtual_studio.providers, "generate_image_with_references", fake_generate)
    virtual_studio.render_mockup(
        model_id="alisa",
        artwork_path=artwork_path,
        shirt_color="black",
        placement="back",
        pose_index=0,
        output_path=tmp_path / "back-result.png",
    )

    assert len(prompts) == 2
    assert "center back" in prompts[0]
    assert "center back" in prompts[1]
    assert "shoulder blades and upper-back curvature" in prompts[1]
    assert "Existing folds must remain visible through the ink" in prompts[1]


def test_catalog_lighting_keeps_neutral_studio(monkeypatch, tmp_path):
    artwork_path = tmp_path / "catalog-print.png"
    Image.new("RGBA", (300, 500), (255, 255, 255, 255)).save(artwork_path)
    prompts = []

    def fake_generate(prompt, references, model=None):
        prompts.append(prompt)
        return Image.new("RGB", (512, 768), (230, 230, 230))

    monkeypatch.setattr(virtual_studio.providers, "generate_image_with_references", fake_generate)
    virtual_studio.render_mockup(
        model_id="alisa",
        artwork_path=artwork_path,
        shirt_color="white",
        placement="front",
        pose_index=0,
        output_path=tmp_path / "catalog-result.png",
        lighting="catalog",
    )

    assert len(prompts) == 2
    assert "Classic catalog lighting" in prompts[0]
    assert "teal rim" not in prompts[0]
    assert "base photograph" in prompts[1]


def test_unknown_lighting_is_rejected(tmp_path):
    artwork_path = tmp_path / "print.png"
    Image.new("RGBA", (300, 500), (255, 255, 255, 255)).save(artwork_path)

    try:
        virtual_studio.render_mockup(
            model_id="alisa",
            artwork_path=artwork_path,
            shirt_color="black",
            placement="front",
            pose_index=0,
            output_path=tmp_path / "bad-light.png",
            lighting="unknown",
        )
    except ValueError as exc:
        assert "режим освещения" in str(exc)
    else:
        raise AssertionError("неизвестный режим освещения должен быть отклонён")


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
        "signature",
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
    assert job["lighting"] == "signature"
    assert list((Path(job["outdir"]) / "uploads").glob("*.png"))
    shutil.rmtree(job["outdir"], ignore_errors=True)
    panel_app._studio_jobs.pop(job_id, None)


def test_studio_api_rejects_unknown_lighting():
    client = TestClient(panel_app.app)
    response = client.post(
        "/api/studio/render",
        data={
            "model_id": "artem",
            "shirt_color": "black",
            "placement": "front",
            "pose_count": "1",
            "quality": "standard",
            "lighting": "unknown",
        },
        files=[("prints", ("sample.png", _png_bytes(), "image/png"))],
    )
    assert response.status_code == 400
    assert "освещения" in response.json()["detail"]


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
