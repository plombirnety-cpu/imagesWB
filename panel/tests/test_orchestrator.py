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


def test_plan_tasks_franchise_preserves_canonical_reference_identity(monkeypatch):
    """Имя и канонический romaji-тайтл из досье должны дойти до рендера.
    Арт-директор не обязан знать свежую франшизу и может иначе подменить героя
    буквальной трактовкой имени (Enjin -> engine)."""
    def fake_build_dossier(title, kind="auto"):
        assert title == "Клинок рассекающий демонов"
        return {
            "title": "Клинок рассекающий демонов",
            "title_ref": "Kimetsu no Yaiba",
            "characters": [
                {"name_ru": "Тандзиро Камадо", "name_en": "Tanjirou Kamado", "score": 100},
            ],
        }

    monkeypatch.setattr(orchestrator.franchise_scout, "build_dossier", fake_build_dossier)
    task = orchestrator.plan_tasks(
        styles=["34_anime_magazine_cover"], count=1,
        theme="Клинок рассекающий демонов", characters="",
    )[0]

    assert task.label == "Тандзиро Камадо"
    assert task.char_en == "Tanjirou Kamado"
    assert task.title_hint == "Kimetsu no Yaiba"


def test_render_task_overrides_art_director_reference_guess(tmp_path, monkeypatch):
    """Для franchise-задачи надёжное досье сильнее догадки арт-директора."""
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: [{
        "prompt": "generic mechanic",
        "chroma": "green",
        "character_en": "",
        "title_en": "Original Concept",
    }])
    captured = {}

    def fake_render_design(design, tag, outdir, **kw):
        captured.update(design)
        path = outdir / f"{tag}.png"
        Image.new("RGB", (4, 4), (0, 200, 0)).save(path)
        return {"ok": True, "green": str(path), "error": None}

    monkeypatch.setattr(orchestrator.batch_print, "render_design", fake_render_design)
    task = orchestrator.DesignTask(
        index=1, label="Энджин", style_id="34_anime_magazine_cover",
        tag="01_enjin", source="franchise", char_en="Enjin", title_hint="Gachiakuta",
    )

    result = orchestrator.render_task(task, tmp_path)

    assert result["ok"] is True
    assert captured["character_en"] == "Enjin"
    assert captured["title_en"] == "Gachiakuta"


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


def test_plan_tasks_without_selected_style_uses_auto_not_anime_style(monkeypatch):
    """Живой job cde0a16ef360: Doctor Doom без выбранного стиля ошибочно получил 34."""
    monkeypatch.setattr(
        orchestrator.franchise_scout,
        "build_dossier",
        lambda *a, **k: {"characters": []},
    )

    task = orchestrator.plan_tasks(
        styles=[], count=1, theme="доктор дум", characters="",
    )[0]

    assert task.style_id == "auto"
    assert "anime_magazine" not in task.tag


def test_plan_tasks_free_prompt_bypasses_dossier_and_selected_styles(monkeypatch):
    def dossier_must_not_run(*args, **kwargs):
        raise AssertionError("свободный режим не должен искать франшизу")

    monkeypatch.setattr(orchestrator.franchise_scout, "build_dossier", dossier_must_not_run)
    brief = "Самурайский кот на луне, неоновый дым и надпись NIGHT PAWS"
    tasks = orchestrator.plan_tasks(
        styles=["34_anime_magazine_cover"],
        count=2,
        theme="эта тема должна игнорироваться",
        characters="и эти персонажи тоже",
        free_prompt=brief,
    )

    assert len(tasks) == 2
    assert [task.source for task in tasks] == ["free", "free"]
    assert [task.label for task in tasks] == [brief, brief]
    assert [task.style_id for task in tasks] == ["auto", "auto"]
    assert all(task.char_en == "" and task.title_hint == "" for task in tasks)


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


def test_render_once_auto_does_not_force_style_34(tmp_path, monkeypatch):
    captured = {}

    def fake_make_ideas(*args, **kwargs):
        captured["style_pref"] = kwargs.get("style_pref")
        return _fake_design()

    monkeypatch.setattr(orchestrator.art_director, "make_ideas", fake_make_ideas)
    monkeypatch.setattr(
        orchestrator.batch_print,
        "render_design",
        lambda design, tag, outdir, **kw: {
            "ok": True,
            "green": str(outdir / f"{tag}.png"),
            "error": None,
        },
    )
    task = orchestrator.DesignTask(
        index=1,
        label="доктор дум",
        style_id="auto",
        tag="01_doktor-dum_auto",
        source="theme",
    )

    result = orchestrator._render_once(task, tmp_path)

    assert result["ok"] is True
    assert captured["style_pref"] is None


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


def test_render_task_retries_then_succeeds(tmp_path, monkeypatch):
    # Авто-ретрай (_RENDER_ATTEMPTS): первая попытка render_design падает
    # (off-style/HARD-reject кадра без хромакея — интермиттентный глюк), вторая —
    # успех. render_task должен вернуть ok=True, не оставляя слот пустым.
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: _fake_design())
    calls = {"n": 0}

    def flaky(design, tag, outdir, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "error": "кадр без хромакей-фона (coverage 0.00)"}
        p = outdir / f"{tag}.png"
        Image.new("RGB", (4, 4), (0, 200, 0)).save(p)
        return {"ok": True, "green": str(p), "error": None}
    monkeypatch.setattr(orchestrator.batch_print, "render_design", flaky)

    task = orchestrator.DesignTask(index=1, label="тачки", style_id="01_baroque_frame",
                                    tag="01_tachki", source="theme")
    result = orchestrator.render_task(task, tmp_path)
    assert result["ok"] is True
    assert result["path"] and Path(result["path"]).exists()
    assert calls["n"] == 2  # ровно 1 провал + 1 успех


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


def test_greenkey_failure_does_not_repeat_paid_generation(tmp_path, monkeypatch):
    """Постобработка идёт после генерационных ретраев и не тратит второй запрос."""
    monkeypatch.setattr(orchestrator.art_director, "make_ideas", lambda *a, **kw: _fake_design())
    calls = {"render": 0}

    def fake_render_design(design, tag, outdir, **kw):
        calls["render"] += 1
        path = outdir / f"{tag}.png"
        Image.new("RGB", (20, 20), (0, 200, 0)).save(path)
        return {"ok": True, "green": str(path), "error": None}

    def fail_greenkey(*args, **kwargs):
        raise RuntimeError("postprocess unavailable")

    monkeypatch.setattr(orchestrator.batch_print, "render_design", fake_render_design)
    monkeypatch.setattr(orchestrator.greenkey_postprocess, "process_file", fail_greenkey)

    task = orchestrator.DesignTask(
        index=1,
        label="тачки",
        style_id="01_baroque_frame",
        tag="01_tachki",
        source="theme",
    )
    result = orchestrator.render_task(task, tmp_path)

    assert result["ok"] is False
    assert result["path"] is None
    assert result["error"] == "GreenKey: postprocess unavailable"
    assert calls["render"] == 1
