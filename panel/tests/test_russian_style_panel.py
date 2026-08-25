from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from PIL import Image

import app as panel_app
import orchestrator


STYLE_ID = "42_russian_style"


def test_explicit_russian_style_request_bypasses_franchise_scout(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator.franchise_scout,
        "build_dossier",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Русский стиль не должен менять запрос на досье франшизы")
        ),
    )

    tasks = orchestrator.plan_tasks(
        styles=[STYLE_ID],
        count=3,
        theme="программист и северное сияние",
        characters="рыжий кот; старый ноутбук",
    )

    assert len(tasks) == 3
    assert all(task.source == "russian_style" for task in tasks)
    assert all(task.style_id == STYLE_ID for task in tasks)
    assert all("программист и северное сияние" in task.label for task in tasks)
    assert all("рыжий кот" in task.label for task in tasks)
    assert len({task.style_brief for task in tasks}) == 3
    assert all("SEMANTIC BRIDGES" in task.style_brief for task in tasks)


def test_empty_russian_style_interleaves_approved_radar_and_evergreen() -> None:
    tasks = orchestrator.plan_tasks(
        styles=[STYLE_ID],
        count=4,
        theme="",
        characters="",
        autonomous_topics=["Свежая тема России", "Второй подтверждённый тренд"],
    )

    assert [task.source for task in tasks] == ["russian_style_auto"] * 4
    assert tasks[0].label == "Свежая тема России"
    assert tasks[2].label == "Второй подтверждённый тренд"
    assert "Жар-птица" in tasks[1].label
    assert "Лиса" in tasks[3].label
    assert len({task.label for task in tasks}) == 4


def test_empty_russian_style_uses_evergreen_without_radar_or_network(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator.franchise_scout,
        "build_dossier",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("evergreen-путь полностью локальный")
        ),
    )

    tasks = orchestrator.plan_tasks(
        styles=[STYLE_ID], count=3, theme="", characters="", autonomous_topics=[]
    )

    assert len(tasks) == 3
    assert all(task.source == "russian_style_auto" for task in tasks)
    assert len({task.label for task in tasks}) == 3


def test_russian_style_render_keeps_only_cyrillic_visible_text(tmp_path, monkeypatch) -> None:
    captured = {}

    monkeypatch.setattr(
        orchestrator.art_director,
        "make_ideas",
        lambda *args, **kwargs: [
            {
                "prompt": "кофейная чашка, пар превращается в орнамент",
                "chroma": "green",
                "style_id": "wrong",
                "style_mix": "01_baroque_frame",
                "quote": "FROM RUSSIA WITH LOVE",
                "slogan": "ТЕПЛО ДОМА",
                "type_spec": "one readable title",
                "kana": "ロシア",
                "name_jp": "露西亜",
                "text_modes_v3": ["headline"],
                "has_human_figure": False,
            }
        ],
    )

    def fake_render_design(design, tag, outdir, **kwargs):
        captured.update(design)
        path = outdir / f"{tag}.png"
        Image.new("RGB", (4, 4), (0, 200, 0)).save(path)
        return {"ok": True, "green": str(path), "error": None}

    monkeypatch.setattr(orchestrator.batch_print, "render_design", fake_render_design)
    task = orchestrator.DesignTask(
        index=1,
        label="кофе",
        style_id=STYLE_ID,
        tag="01_coffee",
        source="russian_style",
    )

    result = orchestrator._render_once(task, tmp_path)

    assert result["ok"] is True
    assert captured["style_id"] == STYLE_ID
    assert captured["style_mix"] == ""
    assert captured["quote"] == "ТЕПЛО ДОМА"
    assert captured["slogan"] == ""
    assert captured["kana"] == ""
    assert captured["name_jp"] == ""
    assert captured["text_modes_v3"] == []
    assert captured["has_human_figure"] is False


def test_russian_visible_text_rejects_latin_and_symbol_noise() -> None:
    assert orchestrator._safe_russian_visible_text("НЕ В ДЕНЬГАХ СЧАСТЬЕ") == (
        "НЕ В ДЕНЬГАХ СЧАСТЬЕ"
    )
    assert orchestrator._safe_russian_visible_text("FROM RUSSIA") == ""
    assert orchestrator._safe_russian_visible_text("2026 !!!") == ""


def test_radar_topics_require_owner_approval_quality_freshness_and_safety(monkeypatch) -> None:
    now = datetime.now(timezone.utc)

    def trend(name, *, approved=True, qualified=True, confidence=85, lifecycle="RISING", age_hours=1):
        return {
            "display_name": name,
            "approved": approved,
            "rejected": False,
            "lifecycle": lifecycle,
            "last_seen_at": (now - timedelta(hours=age_hours)).isoformat(),
            "opportunity": {"qualified": qualified, "confidence": confidence},
        }

    class FakeRadar:
        def list_opportunities(self, limit):
            return [
                trend("Кот учёный"),
                trend("Кот учёный"),
                trend("Президент на даче"),
                trend("Неподтверждённая тема", approved=False),
                trend("Слабая тема", confidence=40),
                trend("Старый тренд", age_hours=90),
                trend("Зрелая тема", lifecycle="MATURE"),
            ]

    monkeypatch.setattr(panel_app, "_radar_store", FakeRadar())

    assert panel_app._approved_russian_style_topics(10) == ["Кот учёный"]


def test_api_accepts_empty_russian_style_and_freezes_radar_plan(monkeypatch) -> None:
    submitted = []
    monkeypatch.setattr(
        panel_app,
        "_approved_russian_style_topics",
        lambda count: ["Подтверждённый российский тренд"],
    )
    monkeypatch.setattr(
        panel_app._executor,
        "submit",
        lambda *args, **kwargs: submitted.append((args, kwargs)),
    )
    client = TestClient(panel_app.app)

    response = client.post(
        "/api/generate",
        json={
            "styles": [STYLE_ID],
            "count": 2,
            "theme": "",
            "characters": "",
        },
    )

    assert response.status_code == 202
    assert submitted
    assert submitted[0][0][-1] == ["Подтверждённый российский тренд"]
    job_id = response.json()["job_id"]
    with panel_app._jobs_lock:
        panel_app._jobs.pop(job_id, None)


def test_russian_style_ui_copy_and_premium_confirmation_are_present() -> None:
    html = panel_app.STATIC_DIR.joinpath("index.html").read_text(encoding="utf-8")

    assert "const RUSSIAN_STYLE_ID = '42_russian_style'" in html
    assert "подтверждённую тему российского радара" in html
    assert "Дополнительные образы и детали" in html
    assert "более дорогую премиальную модель Gemini" in html
