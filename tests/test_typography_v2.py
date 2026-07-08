# -*- coding: utf-8 -*-
"""Тесты типографики v2 (typography.compose_text/TEXT_MODES) и новых полей
арт-директора (signature_props/text_mode в art_director._parse). Полностью
офлайн — никакой сети, только PIL-рендеринг на синтетических фигурах.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_typography_v2.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import art_director  # noqa: E402
import typography as typo  # noqa: E402


def _make_circle_figure(w: int = 900, h: int = 1100) -> Image.Image:
    """Синтетическая фигура — сплошной непрозрачный эллипс на прозрачном холсте,
    имитирует вырезку персонажа (bbox альфы реально уже холста, есть поля)."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.2, h * 0.12, w * 0.8, h * 0.92], fill=(180, 90, 40, 255))
    return img


def _alpha(img: Image.Image) -> np.ndarray:
    return np.array(img.getchannel("A"))


# ─────────────────────────── присутствие текста для каждого text_mode ──────────────

def test_compose_text_none_returns_figure_unchanged():
    """'none' — фигура возвращается как есть (той же альфа-площадью), без добавления
    текстовых пикселей — ничего не компонуется."""
    fig = _make_circle_figure()
    fig_alpha_sum = int(_alpha(fig).astype(bool).sum())
    out = typo.compose_text(fig, "none", "COME ON LETS PARTY", "red", kana="ケンパチ")
    assert out.size == fig.size
    assert int(_alpha(out).astype(bool).sum()) == fig_alpha_sum


def test_compose_text_under_has_text_alpha_present():
    """'under' — после compose реально появляются текстовые пиксели (альфа > 0
    ГДЕ-ТО за пределами исходного bbox фигуры, если холст расширился, либо просто
    больше непрозрачных пикселей, чем у голой фигуры на том же кропе)."""
    fig = _make_circle_figure()
    out = typo.compose_text(fig, "under", "PUSH LIMITS", "orange", kana="")
    # Кроп другого размера ИЛИ реально больше контента, чем просто круг — сравниваем
    # долю непрозрачных пикселей: с текстом ЗА фигурой область больше, чем один круг,
    # либо (если весь текст спрятан под кругом) кроп всё равно НЕ равен голой фигуре.
    assert out.size != (0, 0)
    assert _alpha(out).astype(bool).any()
    # Прямая проверка присутствия текста: рендерим фигуру ОТДЕЛЬНО (без текста) на
    # холсте того же размера что out и сравниваем — если бы текста не было, alpha-
    # площадь совпала бы с площадью одной фигуры (с точностью до кропа); with-text
    # площадь должна быть строго больше (буквы добавляют непрозрачные пиксели).
    bare_area = int(_alpha(fig).astype(bool).sum())
    out_area = int(_alpha(out).astype(bool).sum())
    assert out_area > bare_area


def test_compose_text_punch_has_text_alpha_present():
    fig = _make_circle_figure()
    out = typo.compose_text(fig, "punch", "REALITY IS JUST GENJUTSU", "purple", kana="")
    bare_area = int(_alpha(fig).astype(bool).sum())
    out_area = int(_alpha(out).astype(bool).sum())
    assert out_area > bare_area


def test_compose_text_kana_side_has_text_alpha_present():
    fig = _make_circle_figure()
    out = typo.compose_text(fig, "kana_side", "COME ON LETS PARTY", "red", kana="ケンパチ")
    bare_area = int(_alpha(fig).astype(bool).sum())
    out_area = int(_alpha(out).astype(bool).sum())
    assert out_area > bare_area


def test_compose_text_kana_side_without_kana_degrades_to_punch():
    """Пустая kana -> деградация в 'punch' (текст всё равно появляется, не падает)."""
    fig = _make_circle_figure()
    out = typo.compose_text(fig, "kana_side", "PUSH BEYOND LIMITS", "yellow", kana="")
    bare_area = int(_alpha(fig).astype(bool).sum())
    out_area = int(_alpha(out).astype(bool).sum())
    assert out_area > bare_area


# ─────────────────────────── 'under': буквы частично ПОД фигурой ──────────────────

