# -*- coding: utf-8 -*-
"""Тесты meme_ref.py — референс-картинка ОРИГИНАЛА интернет-мема (жалоба владельца
2026-07-11: сова на скакалке/кот со слюной/Backrooms генерятся НЕ похожими на
оригинал, модель рисует СВОЮ версию по одному текстовому описанию). Аналог
character_ref.py, но БЕЗ сети — референс всегда РУЧНОЙ файл data/meme_refs/<slug>.png.

Покрыто:
- meme_ref.py: загрузка существующего файла, graceful degradation при отсутствующем/
  битом файле/недопустимом slug (НИКОГДА не падает, НИКОГДА не трогает сеть).
- batch_print.render_design: приоритет meme_ref НАД character_ref (design может иметь
  оба поля), fallback на character_ref/чистый текст, если файла meme_ref нет —
  "генерация по описанию как сейчас" (ровно то поведение, что было бы без этой задачи).
- mega_batch_run._process_one: прокидывание rec["meme_ref"] -> design["meme_ref"].
- trends_plan.json: фактически расставленные поля meme_ref по категориям.

Полностью офлайн — Gemini/сеть НЕ используются нигде (generate_image замокан,
character_ref.get_reference замокан там, где нужен, meme_ref.py в принципе не делает
сетевых вызовов). green_only=True используется для render_design-тестов — то же
упрощение, что в tests/test_green_only_mode.py, изолирует именно логику выбора
референса от вырезки/апскейла/типографики.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_meme_ref.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image, ImageDraw  # noqa: E402

import batch_print      # noqa: E402
import meme_ref          # noqa: E402
import mega_batch_run    # noqa: E402

REF_GREEN = (0, 177, 64)


# ═══════════════════════════════════════════════════════════════════════════════
# meme_ref.py — загрузка локального файла, graceful degradation
# ═══════════════════════════════════════════════════════════════════════════════

def test_meme_ref_module_never_imports_network_library():
    """Защита от регресса «не изобретай сеть»: meme_ref.py — ЧИСТО файловая
    операция, никакого requests/urllib для получения референса (в отличие от
    character_ref.py, который ходит в Jikan/AniList)."""
    assert not hasattr(meme_ref, "requests")


def test_get_reference_loads_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(meme_ref, "MEME_REF_DIR", tmp_path)
    Image.new("RGB", (12, 8), (50, 60, 70)).save(tmp_path / "owl_skakalka.png")

    out = meme_ref.get_reference("owl_skakalka")
    assert out is not None
    assert out.mode == "RGB"
    assert out.size == (12, 8)


def test_get_reference_missing_file_returns_none_with_warning(tmp_path, monkeypatch, capsys):
    """Файла нет (владелец ещё не положил референс) — None, НЕ падает, явное
    предупреждение в лог (задача: 'meme_ref <slug> задан, но файла нет — генерация
    по описанию')."""
    monkeypatch.setattr(meme_ref, "MEME_REF_DIR", tmp_path)

    out = meme_ref.get_reference("owl_skakalka")

    assert out is None
    captured = capsys.readouterr().out
    assert "owl_skakalka" in captured
    assert "нет" in captured


def test_get_reference_empty_slug_returns_none_without_touching_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(meme_ref, "MEME_REF_DIR", tmp_path)
    assert meme_ref.get_reference("") is None
    assert meme_ref.get_reference("   ") is None


def test_get_reference_rejects_path_traversal_slug(tmp_path, monkeypatch, capsys):
    """slug с символами вне [A-Za-z0-9_-] (напр. path traversal) -> None с
    предупреждением, файл ВНЕ MEME_REF_DIR не трогается."""
    monkeypatch.setattr(meme_ref, "MEME_REF_DIR", tmp_path / "meme_refs")
    (tmp_path / "meme_refs").mkdir()
    outside = tmp_path / "secret.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(outside)

    out = meme_ref.get_reference("../secret")

    assert out is None
    assert "недопустимый" in capsys.readouterr().out
    assert outside.exists()  # файл вне референс-папки не тронут


def test_get_reference_corrupted_file_returns_none_gracefully(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(meme_ref, "MEME_REF_DIR", tmp_path)
    (tmp_path / "broken.png").write_bytes(b"not a real png file")

    out = meme_ref.get_reference("broken")

    assert out is None
    assert "повреждён" in capsys.readouterr().out


def test_ref_path_builds_expected_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(meme_ref, "MEME_REF_DIR", tmp_path)
    assert meme_ref._ref_path("owl_skakalka") == tmp_path / "owl_skakalka.png"
    assert meme_ref._ref_path("") is None
    assert meme_ref._ref_path("bad slug!") is None


# ═══════════════════════════════════════════════════════════════════════════════
# batch_print.render_design — приоритет meme_ref, fallback, обратная совместимость
# ═══════════════════════════════════════════════════════════════════════════════

def _design(**overrides) -> dict:
    base = {
        "prompt": "The original viral owl-on-a-skipping-rope meme, cute cartoon style.",
        "chroma": "green",
        "slogan": "",
        "slogan_color": "orange",
        "kana": "",
        "character_en": "",
        "title_en": "",
        "signature_props": "",
        "text_mode": "none",
        "text_modes_v3": [],
        "quote": "",
        "name_jp": "",
        "mood": "",
        "type_spec": "",  # пусто -> OCR-контроль не участвует (офлайн-тест)
    }
    base.update(overrides)
    return base


def _gen_img(bg, w=220, h=300) -> Image.Image:
    """Кадр с правильным хромакеем bg и высокой фигурой (bbox >= 0.55 высоты —
    проходит QC-гейт масштаба) — тот же helper, что tests/test_green_only_mode.py."""
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([w * 0.3, h * 0.08, w * 0.7, h * 0.94], fill=(180, 60, 50))
    return img


def _forbid(name):
    def _boom(*a, **k):
        raise AssertionError(f"{name} НЕ должен вызываться в этом сценарии")
    return _boom


def test_render_design_meme_ref_found_takes_priority_over_character_ref(tmp_path, monkeypatch):
    """meme_ref задан И файл есть, character_en ТОЖЕ задан — meme_ref побеждает:
    character_ref.get_reference НЕ вызывается вовсе, в generate_image уходит
    картинка meme_ref + промпт получает _MEME_REFERENCE_PREFIX."""
    ref_dir = tmp_path / "meme_refs"
    ref_dir.mkdir()
    monkeypatch.setattr(meme_ref, "MEME_REF_DIR", ref_dir)
    meme_img = Image.new("RGB", (40, 40), (140, 90, 200))
    meme_img.save(ref_dir / "owl_skakalka.png")

    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        _forbid("character_ref.get_reference"))

    captured = {}

    def fake_generate(prompt, seed=None, model=None, reference=None):
        captured["prompt"] = prompt
        captured["reference"] = reference
        return _gen_img(REF_GREEN)

    monkeypatch.setattr(batch_print.providers, "generate_image", fake_generate)

    design = _design(character_en="Some Anime Character", title_en="Some Title",
                     meme_ref="owl_skakalka")
    outdir = tmp_path / "out"
    outdir.mkdir()

    res = batch_print.render_design(design, "m01", outdir, green_only=True,
                                    design_json_path=tmp_path / "_meta" / "m01_design.json")

    assert res["ok"] is True
    assert captured["reference"] is not None
    assert captured["reference"].size == (40, 40)
    assert "Reproduce the meme subject EXACTLY" in captured["prompt"]
    assert "PRIORITY over the text description" in captured["prompt"]


def test_render_design_meme_ref_missing_file_falls_back_to_character_ref(
        tmp_path, monkeypatch, capsys):
    """meme_ref задан, но файла НЕТ, character_en задан — graceful: НЕ падает,
    предупреждение печатается, генерация продолжается ОБЫЧНЫМ character_ref-путём
    (как если бы meme_ref вовсе не было указано)."""
    monkeypatch.setattr(meme_ref, "MEME_REF_DIR", tmp_path / "meme_refs")  # пустая папка

    char_img = Image.new("RGB", (20, 20), (10, 10, 10))
    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        lambda *a, **k: char_img)

    captured = {}

    def fake_generate(prompt, seed=None, model=None, reference=None):
        captured["prompt"] = prompt
        captured["reference"] = reference
        return _gen_img(REF_GREEN)

    monkeypatch.setattr(batch_print.providers, "generate_image", fake_generate)

    design = _design(character_en="Some Anime Character", meme_ref="owl_skakalka")
    outdir = tmp_path / "out"
    outdir.mkdir()

    res = batch_print.render_design(design, "m02", outdir, green_only=True,
                                    design_json_path=tmp_path / "_meta" / "m02_design.json")

    assert res["ok"] is True
    assert captured["reference"] is char_img
    assert "Use the reference image as the EXACT character identity" in captured["prompt"]
    out = capsys.readouterr().out
    assert "owl_skakalka" in out and "нет" in out


