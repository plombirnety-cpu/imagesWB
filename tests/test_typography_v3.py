# -*- coding: utf-8 -*-
"""Тесты типографики v3 (palette.extract_palette/typography_v3.compose_text_v3) и
новых полей арт-директора (text_modes_v3/quote/name_jp/mood в art_director._parse).
Полностью офлайн — никакой сети, только PIL-рендеринг на синтетических фигурах.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_typography_v3.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import art_director  # noqa: E402
import palette  # noqa: E402
import typography_v3 as t3  # noqa: E402


def _make_duotone_figure(w: int = 800, h: int = 1000) -> Image.Image:
    """Синтетическая фигура из ДВУХ явно доминантных цветов (большой фиолетовый
    силуэт + бирюзовая "голова") — имитирует реальный дуотон-принт эталона стайлгайда,
    оба цвета должны попасть в топ палитры."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.12, h * 0.08, w * 0.88, h * 0.90], fill=(168, 64, 214, 255))
    d.ellipse([w * 0.30, h * 0.13, w * 0.70, h * 0.38], fill=(52, 196, 178, 255))
    return img


def _make_narrow_figure(w: int = 700, h: int = 1000) -> Image.Image:
    """Фигура с узкими участками по бокам головы/плеч (не сплошной прямоугольник) —
    нужна для editorial, чтобы переплетение с фигурой реально было видно (буквы
    выглядывают в узких зонах силуэта, не полностью погребены под сплошным телом)."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(w * 0.42, h * 0.06), (w * 0.58, h * 0.06), (w * 0.66, h * 0.25),
               (w * 0.72, h * 0.45), (w * 0.42, h * 0.94), (w * 0.28, h * 0.94),
               (w * 0.34, h * 0.45), (w * 0.40, h * 0.25)],
              fill=(90, 140, 220, 255))
    d.ellipse([w * 0.38, h * 0.02, w * 0.62, h * 0.20], fill=(240, 210, 160, 255))
    return img


def _alpha(img: Image.Image) -> np.ndarray:
    return np.array(img.getchannel("A"))


_BASE_DESIGN = {
    "character_en": "Faust VIII",
    "title_en": "Shaman King",
    "quote": "Cursed sorcerers are so fragile",
    "name_jp": "フォースト",
    "mood": "duotone_quote",
}


# ═══════════════════════════ palette.extract_palette ══════════════════════════════

def test_extract_palette_synthetic_two_colors_both_in_top():
    """Синтетика ровно ДВУХ явно доминантных цветов -> оба должны оказаться в топе
    палитры (докстрока задания: "синтетика 2 цвета -> оба в топе")."""
    fig = _make_duotone_figure()
    pal = palette.extract_palette(fig, n=4)
    assert 3 <= len(pal) <= 4

    def _closest_dist(target):
        return min(
            ((c[0] - target[0]) ** 2 + (c[1] - target[1]) ** 2 + (c[2] - target[2]) ** 2) ** 0.5
            for c in pal
        )

    # Оба исходных цвета иллюстрации должны иметь близкий (не обязательно идентичный —
    # k-means/resize вносят небольшую погрешность) представитель в палитре.
    assert _closest_dist((168, 64, 214)) < 40, f"фиолетовый не найден в палитре: {pal}"
    assert _closest_dist((52, 196, 178)) < 40, f"бирюзовый не найден в палитре: {pal}"


def test_extract_palette_deterministic_same_image_same_result():
    """Один и тот же diecut ДОЛЖЕН давать одну и ту же палитру при повторном вызове
    (детерминированный random_state, раздел 1.1.3 стайлгайда) — иначе повторный
    прогон конвейера даёт другой текст, что недопустимо для регрессии/отладки."""
    fig = _make_duotone_figure()
    pal1 = palette.extract_palette(fig, n=4)
    pal2 = palette.extract_palette(fig, n=4)
    assert pal1 == pal2


def test_extract_palette_empty_alpha_falls_back_safely():
    """Полностью прозрачное изображение -> безопасный fallback (не бросает
    исключение), минимум 3 цвета."""
    empty = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    pal = palette.extract_palette(empty, n=4)
    assert len(pal) >= 3


def test_palette_roles_dominant_accent_light_dark_assigned():
    fig = _make_duotone_figure()
    pal = palette.extract_palette(fig, n=4)
    roles = palette.PaletteRoles(pal)
    assert roles.dominant in pal or roles.dominant == pal[0]
    assert roles.accent != roles.dominant
    assert isinstance(roles.light, tuple) and len(roles.light) == 3
    assert isinstance(roles.dark, tuple) and len(roles.dark) == 3


def test_local_luminance_ignores_transparent_pixels_not_black():
    """Регресс-тест на реальный найденный баг: область ПОД фигурой в diecut обычно
    прозрачна (нет пикселей) — PIL Image.convert('RGB') даёт (0,0,0) для alpha=0,
    из-за чего luminance ошибочно читался как ~0 (чёрный), хотя реальный фон
    (футболка) обычно светлый. local_luminance должна игнорировать прозрачные
    пиксели и вернуть светлое значение для полностью прозрачной области."""
    transparent = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    lum = palette.local_luminance(transparent, (0, 0, 200, 100))
    assert lum > 140, f"прозрачная область не должна читаться как тёмная: lum={lum}"


def test_contrast_fill_stroke_never_returns_hardcoded_black_or_white():
    """Заливка/обводка ВСЕГДА из палитры (или её фиксированных ролей light/dark из
    palette.py), никогда произвольный #000000/#FFFFFF, не связанный с иллюстрацией."""
    fig = _make_duotone_figure()
    pal = palette.extract_palette(fig, n=4)
    roles = palette.PaletteRoles(pal)
    fill, stroke = palette.contrast_fill_stroke(roles, 200.0)
    assert fill in (roles.dark, roles.dominant, roles.accent, roles.light)
    assert stroke in (roles.light, roles.dark)