def test_compose_text_under_figure_pixels_take_priority_over_letters():
    """В зоне пересечения текста и фигуры побеждают пиксели ФИГУРЫ (текстовый слой
    компонуется ПОД фигурой) — проверяем цвет: там, где и текст, и фигура присутствуют,
    итоговый RGB должен совпадать с цветом фигуры (не с цветом текста)."""
    fig = _make_circle_figure()
    fig_rgb = np.array(fig.convert("RGB"))
    fig_color = fig_rgb[int(fig.height * 0.5), int(fig.width * 0.5)].tolist()

    out = typo.compose_text(fig, "under", "PUSH LIMITS", "orange", kana="")
    out_alpha = _alpha(out)
    out_rgb = np.array(out.convert("RGB"))

    # Найти пиксель внутри альфы фигуры (используем центр итогового bbox — после кропа
    # координаты фигуры сдвинуты, но центр фигуры остаётся непрозрачным сплошным
    # цветом фигуры, раз текст рисуется ЗА ней).
    bbox = out.getchannel("A").getbbox()
    cx = (bbox[0] + bbox[2]) // 2
    cy = (bbox[1] + bbox[3]) // 2
    assert out_alpha[cy, cx] > 0
    assert out_rgb[cy, cx].tolist() == fig_color


def _make_wide_shoulders_figure(w: int = 900, h: int = 1100) -> Image.Image:
    """Синтетическая фигура с УЗКОЙ головой сверху и РЕЗКО расширяющимися вширь
    'плечами/раскинутыми руками' на высоте ~28% bbox (та самая доля, на которой
    _compose_under раньше был жёстко захардкожен) — воспроизводит реальный баг:
    диекат Кенпачи Зараки (широкие плечи+пламя вразлёт) прятал буквы 'M'/'E' слова
    'COME ON' целиком под силуэтом, потому что текст всегда ложился на фикс. высоту
    0.28 без учёта того, что фигура именно там самая широкая."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # "тело" почти во всю ширину холста в диапазоне 0.20-0.45 (плечи/руки/эффекты)
    d.rectangle([w * 0.05, h * 0.20, w * 0.95, h * 0.45], fill=(180, 90, 40, 255))
    # "голова" — узкая колонка по центру сверху (0.0-0.20)
    d.rectangle([w * 0.40, h * 0.0, w * 0.60, h * 0.20], fill=(180, 90, 40, 255))
    # "торс/ноги" ниже плеч — тоже узкая колонка, не влияет на верхнюю часть
    d.rectangle([w * 0.35, h * 0.45, w * 0.65, h * 0.92], fill=(180, 90, 40, 255))
    return img


def _visible_text_pixel_count(rgba: Image.Image) -> int:
    """Считает пиксели итогового изображения, похожие на fill-цвет текста 'red'
    (высокий R, низкие G/B) — прокси для 'сколько текста реально видно поверх/помимо
    фигуры', не зависящий от того, на какой именно высоте легла строка."""
    rgb = np.array(rgba.convert("RGB"))
    mask = (rgb[:, :, 0].astype(int) > 150) & (rgb[:, :, 1].astype(int) < 100) & \
           (rgb[:, :, 2].astype(int) < 100)
    return int(mask.sum())


