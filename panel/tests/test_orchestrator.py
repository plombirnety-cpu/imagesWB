# -*- coding: utf-8 -*-
"""test_orchestrator.py — мок-тесты логики оркестрации панели, БЕЗ единого
платного вызова движка (art_director.make_ideas/batch_print.render_design/
franchise_scout.build_dossier — все монкипатчатся). Покрывает все 3 ветки
логики (см. panel/PLAN.md "Логика оркестрации") + сам render_task."""
from pathlib import Path

from PIL import Image

import orchestrator


# ── plan_tasks: ветка 1 — персонажи заданы явно ─────────────────────────────

def test_plan_tasks_characters_branch():
    tasks = orchestrator.plan_tasks(
        styles=["01_baroque_frame", "09_ring_medallion"],
        count=5, theme="", characters="Наруто, Саске",
    )
    assert len(tasks) == 5
    assert [t.source for t in tasks] == ["characters"] * 5
    # персонажи по кругу
    assert [t.label for t in tasks] == ["Наруто", "Саске", "Наруто", "Саске", "Наруто"]
    # стили по кругу, независимо от персонажей
    assert [t.style_id for t in tasks] == [
        "01_baroque_frame", "09_ring_medallion", "01_baroque_frame",
        "09_ring_medallion", "01_baroque_frame",
    ]
    # теги уникальны и пронумерованы
    assert len({t.tag for t in tasks}) == 5
    assert tasks[0].tag.startswith("01_")
    assert tasks[4].tag.startswith("05_")


def test_plan_tasks_characters_newline_and_semicolon_separated():
    tasks = orchestrator.plan_tasks(
        styles=["01_baroque_frame"], count=3, theme="",
        characters="Zoro\nLuffy;Nami",
    )
    assert [t.label for t in tasks] == ["Zoro", "Luffy", "Nami"]


# ── plan_tasks: ветка 2 — тема это тайтл (франшиза) ─────────────────────────

def test_plan_tasks_franchise_branch(monkeypatch):
    def fake_build_dossier(title, kind="auto"):
        assert title == "Dragon Ball"
        return {
            "title": "Dragon Ball",
            "characters": [
                {"name_ru": "Гоку", "score": 95.0},
                {"name_ru": "Веджита", "score": 88.0},
            ],
        }
    monkeypatch.setattr(orchestrator.franchise_scout, "build_dossier", fake_build_dossier)

    tasks = orchestrator.plan_tasks(
        styles=["01_baroque_frame"], count=4, theme="Dragon Ball", characters="",
    )
    assert len(tasks) == 4
    assert [t.source for t in tasks] == ["franchise"] * 4
    assert [t.label for t in tasks] == ["Гоку", "Веджита", "Гоку", "Веджита"]


def test_plan_tasks_franchise_falls_back_to_name_en(monkeypatch):
    def fake_build_dossier(title, kind="auto"):
        return {"characters": [{"name_en": "Levi Ackerman", "score": 70.0}]}
    monkeypatch.setattr(orchestrator.franchise_scout, "build_dossier", fake_build_dossier)

    tasks = orchestrator.plan_tasks(styles=[], count=2, theme="Attack on Titan", characters="")
    assert [t.label for t in tasks] == ["Levi Ackerman", "Levi Ackerman"]
    # styles=[] -> дефолтный стиль из settings
    assert all(t.style_id == orchestrator.settings.DEFAULT_STYLE for t in tasks)


# ── plan_tasks: ветка 3 — просто тема (не тайтл / без сигналов) ────────────

def test_plan_tasks_theme_branch_empty_dossier(monkeypatch):
    def fake_build_dossier(title, kind="auto"):
        return {"characters": []}  # тема реальная, но не тайтл/сигналов нет
    monkeypatch.setattr(orchestrator.franchise_scout, "build_dossier", fake_build_dossier)

    tasks = orchestrator.plan_tasks(styles=["11_propaganda_A"], count=3, theme="тачки", characters="")
    assert len(tasks) == 3
    assert [t.source for t in tasks] == ["theme"] * 3
    assert [t.label for t in tasks] == ["тачки"] * 3