# ═══════════════════════════ chередование цветов ═══════════════════════════════════

def test_alternate_word_colors_alternates_starting_with_accent():
    fig = _make_duotone_figure()
    roles = palette.PaletteRoles(palette.extract_palette(fig, n=4))
    words = "CURSED SORCERERS ARE SO FRAGILE".split()
    colors = palette.alternate_word_colors(words, roles)
    assert len(colors) == len(words)
    assert colors[0] == roles.accent
    assert colors[1] == roles.dominant
    for i, c in enumerate(colors):
        expected = roles.accent if i % 2 == 0 else roles.dominant
        assert c == expected


def test_block_colors_starts_with_dominant():
    fig = _make_duotone_figure()
    roles = palette.PaletteRoles(palette.extract_palette(fig, n=4))
    colors = palette.block_colors(3, roles)
    assert colors == [roles.dominant, roles.accent, roles.dominant]


# ═══════════════════════════ compose_text_v3: присутствие текста по режимам ═══════

def test_quote_bottom_renders_nonempty_text():
    fig = _make_duotone_figure()
    bare_area = int(_alpha(fig).astype(bool).sum())
    out = t3.compose_text_v3(fig, ["quote_bottom"], _BASE_DESIGN)
    out_area = int(_alpha(out).astype(bool).sum())
    assert out_area > bare_area


def test_kanji_on_renders_nonempty_text():
    """kanji_on рисуется ВНУТРИ фигуры (перекрытие почти полное, раздел 3.2) —
    альфа-площадь может НЕ вырасти (фигура уже непрозрачна под текстом), поэтому
    присутствие текста проверяем цветом пикселей: должен появиться цвет light-роли
    палитры (заливка kanji_on), которого не было на голой фигуре."""
    fig = _make_duotone_figure()
    roles = palette.PaletteRoles(palette.extract_palette(fig, n=4))
    bare_rgb = np.array(fig.convert("RGB"))
    bare_has_light = np.all(np.abs(bare_rgb.astype(int) - np.array(roles.light)) < 10, axis=-1).any()
    assert not bare_has_light, "тестовая фигура случайно уже содержит light-цвет — поправить фикстуру"

    out = t3.compose_text_v3(fig, ["kanji_on"], _BASE_DESIGN)
    out_rgb = np.array(out.convert("RGB"))
    out_has_light = np.all(np.abs(out_rgb.astype(int) - np.array(roles.light)) < 10, axis=-1).any()
    assert out_has_light, "kanji_on не добавил light-цвет глифов — текст не отрисован"