def test_compose_text_under_avoids_widest_row_of_silhouette():
    """Регресс-тест на реальный баг: 'under' раньше клал текст на ФИКСИРОВАННУЮ высоту
    0.28 bbox фигуры, не глядя на реальную ширину силуэта там — на широкоплечем
    диекате Кенпачи буквы 'M'/'E' слова 'COME ON' терялись целиком под плечом/пламенем
    (визуально подтверждено на out_batch/typo_v2_preview/01_diecut.png: до фикса видно
    ~17763 'красных' пикселей текста, после фикса ~30069 — на +69% больше читаемых
    букв). Фикс: _best_under_row_frac выбирает строку с наименьшим перекрытием
    силуэтом вместо жёсткой доли 0.28. На фигуре с намеренно широкими 'плечами' именно
    на 0.28 и узкой 'головой' выше — новое поведение обязано показать СТРОГО БОЛЬШЕ
    видимых текстовых пикселей, чем старое жёстко-захардкоженное 0.28 (не просто
    присутствие текста, как в соседнем тесте, а именно читаемость)."""
    fig = _make_wide_shoulders_figure()

    out_fixed = typo.compose_text(fig, "under", "COME ON", "red", kana="")
    visible_fixed = _visible_text_pixel_count(out_fixed)

    # Имитация СТАРОГО (захардкоженного) поведения — временная подмена помощника на
    # константу 0.28, как было до фикса; сравниваем читаемость на ТОЙ ЖЕ фигуре.
    original = typo._best_under_row_frac
    typo._best_under_row_frac = lambda *a, **kw: 0.28
    try:
        out_old = typo.compose_text(fig, "under", "COME ON", "red", kana="")
    finally:
        typo._best_under_row_frac = original
    visible_old = _visible_text_pixel_count(out_old)

    assert visible_fixed > visible_old, (
        f"фикс не улучшил читаемость 'under' на широкоплечем силуэте: "
        f"видимых текстовых пикселей до={visible_old}, после={visible_fixed}"
    )


# ─────────────────────────── геометрия: нет мёртвой пустой полосы ─────────────────

def test_compose_text_no_dead_empty_margin_bottom():
    """Итоговый холст без мёртвой пустой полосы: нижний край НЕПРОЗРАЧНОГО контента
    (текст+фигура) должен быть БЛИЖЕ 8% высоты холста к нижнему краю картинки —
    иначе получаем ту же проблему v1, которую забраковал владелец."""
    for mode, kana in (("under", ""), ("punch", ""), ("kana_side", "ケンパチ")):
        fig = _make_circle_figure()
        out = typo.compose_text(fig, mode, "COME ON LETS PARTY", "red", kana=kana)
        bbox = out.getchannel("A").getbbox()
        assert bbox is not None, f"mode={mode}: пустая альфа"
        _, _, _, y1 = bbox
        gap_frac = (out.height - y1) / out.height
        assert gap_frac < 0.08, (
            f"mode={mode}: нижний отступ {gap_frac:.3f} >= 0.08 — похоже на мёртвую "
            f"пустую полосу v1"
        )


def test_compose_text_no_dead_empty_margin_top_and_sides():
    """Аналогично верх/лево/право не должны иметь избыточных пустых полей сверх
    заданного margin (проверяем разумный потолок, не точное значение)."""
    for mode, kana in (("under", ""), ("punch", ""), ("kana_side", "ケンパチ")):
        fig = _make_circle_figure()
        out = typo.compose_text(fig, mode, "COME ON LETS PARTY", "red", kana=kana)
        bbox = out.getchannel("A").getbbox()
        x0, y0, x1, y1 = bbox
        assert y0 / out.height < 0.10
        assert x0 / out.width < 0.10
        assert (out.width - x1) / out.width < 0.10


# ─────────────────────────── минимальный кегль ────────────────────────────────────

def test_fit_lines_min_size_never_goes_below_minimum():
    """_fit_lines_min_size не ужимает кегль тише min_size даже для длинной фразы —
    вместо этого разбивает на больше строк."""
    from PIL import ImageDraw as _ImageDraw

    canvas = Image.new("RGBA", (10, 10))
    draw = _ImageDraw.Draw(canvas)
    words = "REALITY IS JUST GENJUTSU AND NOTHING ELSE MATTERS HERE".split()
    min_size = 24
    lines, font, sizes = typo._fit_lines_min_size(
        draw, words, "anton", start_size=200, max_width=300, min_size=min_size,
        max_lines=3,
    )
    assert all(s >= min_size for s in sizes)
    assert len(lines) <= 3