def test_render_design_meme_ref_missing_file_no_character_generates_by_text(
        tmp_path, monkeypatch):
    """meme_ref задан, файла НЕТ, character_en ПУСТ (обычный случай для мемов) —
    генерация идёт по чистому тексту, character_ref НЕ вызывается вовсе (нечего
    искать по пустому имени)."""
    monkeypatch.setattr(meme_ref, "MEME_REF_DIR", tmp_path / "meme_refs")
    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        _forbid("character_ref.get_reference"))

    captured = {}

    def fake_generate(prompt, seed=None, model=None, reference=None):
        captured["prompt"] = prompt
        captured["reference"] = reference
        return _gen_img(REF_GREEN)

    monkeypatch.setattr(batch_print.providers, "generate_image", fake_generate)

    design = _design(character_en="", meme_ref="owl_skakalka")
    outdir = tmp_path / "out"
    outdir.mkdir()

    res = batch_print.render_design(design, "m03", outdir, green_only=True,
                                    design_json_path=tmp_path / "_meta" / "m03_design.json")

    assert res["ok"] is True
    assert captured["reference"] is None
    assert "Reproduce the meme subject EXACTLY" not in captured["prompt"]


def test_render_design_without_meme_ref_field_unchanged_behaviour(tmp_path, monkeypatch):
    """Регресс-предохранитель: design БЕЗ ключа "meme_ref" вовсе (весь остальной
    существующий парк тестов/дампов) — поведение 1:1 как до этой задачи, только
    character_ref участвует."""
    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        lambda *a, **k: None)  # "не найден", как раньше

    captured = {}

    def fake_generate(prompt, seed=None, model=None, reference=None):
        captured["prompt"] = prompt
        captured["reference"] = reference
        return _gen_img(REF_GREEN)

    monkeypatch.setattr(batch_print.providers, "generate_image", fake_generate)

    design = _design(character_en="Zoro")
    assert "meme_ref" not in design
    outdir = tmp_path / "out"
    outdir.mkdir()

    res = batch_print.render_design(design, "m04", outdir, green_only=True,
                                    design_json_path=tmp_path / "_meta" / "m04_design.json")

    assert res["ok"] is True
    assert captured["reference"] is None
    assert "Reproduce the meme subject EXACTLY" not in captured["prompt"]