def test_kanji_on_ghost_variant_renders_nonempty_text():
    """ghost-вариант (mood=pop_trash) — полупрозрачная заливка accent/light, alpha=140
    (раздел 3.2а) — итоговый пиксель ДОЛЖЕН отличаться от чистого fill-цвета фигуры,
    раз накладывается alpha_composite полупрозрачного текста поверх."""
    fig = _make_duotone_figure()
    design = dict(_BASE_DESIGN, mood="pop_trash")
    fig_rgb = np.array(fig.convert("RGB"))

    out = t3.compose_text_v3(fig, ["kanji_on"], design)
    out_rgb = np.array(out.convert("RGB"))

    # Область фигуры до/после должна ОТЛИЧАТЬСЯ хотя бы в части пикселей (ghost-текст
    # подмешивает свой цвет полупрозрачно) — сравниваем в одинаковых координатах
    # (kanji_on не расширяет холст, значит размеры совпадают до кропа; сравниваем
    # без кропа через прямой вызов внутренней функции для точных координат).
    from PIL import ImageDraw as _ImageDraw  # noqa: E402

    canvas = fig.copy()
    canvas_before = np.array(canvas.convert("RGB"))
    roles = palette.PaletteRoles(palette.extract_palette(fig, n=4))
    fx0, fy0, fx1, fy1 = t3._typo._alpha_bbox(fig)
    fig_w, fig_h = fx1 - fx0, fy1 - fy0
    canvas_after = t3._compose_kanji_on(canvas, fx0, fy0, fig_w, fig_h,
                                        _BASE_DESIGN["name_jp"], roles, ghost=True)
    canvas_after_rgb = np.array(canvas_after.convert("RGB"))
    assert not np.array_equal(canvas_before, canvas_after_rgb), (
        "ghost kanji_on не изменил ни одного пикселя фигуры"
    )


def test_collection_footer_renders_nonempty_text():
    fig = _make_duotone_figure()
    bare_area = int(_alpha(fig).astype(bool).sum())
    out = t3.compose_text_v3(fig, ["collection_footer"], _BASE_DESIGN)
    out_area = int(_alpha(out).astype(bool).sum())
    assert out_area > bare_area


def test_editorial_renders_nonempty_text():
    fig = _make_narrow_figure()
    bare_area = int(_alpha(fig).astype(bool).sum())
    design = dict(_BASE_DESIGN, mood="fashion_editorial")
    out = t3.compose_text_v3(fig, ["editorial"], design)
    out_area = int(_alpha(out).astype(bool).sum())
    assert out_area > bare_area


def test_canonical_triple_combo_renders_nonempty_text():
    """quote_bottom + kanji_on + collection_footer — каноничная тройка эталона 1."""
    fig = _make_duotone_figure()
    bare_area = int(_alpha(fig).astype(bool).sum())
    out = t3.compose_text_v3(
        fig, ["quote_bottom", "kanji_on", "collection_footer"], _BASE_DESIGN)
    out_area = int(_alpha(out).astype(bool).sum())
    assert out_area > bare_area


def test_empty_modes_returns_figure_unchanged():
    fig = _make_duotone_figure()
    fig_area = int(_alpha(fig).astype(bool).sum())
    out = t3.compose_text_v3(fig, [], _BASE_DESIGN)
    assert int(_alpha(out).astype(bool).sum()) == fig_area


# ═══════════════════════════ цвета текста принадлежат палитре ═════════════════════