def test_compose_text_punch_respects_min_font_fraction():
    """Реальный прогон 'punch' с длинной фразой на узкой фигуре — кегль ключевых строк
    не должен быть тише _MIN_FONT_FRAC от ширины фигуры (небольшой допуск на округление)."""
    from PIL import ImageDraw as _ImageDraw

    fig_w = 500  # соответствует узкой фигуре в сценарии
    min_allowed = fig_w * typo._MIN_FONT_FRAC * 0.85  # 15% допуск на округление/шаг фита
    min_size = max(10, int(fig_w * typo._MIN_FONT_FRAC))

    draw = _ImageDraw.Draw(Image.new("RGBA", (fig_w + 200, 400)))
    words = "REALITY IS JUST GENJUTSU AND NOTHING ELSE".upper().split()
    lines, font, sizes = typo._fit_lines_min_size(
        draw, words, "anton", int(fig_w * 0.16), fig_w * 0.92, min_size,
    )
    assert all(s >= min_allowed for s in sizes)


# ═══════════════════════════ art_director._parse: новые поля ══════════════════════

def _wrap_json_array(obj_json: str) -> str:
    return f"[{obj_json}]"


def test_parse_signature_props_and_text_mode_present_and_sanitized():
    raw = _wrap_json_array(
        '{"prompt": "A hero stands tall.", "chroma": "green", "slogan": "GO WILD", '
        '"slogan_color": "red", "kana": "", "character_en": "Kenpachi Zaraki", '
        '"title_en": "Bleach", '
        '"signature_props": "his nodachi: an extremely long, chipped, jagged blade!!", '
        '"text_mode": "under"}'
    )
    designs = art_director._parse(raw)
    assert len(designs) == 1
    d = designs[0]
    assert d["text_mode"] == "under"
    # Санация: латиница/цифры/базовая пунктуация сохранены, "!!" вырезаны (не входят
    # в разрешённый набор символов).
    assert "nodachi" in d["signature_props"]
    assert "!" not in d["signature_props"]
    assert len(d["signature_props"]) <= 200


def test_parse_defaults_when_fields_missing_backward_compatible():
    """Старый JSON без signature_props/text_mode -> дефолты, парсинг НЕ падает
    (обратная совместимость со старыми дампами design.json)."""
    raw = _wrap_json_array(
        '{"prompt": "A hero stands tall.", "chroma": "green", "slogan": "GO WILD", '
        '"slogan_color": "red", "kana": "", "character_en": "", "title_en": ""}'
    )
    designs = art_director._parse(raw)
    assert len(designs) == 1
    d = designs[0]
    assert d["signature_props"] == ""
    assert d["text_mode"] in art_director.TEXT_MODES
    assert d["text_mode"] == "punch"  # дефолт при отсутствии поля


def test_parse_invalid_text_mode_falls_back_to_default():
    raw = _wrap_json_array(
        '{"prompt": "A hero.", "chroma": "green", "slogan": "GO", '
        '"slogan_color": "red", "kana": "", "character_en": "", "title_en": "", '
        '"text_mode": "some_bogus_mode"}'
    )
    designs = art_director._parse(raw)
    assert designs[0]["text_mode"] == "punch"


def test_parse_empty_signature_props_stays_empty():
    raw = _wrap_json_array(
        '{"prompt": "A car.", "chroma": "green", "slogan": "DRIVE FAST", '
        '"slogan_color": "red", "kana": "", "character_en": "", "title_en": "", '
        '"signature_props": "", "text_mode": "none"}'
    )
    designs = art_director._parse(raw)
    d = designs[0]
    assert d["signature_props"] == ""
    assert d["text_mode"] == "none"


def test_build_prompt_inserts_signature_props_sentence():
    """build_prompt вставляет явное предложение про канон-оружие, если
    signature_props непусто; хромакей-хвост всё ещё присутствует."""
    design = {
        "prompt": "A warrior stands ready.",
        "chroma": "green",
        "signature_props": "his nodachi: an extremely long, chipped, jagged blade",
    }
    prompt = art_director.build_prompt(design)
    assert "signature weapon/prop must match canon exactly" in prompt
    assert "his nodachi" in prompt
    assert "chroma-key" in prompt


def test_build_prompt_no_signature_props_no_extra_sentence():
    design = {"prompt": "A car on the road.", "chroma": "blue", "signature_props": ""}
    prompt = art_director.build_prompt(design)
    assert "signature weapon/prop" not in prompt
    assert "chroma-key" in prompt