# ═══════════════════════════════════════════════════════════════════════════════
# mega_batch_run._process_one — прокидывание meme_ref из записи плана в design
# ═══════════════════════════════════════════════════════════════════════════════

def _fake_render_design_ok(captured: dict):
    def _fake(design, *a, **k):
        captured["design"] = design
        return {"ok": True, "attempts": 1, "images": 1, "error": None,
                "print_fallback": False, "text_fallback": False, "raw": None}
    return _fake


def test_process_one_injects_meme_ref_from_plan_record_into_design(tmp_path, monkeypatch):
    monkeypatch.setattr(mega_batch_run.art_director, "make_ideas",
                        lambda *a, **k: [{"prompt": "x", "chroma": "green"}])
    captured = {}
    monkeypatch.setattr(mega_batch_run.batch_print, "render_design",
                        _fake_render_design_ok(captured))

    rec = {"seq": 1, "filename_base": "owl_original_jump_01", "category": "trends/owl",
           "theme": "сова на скакалке", "format": "diecut", "style_pref": None,
           "meme_ref": "owl_skakalka"}
    journal = mega_batch_run._process_one(rec, tmp_path, None)

    assert journal["status"] == "done"
    assert captured["design"]["meme_ref"] == "owl_skakalka"


def test_process_one_without_meme_ref_in_record_does_not_set_design_key(tmp_path, monkeypatch):
    """Запись плана БЕЗ поля meme_ref (напр. memcho_*, или весь mega_plan_800.json
    сегодня) — design["meme_ref"] не выставляется вовсе, обратная совместимость."""
    monkeypatch.setattr(mega_batch_run.art_director, "make_ideas",
                        lambda *a, **k: [{"prompt": "x", "chroma": "green"}])
    captured = {}
    monkeypatch.setattr(mega_batch_run.batch_print, "render_design",
                        _fake_render_design_ok(captured))

    rec = {"seq": 2, "filename_base": "memcho_original_portrait_01",
           "category": "trends/memcho", "theme": "Мем Чо", "format": "diecut",
           "style_pref": None}
    journal = mega_batch_run._process_one(rec, tmp_path, None)

    assert journal["status"] == "done"
    assert "meme_ref" not in captured["design"]