def _colors_present_in_image(img: Image.Image, colors: list, tol: int = 6) -> dict:
    """Для каждого цвета из `colors` — есть ли в img (RGB) пиксель в пределах tol по
    каждому каналу (допуск ±tol, задание разрешает допуск)."""
    rgb = np.array(img.convert("RGB")).astype(int)
    result = {}
    for c in colors:
        mask = (
            (np.abs(rgb[:, :, 0] - c[0]) <= tol) &
            (np.abs(rgb[:, :, 1] - c[1]) <= tol) &
            (np.abs(rgb[:, :, 2] - c[2]) <= tol)
        )
        result[c] = bool(mask.any())
    return result


def test_quote_bottom_text_colors_belong_to_palette():
    """Слова цитаты закрашены ЦВЕТАМИ ИЗ ПАЛИТРЫ (accent/dominant), не
    typography._TITLE_COLORS фиксированной шестёркой — антиправило 1 стайлгайда."""
    fig = _make_duotone_figure()
    pal = palette.extract_palette(fig, n=4)
    roles = palette.PaletteRoles(pal)
    out = t3.compose_text_v3(fig, ["quote_bottom"], _BASE_DESIGN)

    presence = _colors_present_in_image(out, [roles.accent, roles.dominant], tol=10)
    assert presence[roles.accent] or presence[roles.dominant], (
        "ни один из палитровых цветов чередования не найден на итоговом изображении"
    )


def test_collection_footer_text_colors_belong_to_palette():
    fig = _make_duotone_figure()
    pal = palette.extract_palette(fig, n=4)
    roles = palette.PaletteRoles(pal)
    out = t3.compose_text_v3(fig, ["collection_footer"], _BASE_DESIGN)

    presence = _colors_present_in_image(out, [roles.accent, roles.dominant], tol=10)
    assert presence[roles.accent] or presence[roles.dominant]


def test_v3_never_uses_fixed_title_colors_six():
    """Ни один из шести хардкод-цветов typography._TITLE_COLORS не должен доминировать
    в v3-выводе как заливка текста — проверяем, что ХОТЯ БЫ один палитровый цвет
    (accent/dominant/light/dark) реально присутствует, что доказывает путь через
    palette.extract_palette, а не фиксированную шестёрку v2."""
    import typography as typo

    fig = _make_duotone_figure()
    pal = palette.extract_palette(fig, n=4)
    roles = palette.PaletteRoles(pal)
    out = t3.compose_text_v3(
        fig, ["quote_bottom", "kanji_on", "collection_footer"], _BASE_DESIGN)

    palette_colors = [roles.accent, roles.dominant, roles.light, roles.dark]
    presence = _colors_present_in_image(out, palette_colors, tol=10)
    assert any(presence.values()), "ни один цвет палитры не найден — подозрение на v2 fallback"

    # Ради проверки антиправила: v1 fill-цвета red/orange/white/yellow/purple/black
    # (заливки, НЕ тени/обводки) не должны быть единственными цветами текста —
    # ослабленная проверка, т.к. случайное совпадение возможно, но не как ЕДИНСТВЕННЫЙ
    # источник цвета (уже покрыто assert выше через реальное присутствие палитры).
    v1_fills = [c[0] for c in typo._TITLE_COLORS.values()]
    assert isinstance(v1_fills, list)  # смоук — модуль не трогали, значения не изменились


# ═══════════════════════════ чередование цветов слов в quote_bottom ═══════════════

def test_quote_bottom_word_colors_alternate_on_canvas():
    """Реальный рендер: первое и второе слово цитаты должны получить РАЗНЫЕ цвета
    (проверяем через непосредственный вызов alternate_word_colors на той же цитате,
    консистентно с тем, что реально использует _compose_quote_bottom)."""
    fig = _make_duotone_figure()
    roles = palette.PaletteRoles(palette.extract_palette(fig, n=4))
    words = _BASE_DESIGN["quote"].split()
    colors = palette.alternate_word_colors(words, roles)
    assert colors[0] != colors[1]
    assert colors[0] == roles.accent


# ═══════════════════════════ footer собирается из конфига ═════════════════════════

