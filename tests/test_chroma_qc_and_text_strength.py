# -*- coding: utf-8 -*-
"""Тесты одиннадцатого захода (2026-07-08) — калибровка после переезда на Nano Banana 2.

Живой кейс (out_batch/20260708_205007): дизайн 01 (Кенпачи) — модель нарисовала БЕЛЫЙ
фон, а зелёный хромакей пустила В ДИЗАЙН как ауру вокруг фигуры; старый QC-гейт
(проверял только однотонность рамки, не её ЦВЕТ) пропустил, вырезка взяла белый ключ с
рамки и убила белое хаори + зелёную ауру. Дизайн 03 (Supra) — слипшиеся слова в тексте
("BORN TO" читалось как "BORNTO"). Дизайн 02 (Тандзиро) — фраза нарисована ДВАЖДЫ.

Двенадцатый заход (2026-07-08, регресс по отчёту тестировщика): та же битва тем же
дизайном 02 (Тандзиро, out_batch/20260708_211920) — усиление промпта одиннадцатого
захода НЕ предотвратило дубль фразы на живой генерации, а старый `_verify_text`
проверял ТОЛЬКО substring-вхождение (не ловил дублирование) — дизайн формально прошёл
OCR-контроль с дублированной фразой на финальном diecut. Также подтверждён попиксельно
белый sticker-style ободок вокруг силуэта водно-огненной ауры (не рамка кадра — QC-гейт
цвета рамки это корректно не ловит, вне его области действия). Фиксы: (1)
`batch_print._verify_text` теперь считает НЕПЕРЕСЕКАЮЩИЕСЯ вхождения каждой ожидаемой
фразы — больше одного вхождения = провал OCR-контроля (тот же ретрай/фолбэк механизм);
(2) `art_director._text_render_block`/`_TYPE_SPEC_SCHEMA` — третье усиление промпта,
явно называющее паттерн "маленькое эхо у фигуры + отдельная крупная копия внизу";
(3) `art_director._chroma_bg` — явный запрет белой sticker-style обводки ВОКРУГ силуэта
персонажа/эффектов (не только рамки кадра).

Полностью офлайн — только PIL/numpy синтетика + чтение build_prompt как строки, без
сети.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_chroma_qc_and_text_strength.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import art_director  # noqa: E402
import batch_print  # noqa: E402
import providers  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# batch_print._border_chroma_coverage — QC-гейт ЦВЕТА рамки (не только однотонности)
# ═══════════════════════════════════════════════════════════════════════════════

def _solid_frame_image(rgb: tuple, size: tuple = (400, 600)) -> Image.Image:
    """Синтетика: рамка (и весь кадр целиком, для простоты) залита одним RGB —
    _border_chroma_coverage смотрит только на цвет рамки + однотонность вокруг неё, для
    гейта цвета достаточно чистой заливки."""
    return Image.new("RGB", size, rgb)


def test_green_frame_passes_color_gate():
    """Настоящий зелёный хромакей (эталон RGB(0,177,64)) должен пройти гейт — coverage
    высокий (не 0.0)."""
    img = _solid_frame_image((0, 177, 64))
    cov = batch_print._border_chroma_coverage(img, chroma="green")
    assert cov > 0.9


def test_white_frame_fails_color_gate_even_though_solid():
    """Живой кейс: рамка ОДНОТОННАЯ, но БЕЛАЯ — не должна проходить как зелёный
    хромакей (раньше однотонность белого тоже давала coverage=1.0, гейт цвета не
    проверялся вовсе)."""
    img = _solid_frame_image((255, 255, 255))
    cov = batch_print._border_chroma_coverage(img, chroma="green")
    assert cov == 0.0


def test_blue_frame_passes_color_gate_when_chroma_is_blue():
    """Настоящий синий хромакей (эталон RGB(0,71,255)) проходит гейт при chroma='blue'."""
    img = _solid_frame_image((0, 71, 255))
    cov = batch_print._border_chroma_coverage(img, chroma="blue")
    assert cov > 0.9


def test_blue_frame_fails_color_gate_when_chroma_is_green():
    """Синий хромакей НЕ должен ложно проходить как зелёный (перепутанный chroma)."""
    img = _solid_frame_image((0, 71, 255))
    cov = batch_print._border_chroma_coverage(img, chroma="green")
    assert cov == 0.0


def test_green_frame_fails_color_gate_when_chroma_is_blue():
    """И наоборот — зелёный не проходит как синий."""
    img = _solid_frame_image((0, 177, 64))
    cov = batch_print._border_chroma_coverage(img, chroma="blue")
    assert cov == 0.0


def test_real_calibration_frames_from_past_correct_runs_pass_green_gate():
    """Калибровочная проверка НА РЕАЛЬНЫХ данных: медианные цвета рамки, замеренные на
    живых ПРАВИЛЬНЫХ raw прошлых зелёных прогонов (out_batch/20260708_*), диапазон
    дистанции до эталона green в CrCb ~9..63 — все должны пройти _CHROMA_COLOR_TOL=70."""
    real_green_border_rgbs = [
        (131, 185, 87),   # out_batch/20260708_150331/01_raw.png
        (116, 233, 112),  # out_batch/20260708_150331/02_raw.png
        (127, 216, 53),   # out_batch/20260708_150331/03_raw.png
        (125, 202, 103),  # out_batch/20260708_150331/04_raw.png
        (140, 196, 104),  # out_batch/20260708_153106/01_raw.png
        (133, 202, 79),   # out_batch/20260708_153106/03_raw.png
        (127, 184, 96),   # out_batch/20260708_182047/01_raw.png
        (140, 190, 98),   # out_batch/20260708_183339/01_raw.png
        (139, 229, 101),  # out_batch/20260708_194507/01_raw.png
        (11, 169, 58),    # out_batch/20260708_205007/02_raw.png (правильный, того же батча)
        (11, 173, 56),    # out_batch/20260708_205007/03_raw.png (правильный, того же батча)
    ]
    for rgb in real_green_border_rgbs:
        img = _solid_frame_image(rgb)
        cov = batch_print._border_chroma_coverage(img, chroma="green")
        assert cov > 0.0, f"реальная зелёная рамка {rgb} не должна отсекаться гейтом цвета"


def test_real_white_case_border_fails_green_gate():
    """Калибровочная проверка на РЕАЛЬНОМ белофонном кейсе (медианный цвет рамки
    out_batch/20260708_205007/01_raw.png = чистый белый (255,255,255)) — обязан
    провалить гейт цвета."""
    img = _solid_frame_image((255, 255, 255))
    cov = batch_print._border_chroma_coverage(img, chroma="green")
    assert cov == 0.0


def test_real_blue_case_border_passes_blue_gate():
    """Калибровочная проверка на РЕАЛЬНОМ синем прогоне (медианный цвет рамки
    out_batch/20260708_153106/02_raw.png ~ (86,163,248)) — обязан пройти гейт при
    chroma='blue'."""
    img = _solid_frame_image((86, 163, 248))
    cov = batch_print._border_chroma_coverage(img, chroma="blue")
    assert cov > 0.0


def test_border_chroma_coverage_default_chroma_is_green():
    """Обратная совместимость: вызов без chroma= (как раньше) по умолчанию трактует
    рамку как зелёную."""
    img = _solid_frame_image((0, 177, 64))
    cov = batch_print._border_chroma_coverage(img)
    assert cov > 0.9


# ═══════════════════════════════════════════════════════════════════════════════
# art_director._chroma_bg — запрет белой подложки + запрет цвета хромакея В дизайне
# ═══════════════════════════════════════════════════════════════════════════════

def test_chroma_bg_forbids_white_border_or_sticker_mount():
    prompt = art_director._chroma_bg("green")
    assert "no white border" in prompt.lower() or "no white sticker" in prompt.lower()


def test_chroma_bg_forbids_chroma_color_inside_artwork():
    prompt = art_director._chroma_bg("green")
    assert "ONLY as the flat background" in prompt
    assert "no glow, aura, outline or accent in that color" in prompt


def test_chroma_bg_edge_to_edge_reaches_every_border():
    prompt = art_director._chroma_bg("green")
    assert "edge to edge" in prompt or "reach every edge" in prompt


def test_chroma_bg_blue_variant_also_forbids_blue_inside_artwork():
    """Тот же запрет должен работать для синего хромакея, не только зелёного (общая
    формула по color, не хардкод green)."""
    prompt = art_director._chroma_bg("blue")
    assert "no blue anywhere" in prompt.lower() or "blue anywhere on the character" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# art_director._text_render_block — запрет дублей фразы + видимые пробелы
# ═══════════════════════════════════════════════════════════════════════════════

def _design(**overrides) -> dict:
    base = {
        "prompt": "A young man with spiky hair stands confidently.",
        "chroma": "green",
        "slogan": "BORN TO RIDE",
        "slogan_color": "red",
        "kana": "",
        "character_en": "",
        "title_en": "",
        "signature_props": "",
        "text_mode": "punch",
        "text_modes_v3": [],
        "quote": "",
        "name_jp": "",
        "mood": "",
        "type_spec": "bold street caps lettering placed along the bottom",
    }
    base.update(overrides)
    return base


def test_text_render_block_forbids_duplicate_phrase():
    block = art_director._text_render_block(_design())
    assert "exactly ONCE" in block
    assert "no duplicates elsewhere" in block


def test_text_render_block_requires_visible_word_spacing():
    block = art_director._text_render_block(_design())
    assert "visible spacing between words" in block
    assert "must never touch or merge" in block


def test_build_prompt_text_render_image_includes_no_duplicate_and_spacing_rules(monkeypatch):
    import config
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    prompt = art_director.build_prompt(_design())
    assert "no duplicates elsewhere" in prompt
    assert "must never touch or merge" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# Двенадцатый заход (регресс по отчёту тестировщика, 2026-07-08)
# ═══════════════════════════════════════════════════════════════════════════════
#
# batch_print._verify_text — QC-гейт ДУБЛИРОВАНИЯ фразы (не только substring-вхождения)
# ═══════════════════════════════════════════════════════════════════════════════

def _tiny_rgba(w=20, h=20) -> Image.Image:
    return Image.new("RGBA", (w, h), (10, 20, 30, 255))


def test_count_nonoverlapping_basic():
    assert batch_print._count_nonoverlapping("ABCABC", "ABC") == 2
    assert batch_print._count_nonoverlapping("ABC", "ABC") == 1
    assert batch_print._count_nonoverlapping("XYZ", "ABC") == 0
    assert batch_print._count_nonoverlapping("ABC", "") == 0


def test_verify_text_real_reported_duplicate_transcript_fails(monkeypatch):
    """Регресс живого бага (out_batch/20260708_211920/02, Тандзиро) — РЕАЛЬНЫЙ OCR-
    транскрипт из отчёта тестировщика (фраза встречается дважды в разном регистре/
    форматировании) ДОЛЖЕН провалить _verify_text. ДО фикса (substring-вхождение без
    подсчёта повторов) этот же транскрипт формально проходил как OK, хотя финальная
    картинка содержала дублированную фразу."""
    real_transcript = ("I WILL NEVER STOP\nNO MATTER WHAT\nI will never stop\n"
                        "no matter what")
    monkeypatch.setattr(providers, "verify_text_in_image", lambda img: real_transcript)
    ok = batch_print._verify_text(_tiny_rgba(), ["I will never stop no matter what"])
    assert ok is False


def test_verify_text_phrase_present_exactly_once_passes(monkeypatch):
    """Контрольный случай (дизайн 01, Кенпачи, из того же живого батча — чистый,
    фраза один раз) — не должен ложно провалиться из-за нового гейта дублей."""
    monkeypatch.setattr(providers, "verify_text_in_image",
                        lambda img: "I just want to keep fighting until I die.")
    ok = batch_print._verify_text(_tiny_rgba(),
                                   ["I just want to keep fighting until I die"])
    assert ok is True


def test_verify_text_duplicate_phrase_logs_distinct_reason(monkeypatch, capsys):
    monkeypatch.setattr(providers, "verify_text_in_image",
                        lambda img: "LETS PARTY LETS PARTY")
    ok = batch_print._verify_text(_tiny_rgba(), ["LETS PARTY"])
    assert ok is False
    out = capsys.readouterr().out
    assert "повторены ДВАЖДЫ" in out


def test_verify_text_missing_phrase_still_fails_as_before(monkeypatch):
    """Старое поведение (фраза отсутствует вовсе) не сломано новым гейтом дублей."""
    monkeypatch.setattr(providers, "verify_text_in_image", lambda img: "SOME OTHER TEXT")
    ok = batch_print._verify_text(_tiny_rgba(), ["LETS PARTY"])
    assert ok is False


def test_verify_text_name_jp_duplicate_also_fails():
    """Дубль кандзи-колонки (name_jp) ловится той же логикой, что и slogan/quote —
    _verify_text не выделяет name_jp как особый случай."""
    import providers as _providers

    def _fake(img):
        return "更木剣八 更木剣八"

    orig = _providers.verify_text_in_image
    _providers.verify_text_in_image = _fake
    try:
        ok = batch_print._verify_text(_tiny_rgba(), ["更木剣八"])
        assert ok is False
    finally:
        _providers.verify_text_in_image = orig


# ═══════════════════════════════════════════════════════════════════════════════
# art_director._text_render_block / _TYPE_SPEC_SCHEMA — третье усиление (named
# anti-pattern: маленькое эхо у фигуры + отдельная крупная копия внизу)
# ═══════════════════════════════════════════════════════════════════════════════

def test_text_render_block_names_the_echo_near_figure_antipattern():
    block = art_director._text_render_block(_design())
    assert "echo or teaser copy" in block
    assert "ONE lettering placement" in block


def test_text_render_block_forbids_rephrasing_same_meaning():
    block = art_director._text_render_block(_design())
    assert "rephrase or repeat the same meaning" in block


def test_type_spec_schema_requires_single_placement_zone():
    assert "РОВНО ОДНУ зону размещения" in art_director._TYPE_SPEC_SCHEMA


# ═══════════════════════════════════════════════════════════════════════════════
# art_director._chroma_bg — запрет sticker-style обводки ВОКРУГ силуэта (не только
# рамки кадра)
# ═══════════════════════════════════════════════════════════════════════════════

def test_chroma_bg_forbids_outline_hugging_the_silhouette():
    prompt = art_director._chroma_bg("green")
    assert "hugging the silhouette" in prompt
    assert "no white or light-colored ring, halo outline, or contour stroke" in prompt


def test_chroma_bg_silhouette_outline_ban_works_for_blue_too():
    prompt = art_director._chroma_bg("blue")
    assert "hugging the silhouette" in prompt