# ═══════════════════════════════════════════════════════════════════════════════
# trends_plan.json — фактически расставленные поля meme_ref
# ═══════════════════════════════════════════════════════════════════════════════

def test_trends_plan_meme_ref_assignments_by_category():
    plan = json.loads((PROJECT_ROOT / "trends_plan.json").read_text(encoding="utf-8"))
    by_base = {r["filename_base"]: r for r in plan}
    assert len(by_base) == len(plan), "filename_base должны оставаться уникальными"
    assert len(plan) == 24

    for base, rec in by_base.items():
        if base.startswith("owl_"):
            assert rec.get("meme_ref") == "owl_skakalka", base
        elif base.startswith("drooling_cat_"):
            assert rec.get("meme_ref") == "drooling_cat", base
        elif base.startswith("backrooms_"):
            assert rec.get("meme_ref") == "backrooms", base
        elif base.startswith("kolyaka_"):
            # разные животные -> РАЗНЫЕ картинки колоды, не один общий slug
            assert rec.get("meme_ref"), f"{base} должен иметь непустой meme_ref"
            assert rec["meme_ref"] not in ("owl_skakalka", "drooling_cat", "backrooms")
        elif base.startswith("memcho_"):
            assert "meme_ref" not in rec, (
                f"{base} — аниме-персонаж, идёт через character_ref, meme_ref не нужен")
        else:
            raise AssertionError(f"неизвестная категория темы: {base}")

    kolyaka_refs = {r["meme_ref"] for base, r in by_base.items()
                    if base.startswith("kolyaka_")}
    assert len(kolyaka_refs) == 5, "5 разных карт Коляки должны иметь 5 разных meme_ref"


def test_trends_plan_meme_ref_values_are_valid_slugs():
    """Каждый непустой meme_ref реально резолвится в путь meme_ref._ref_path (не
    падает на санацию slug) — застраховаться от опечатки/недопустимого символа в
    самом trends_plan.json."""
    plan = json.loads((PROJECT_ROOT / "trends_plan.json").read_text(encoding="utf-8"))
    for rec in plan:
        slug = rec.get("meme_ref")
        if slug:
            assert meme_ref._ref_path(slug) is not None, (
                f"{rec['filename_base']}: невалидный slug meme_ref={slug!r}")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