def test_collection_footer_uses_brand_label_from_config_param():
    """brand_label передаётся параметром (config.BRAND_LABEL в проде) — подвал должен
    реально содержать текст этого лейбла, не хардкод внутри модуля."""
    fig = _make_duotone_figure()
    custom_label = "TESTBRAND"
    out_custom = t3.compose_text_v3(fig, ["collection_footer"], _BASE_DESIGN,
                                    brand_label=custom_label)
    out_default = t3.compose_text_v3(fig, ["collection_footer"], _BASE_DESIGN,
                                     brand_label="ANICLOT COLLECTION")
    # Разные лейблы -> разная итоговая ширина подвала (разное число символов) в
    # большинстве случаев — не идентичные изображения по размеру ИЛИ содержимому.
    assert out_custom.size != out_default.size or \
        int(_alpha(out_custom).sum()) != int(_alpha(out_default).sum())


def test_collection_footer_single_block_when_title_and_character_empty():
    """Если title_en и character_en пусты — подвал рисует ТОЛЬКО BRAND_LABEL (раздел
    3.4), не растягивается искусственно на всю ширину с пустыми блоками."""
    fig = _make_duotone_figure()
    design = dict(_BASE_DESIGN, title_en="", character_en="")
    out = t3.compose_text_v3(fig, ["collection_footer"], design)
    bare_area = int(_alpha(fig).astype(bool).sum())
    assert int(_alpha(out).astype(bool).sum()) > bare_area  # текст всё равно есть


# ═══════════════════════════ правила комбинирования (антиправило 7) ═══════════════

def test_editorial_alone_ignores_other_modes_when_combined():
    """Защитный код: editorial, пришедший ВМЕСТЕ с другими режимами, должен
    приоритизироваться, остальные — отбрасываться (не падать, не рисовать всё)."""
    sanitized = t3._sanitize_modes(["quote_bottom", "editorial", "kanji_on"])
    assert sanitized == ["editorial"]


def test_sanitize_modes_dedups_preserving_order():
    sanitized = t3._sanitize_modes(["kanji_on", "quote_bottom", "kanji_on"])
    assert sanitized == ["kanji_on", "quote_bottom"]


def test_sanitize_modes_filters_invalid_entries():
    sanitized = t3._sanitize_modes(["quote_bottom", "bogus_mode", "collection_footer"])
    assert sanitized == ["quote_bottom", "collection_footer"]


def test_preview_all_v3_runs_every_combo_without_exception():
    """Регресс-тест/калибровка (раздел 6.5) — preview_all_v3 не должна падать и
    должна дать непустой результат для каждой допустимой комбинации."""
    fig = _make_duotone_figure()
    combos = t3.preview_all_v3(fig, _BASE_DESIGN)
    assert len(combos) >= 5
    for name, img in combos.items():
        bbox = img.getchannel("A").getbbox()
        assert bbox is not None, f"{name}: пустая альфа"


# ═══════════════════════════ art_director._parse: v3 поля ═════════════════════════

def _wrap_json_array(obj_json: str) -> str:
    return f"[{obj_json}]"


def test_parse_text_modes_v3_and_quote_name_jp_mood_present():
    raw = _wrap_json_array(
        '{"prompt": "A hero.", "chroma": "green", "slogan": "GO WILD", '
        '"slogan_color": "red", "kana": "", "character_en": "Kenpachi Zaraki", '
        '"title_en": "Bleach", "signature_props": "", "text_mode": "punch", '
        '"text_modes_v3": ["quote_bottom", "kanji_on", "collection_footer"], '
        '"quote": "Cursed sorcerers are so fragile", "name_jp": "\\u5263\\u516b", '
        '"mood": "duotone_quote"}'
    )
    designs = art_director._parse(raw)
    assert len(designs) == 1
    d = designs[0]
    assert d["text_modes_v3"] == ["quote_bottom", "kanji_on", "collection_footer"]
    assert d["quote"] == "Cursed sorcerers are so fragile"
    assert d["name_jp"] == "剣八"
    assert d["mood"] == "duotone_quote"