def test_plan_tasks_theme_branch_dossier_network_failure(monkeypatch):
    def boom(title, kind="auto"):
        raise RuntimeError("сеть недоступна")
    monkeypatch.setattr(orchestrator.franchise_scout, "build_dossier", boom)

    # сбой build_dossier НЕ должен ронять панель — тихий откат на тему
    tasks = orchestrator.plan_tasks(styles=[], count=2, theme="тачки", characters="")
    assert [t.source for t in tasks] == ["theme", "theme"]
    assert [t.label for t in tasks] == ["тачки", "тачки"]


def test_plan_tasks_requires_theme_or_characters(monkeypatch):
    def fake_build_dossier(title, kind="auto"):
        return {"characters": []}
    monkeypatch.setattr(orchestrator.franchise_scout, "build_dossier", fake_build_dossier)
    try:
        orchestrator.plan_tasks(styles=[], count=3, theme="", characters="")
        assert False, "ожидалась ValueError"
    except ValueError:
        pass


# ── sanitize_slug ────────────────────────────────────────────────────────────

def test_sanitize_slug_transliterates_cyrillic():
    assert orchestrator.sanitize_slug("Поднятие уровня") == "podnyatie-urovnya"


def test_sanitize_slug_fallback_on_empty():
    assert orchestrator.sanitize_slug("", fallback="item") == "item"
    assert orchestrator.sanitize_slug("!!!", fallback="item") == "item"


# ── render_task ──────────────────────────────────────────────────────────────

def _fake_design():
    return [{"prompt": "test prompt", "chroma": "green", "style_id": "01_baroque_frame"}]


def test_render_task_success(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: _fake_design())

    def fake_render_design(design, tag, outdir, **kw):
        assert kw.get("green_only") is True
        p = outdir / f"{tag}.png"
        Image.new("RGB", (4, 4), (0, 200, 0)).save(p)
        return {"ok": True, "green": str(p), "error": None}
    monkeypatch.setattr(orchestrator.batch_print, "render_design", fake_render_design)

    task = orchestrator.DesignTask(index=1, label="тачки", style_id="01_baroque_frame",
                                    tag="01_tachki_01_baroque_frame", source="theme")
    result = orchestrator.render_task(task, tmp_path)
    assert result["ok"] is True
    assert result["error"] is None
    assert result["path"] and Path(result["path"]).exists()


def test_render_task_art_director_failure(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("невалидный JSON от Claude")
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", boom)

    task = orchestrator.DesignTask(index=1, label="тачки", style_id="01_baroque_frame",
                                    tag="01_tachki", source="theme")
    result = orchestrator.render_task(task, tmp_path)
    assert result["ok"] is False
    assert result["path"] is None
    assert "арт-директор" in result["error"]


def test_render_task_render_design_not_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: _fake_design())
    monkeypatch.setattr(orchestrator.batch_print, "render_design",
                         lambda *a, **kw: {"ok": False, "error": "border coverage < 0.90"})

    task = orchestrator.DesignTask(index=1, label="тачки", style_id="01_baroque_frame",
                                    tag="01_tachki", source="theme")
    result = orchestrator.render_task(task, tmp_path)
    assert result["ok"] is False
    assert result["error"] == "border coverage < 0.90"


def test_render_task_missing_green_path(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: _fake_design())
    monkeypatch.setattr(orchestrator.batch_print, "render_design",
                         lambda *a, **kw: {"ok": True, "green": None})

    task = orchestrator.DesignTask(index=1, label="тачки", style_id="01_baroque_frame",
                                    tag="01_tachki", source="theme")
    result = orchestrator.render_task(task, tmp_path)
    assert result["ok"] is False
    assert "green_only" in result["error"]


def test_render_task_engine_exception_does_not_propagate(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: _fake_design())

    def boom(*a, **kw):
        raise ConnectionError("Gemini недоступен")
    monkeypatch.setattr(orchestrator.batch_print, "render_design", boom)

    task = orchestrator.DesignTask(index=1, label="тачки", style_id="01_baroque_frame",
                                    tag="01_tachki", source="theme")
    result = orchestrator.render_task(task, tmp_path)  # не должно бросить исключение
    assert result["ok"] is False
    assert "render_design" in result["error"]