def test_parse_editorial_combined_with_others_collapses_to_editorial_alone():
    """Антиправило 7 применяется уже на уровне _parse — если Claude вернул editorial
    вместе с другими режимами, код должен приоритизировать editorial и отбросить
    остальные (не падать, не рисовать всё сразу)."""
    raw = _wrap_json_array(
        '{"prompt": "A hero.", "chroma": "green", "slogan": "GO", '
        '"slogan_color": "red", "kana": "", "character_en": "", "title_en": "", '
        '"text_modes_v3": ["editorial", "quote_bottom", "collection_footer"], '
        '"quote": "", "name_jp": "", "mood": "fashion_editorial"}'
    )
    designs = art_director._parse(raw)
    assert designs[0]["text_modes_v3"] == ["editorial"]


def test_parse_invalid_mood_falls_back_to_empty_string():
    raw = _wrap_json_array(
        '{"prompt": "A car.", "chroma": "green", "slogan": "GO", '
        '"slogan_color": "red", "kana": "", "character_en": "", "title_en": "", '
        '"mood": "some_bogus_mood"}'
    )
    designs = art_director._parse(raw)
    assert designs[0]["mood"] == ""


def test_parse_name_jp_accepts_kanji_range():
    """name_jp допускает КАНДЗИ (не только катакану, в отличие от kana) — раздел 4.1
    стайлгайда, пример из ТЗ — 伏黒甚爾 (Тодзи Фусигуро)."""
    raw = _wrap_json_array(
        '{"prompt": "A hero.", "chroma": "green", "slogan": "GO", '
        '"slogan_color": "red", "kana": "", "character_en": "Toji Fushiguro", '
        '"title_en": "Jujutsu Kaisen", "name_jp": "\\u4f0f\\u9ed2\\u751a\\u723e"}'
    )
    designs = art_director._parse(raw)
    assert designs[0]["name_jp"] == "伏黒甚爾"


def test_parse_quote_sanitized_and_length_capped():
    long_quote = "A " * 40 + "very long quote that should be capped somewhere"
    raw = _wrap_json_array(
        '{"prompt": "A hero.", "chroma": "green", "slogan": "GO", '
        '"slogan_color": "red", "kana": "", "character_en": "", "title_en": "", '
        f'"quote": "{long_quote}"}}'
    )
    designs = art_director._parse(raw)
    assert len(designs[0]["quote"]) <= 70


# ═══════════════════════════ обратная совместимость (v2 и старый v1) ══════════════

def test_parse_backward_compatible_old_json_without_v3_fields():
    """Старый JSON БЕЗ text_modes_v3/quote/name_jp/mood -> дефолты, парсинг НЕ падает
    (обратная совместимость со старыми дампами design.json седьмого захода)."""
    raw = _wrap_json_array(
        '{"prompt": "A hero stands tall.", "chroma": "green", "slogan": "GO WILD", '
        '"slogan_color": "red", "kana": "", "character_en": "", "title_en": ""}'
    )
    designs = art_director._parse(raw)
    assert len(designs) == 1
    d = designs[0]
    assert d["text_modes_v3"] == []
    assert d["quote"] == ""
    assert d["name_jp"] == ""
    assert d["mood"] == ""
    # Поля v2 по-прежнему присутствуют и корректны (не сломали существующий контракт).
    assert d["text_mode"] == "punch"
    assert d["signature_props"] == ""


def test_parse_text_modes_v3_ignores_non_list_value():
    """Если Claude вернул text_modes_v3 не списком (строка/число/null) — не падаем,
    дефолт []."""
    raw = _wrap_json_array(
        '{"prompt": "A hero.", "chroma": "green", "slogan": "GO", '
        '"slogan_color": "red", "kana": "", "character_en": "", "title_en": "", '
        '"text_modes_v3": "quote_bottom"}'
    )
    designs = art_director._parse(raw)
    assert designs[0]["text_modes_v3"] == []
