# -*- coding: utf-8 -*-
"""typography_v3.py — типографика v3: текст ИЗ ЦВЕТОВ ИЛЛЮСТРАЦИИ, дизайнерские
композиционные режимы (docs/PRINT_STYLE_GUIDE.md).

Не переписывает typography.py (v1/v2 остаются как есть, см. TEXT_MODES/compose_text
там) — новый файл рядом, импортирует низкоуровневые примитивы (_font, _measure,
_draw_spaced, _hard_shadow_text, _alpha_bbox, _crop_to_content, _fit_lines_min_size)
из typography.py напрямую (раздел 6.1 стайлгайда: дублировать их не нужно).

Публичная точка входа: compose_text_v3(figure_rgba, modes, design, brand_label).

Три пробела v2, которые закрывает v3 (раздел 0 стайлгайда):
1. Цвет текста — ТОЛЬКО из palette.extract_palette конкретной иллюстрации, никогда
   typography._TITLE_COLORS.
2. Минимум 2-3 шрифтовые роли на принт вместо одной (quote/display/gothic/caps/jp).
3. Структурный подвал-этикетка (collection_footer) и цитата в кавычках (quote_bottom)
   как отдельные графические блоки.
"""
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import palette
import typography as _typo

FONTS_DIR = _typo.FONTS_DIR

# Шрифтовые роли v3 (раздел 2 стайлгайда) — файлы уже скачаны в fonts/, см. fonts/FONTS.md.
_FONT_FILES_V3 = {
    "quote": ["PermanentMarker-Regular.ttf"],
    "quote_alt": ["CaveatBrush-Regular.ttf"],
    "display": ["PlayfairDisplay[wght].ttf"],
    "gothic": ["Cinzel[wght].ttf"],
    "gothic_heavy": ["UnifrakturCook-Bold.ttf"],
}
# Оси variable-шрифтов, которые нужно зафиксировать на Black/900 (тот же приём, что
# уже есть в typography._font для notojp) — иначе variable-файл рендерится в Thin.
_VARIABLE_BLACK = {"display": "PlayfairDisplay", "gothic": "Cinzel"}

_font_cache_v3 = {}

# Композиционные режимы v3 (раздел 3).
TEXT_MODES_V3 = ("quote_bottom", "kanji_on", "editorial", "collection_footer")

# Фиксированный порядок применения при комбинации режимов (раздел 3, не зависит от
# порядка на входе): kanji_on -> quote_bottom -> collection_footer. editorial всегда
# соло (обрабатывается отдельной веткой в compose_text_v3).
_APPLY_ORDER = ("kanji_on", "quote_bottom", "collection_footer")

_MIN_FONT_FRAC_QUOTE = 0.05
_CROP_MARGIN_FRAC = _typo._CROP_MARGIN_FRAC


def _font_v3(role: str, size: int) -> "ImageFont.FreeTypeFont":
    """Загрузить шрифт по роли v3 с кэшем; фиксирует ось Weight=900 (Black) для
    variable-файлов display/gothic (см. fonts/FONTS.md — Cinzel/PlayfairDisplay в
    google/fonts доступны только как variable font, статических Black-файлов нет)."""
    cache_key = (role, size)
    if cache_key in _font_cache_v3:
        return _font_cache_v3[cache_key]

    candidates = [str(FONTS_DIR / fn) for fn in _FONT_FILES_V3.get(role, [])]
    candidates += _typo._FALLBACK_CHAIN

    font = None
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            marker = _VARIABLE_BLACK.get(role)
            if marker and marker in path:
                try:
                    font.set_variation_by_axes([900])
                except Exception:  # noqa: BLE001 — не variable/нет оси, ок
                    pass
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache_v3[cache_key] = font
    return font


def _fit_font_v3(draw: "ImageDraw.ImageDraw", text: str, role: str, size: int,
                  max_width: float, spacing: int = 0) -> tuple:
    """Аналог typography._fit_font, но для шрифтов v3 (своя загрузка _font_v3)."""
    font = _font_v3(role, size)
    tw = _typo._measure(draw, text, font, spacing)
    if tw > max_width and tw > 0:
        size = max(10, int(size * max_width / tw))
        font = _font_v3(role, size)
        tw = _typo._measure(draw, text, font, spacing)
    return font, tw


def _measure_piece(draw: "ImageDraw.ImageDraw", piece: str, font, spacing: int = 0) -> int:
    """Ширина куска текста (слово + опциональный завершающий пробел) — курсор для
    ПОЗИЦИОНИРОВАНИЯ следующего куска, который рисуется через typography._draw_spaced.

    ВАЖНО: должна использовать РОВНО ТУ ЖЕ формулу, что _draw_spaced использует для
    продвижения курсора при отрисовке (сумма individual-glyph bbox-width на каждый
    символ + spacing), а НЕ draw.textlength() всей строки целиком. На курсивных/
    маркерных шрифтах (PermanentMarker/CaveatBrush) посимвольная сумма bbox-width
    ЗАМЕТНО ШИРЕ, чем textlength() всей строки разом (PIL применяет кернинг между
    соседними глифами при измерении строки целиком, а _draw_spaced рисует и меряет
    каждый символ независимо, без кернинга) — при использовании textlength() для
    курсора следующее слово рисовалось ПОВЕРХ хвоста предыдущего (пойман визуально:
    "CURSED SORCERERS" слипались в "CURSEDSORCERERS" при курсорном расхождении ~25px
    на 300px слове). Формула продублирована из typography._draw_spaced построчно —
    не вызывать _typo._measure(text, spacing=0) для НЕСКОЛЬКИХ символов подряд."""
    total = 0
    for ch in piece:
        bbox = draw.textbbox((0, 0), ch, font=font)
        total += (bbox[2] - bbox[0]) + spacing
    return max(0, total - spacing) if spacing else total


def _mood_font_pair(mood: str) -> dict:
    """Шрифтовая пара под mood (раздел 2/4.2 стайлгайда). Возвращает dict с ключами
    quote/caps/jp/display/gothic — какие из них реально нужны, решает вызывающий код
    по модам, применённым в конкретном принте."""
    if mood == "fashion_editorial":
        return {"display": "display", "jp": "notojp"}
    if mood == "pop_trash":
        return {"gothic": "gothic", "jp": "notojp"}
    # duotone_quote и дефолт
    return {"quote": "quote", "caps": "anton", "jp": "notojp"}


# ── quote_bottom ────────────────────────────────────────────────────────────────

def _split_quote(draw, quote: str, font_role: str, start_size: int, max_width: float,
                  min_size: int) -> tuple:
    """Уложить цитату в 1 строку, либо в 2 строки балансом 55/45 (раздел 3.1) — если
    даже на min_size одна строка не влезает. Кегль второй строки НЕ уменьшается
    относительно первой (обе строки — один размер)."""
    font, tw = _fit_font_v3(draw, quote, font_role, start_size, max_width)
    if tw > 0 and font.size >= min_size:
        return [quote], font

    words = quote.split()
    if len(words) < 2:
        font = _font_v3(font_role, min_size)
        return [quote], font

    split_at = max(1, round(len(words) * 0.55))
    line1 = " ".join(words[:split_at])
    line2 = " ".join(words[split_at:])
    font_min = _font_v3(font_role, min_size)
    return [line1, line2], font_min


def _compose_quote_bottom(canvas: Image.Image, fx0: int, fy0: int, fx1: int, fy1: int,
                           fig_w: int, fig_h: int, quote: str, roles: "palette.PaletteRoles",
                           font_role: str) -> Image.Image:
    """Раздел 3.1 — цитата в кавычках под фигурой, чередование слов accent/dominant,
    кавычки — цвет первого слова, обводка единая dark/light по контрасту."""
    quote = quote.strip()
    if not quote:
        return canvas

    draw = ImageDraw.Draw(canvas)
    max_width = fig_w * 0.92
    start_size = int(fig_w * 0.10)
    min_size = max(1, int(fig_w * _MIN_FONT_FRAC_QUOTE))

    lines, font = _split_quote(draw, quote, font_role, start_size, max_width, min_size)

    # Область под фигурой для локальной яркости — полоса bottom_margin ниже bbox.
    bottom_margin = int(canvas.height * 0.04)
    probe_h = max(10, int(fig_h * 0.12))
    local_lum = palette.local_luminance(
        canvas, (fx0, fy1, fx1, min(canvas.height, fy1 + probe_h)))
    _fill_unused, stroke_color = palette.contrast_fill_stroke(roles, local_lum)

    line_height = font.size
    total_h = int(line_height * (1 + 0.15 * (len(lines) - 1)) * len(lines))
    y_top = fy1 + bottom_margin

    y_cursor = y_top
    for li, line in enumerate(lines):
        words = line.split()
        word_colors = palette.alternate_word_colors(words, roles)
        quote_text = f"“{line}”"
        tw_full = _typo._measure(draw, quote_text, font)
        x_cursor = int(fx0 + fig_w / 2 - tw_full / 2)
        stroke_w = max(1, int(font.size * 0.04))

        # Открывающая кавычка — цвет первого слова.
        first_color = word_colors[0] if word_colors else roles.dominant
        quote_font = _font_v3(font_role, int(font.size * 1.15))
        open_q = "“"
        _typo._draw_spaced(canvas, (x_cursor, y_cursor), open_q, quote_font,
                            first_color, 0, stroke_width=stroke_w,
                            stroke_fill=stroke_color)
        qw = _typo._measure(draw, open_q, quote_font)
        x_cursor += qw

        for wi, word in enumerate(words):
            color = word_colors[wi]
            # Точка в конце последней строки/слова — цвет последнего слова (уже
            # часть строки `line`, поэтому просто рисуем слово как есть).
            piece = word + (" " if wi < len(words) - 1 else "")
            _typo._draw_spaced(canvas, (x_cursor, y_cursor), piece, font, color, 0,
                                stroke_width=stroke_w, stroke_fill=stroke_color)
            x_cursor += _measure_piece(draw, piece, font)

        last_color = word_colors[-1] if word_colors else roles.dominant
        close_q = "”"
        _typo._draw_spaced(canvas, (x_cursor, y_cursor), close_q, quote_font,
                            last_color, 0, stroke_width=stroke_w,
                            stroke_fill=stroke_color)

        y_cursor += int(line_height * 1.15)

    return canvas


# ── kanji_on ────────────────────────────────────────────────────────────────────

def _compose_kanji_on(canvas: Image.Image, fx0: int, fy0: int, fig_w: int, fig_h: int,
                       name_jp: str, roles: "palette.PaletteRoles",
                       ghost: bool = False) -> Image.Image:
    """Раздел 3.2 — вертикальная колонка кандзи/катаканы НА фигуре (перекрытие почти
    полное, в отличие от v2 kana_side). ghost=True — вариант 3.2а (полупрозрачная,
    без обводки, для pop_trash mood)."""
    name_jp = name_jp.strip()
    if not name_jp:
        return canvas

    draw = ImageDraw.Draw(canvas)
    min_size = max(1, int(fig_w * 0.08))
    kegl = max(min_size, int(fig_w * 0.16))
    max_col_h = fig_h * 0.55

    font = _typo._font("notojp", kegl)

    def _dims(f):
        widths, heights = [], []
        for ch in name_jp:
            b = draw.textbbox((0, 0), ch, font=f)
            widths.append(b[2] - b[0])
            heights.append(b[3] - b[1])
        return max(widths, default=1), (max(heights, default=1) if heights else 1)

    col_w, glyph_h = _dims(font)
    total_h = glyph_h * len(name_jp) * 1.05
    if total_h > max_col_h:
        scale = max_col_h / total_h
        kegl = max(min_size, int(kegl * scale))
        font = _typo._font("notojp", kegl)
        col_w, glyph_h = _dims(font)
        total_h = glyph_h * len(name_jp) * 1.05

    cx = fx0 + fig_w * 0.5
    cy = fy0 + fig_h * 0.55
    y = cy - total_h / 2

    if ghost:
        fill_color = roles.accent
        alpha = 140
        stroke_width = 0
        stroke_color = None
    else:
        fill_color = roles.light
        alpha = 242
        stroke_width = max(1, int(kegl * 0.09))
        stroke_color = roles.dark

    fill_rgba = fill_color + (alpha,)
    stroke_rgba = (stroke_color + (alpha,)) if stroke_color else None

    for ch in name_jp:
        bbox = draw.textbbox((0, 0), ch, font=font)
        ch_w = bbox[2] - bbox[0]
        x = int(cx - ch_w / 2) - bbox[0]
        if stroke_width > 0:
            draw.text((x, int(y) - bbox[1]), ch, font=font, fill=fill_rgba,
                       stroke_width=stroke_width, stroke_fill=stroke_rgba)
        else:
            draw.text((x, int(y) - bbox[1]), ch, font=font, fill=fill_rgba)
        y += glyph_h * 1.05

    return canvas


# ── editorial ─────────────────────────────────────────────────────────────────

def _compose_editorial(figure_rgba: Image.Image, design: dict,
                        roles: "palette.PaletteRoles") -> Image.Image:
    """Раздел 3.3 — display-заголовок, переплетённый с фигурой по глубине. Соло-режим
    (не комбинируется). Упрощённая v3.0-реализация переплетения (см. TODO ниже) —
    полная двухпроходная маска по X-половинам заявлена как допустимый упрощённый
    fallback стайлгайдом при избыточной сложности."""
    fx0, fy0, fx1, fy1 = _typo._alpha_bbox(figure_rgba)
    fig_w, fig_h = fx1 - fx0, fy1 - fy0

    character_en = str(design.get("character_en") or "").strip().upper()
    if not character_en:
        return figure_rgba.copy()

    pad = int(fig_w * 0.4)
    W, H = figure_rgba.width + 2 * pad, figure_rgba.height + 2 * pad

    under_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(under_layer)

    start_size = int(fig_w * 0.30)
    min_size = max(1, int(fig_w * 0.14))
    max_width = fig_w * 1.05
    font, tw = _fit_font_v3(draw, character_en, "display", start_size, max_width)
    if font.size < min_size:
        font = _font_v3("display", min_size)
        tw = _typo._measure(draw, character_en, font)

    bbox = draw.textbbox((0, 0), character_en, font=font)
    text_h = bbox[3] - bbox[1]
    cx = pad + fx0 + fig_w / 2
    baseline_y = pad + fy0 + fig_h * 0.30
    x = int(cx - tw / 2) - bbox[0]
    y = int(baseline_y - text_h / 2) - bbox[1]

    # Заливка dark, БЕЗ обводки (раздел 3.3/3.5).
    draw.text((x, y), character_en, font=font, fill=roles.dark)

    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.alpha_composite(under_layer)
    out.alpha_composite(figure_rgba.convert("RGBA"), (pad, pad))

    # v3.0-упрощение переплетения: дублируем последние 2-3 символа НАД фигурой на их
    # исходной позиции (полная двухпроходная X-маска — TODO v3.1, см. раздел 3.3
    # стайлгайда, допущен как упрощённый fallback).
    tail = character_en.split()[-1] if character_en.split() else character_en
    tail = tail[-3:] if len(tail) >= 3 else tail
    if tail:
        tail_w = _typo._measure(draw, tail, font)
        # Правый визуальный край заголовка (x уже компенсирует -bbox[0], см. выше) -
        # ширина хвоста = позиция начала хвоста, чтобы он лёг РОВНО поверх своего
        # исходного вхождения в слове, не смещаясь.
        tail_x = int(x + bbox[0] + tw - tail_w)
        over_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(over_layer)
        odraw.text((tail_x, y), tail, font=font, fill=roles.dark)
        out.alpha_composite(over_layer)

    # Подзаголовок катакана/кандзи транслитерации title_en (name_jp персонажа НЕ то же
    # самое, что title_jp — стайлгайд просит транслитерацию title_en; если её нет,
    # используем name_jp персонажа как приемлемая замена смыслового японского элемента,
    # раз отдельного поля title_jp в схеме art_director нет).
    name_jp = str(design.get("name_jp") or design.get("kana") or "").strip()
    if name_jp:
        sub_size = max(1, int(fig_w * 0.045))
        sub_font = _typo._font("notojp", sub_size)
        sdraw = ImageDraw.Draw(out)
        sub_bbox = sdraw.textbbox((0, 0), name_jp, font=sub_font)
        sub_w = sub_bbox[2] - sub_bbox[0]
        sub_line_h = bbox[3] - bbox[1]
        right_edge = x + bbox[0] + tw
        sub_x = int(right_edge - sub_w)
        sub_y = int(y + bbox[1] + sub_line_h * 1.1) - sub_bbox[1]
        sub_color = roles.dominant if roles.dominant != roles.dark else roles.accent
        sdraw.text((sub_x, sub_y), name_jp, font=sub_font, fill=sub_color)

    # Мини-лого "ANICLOT" антиквой над заголовком.
    brand_word = "Aniclot"
    logo_size = max(1, int(fig_w * 0.035))
    logo_font = _font_v3("display", logo_size)
    ldraw = ImageDraw.Draw(out)
    logo_bbox = ldraw.textbbox((0, 0), brand_word, font=logo_font)
    logo_w = logo_bbox[2] - logo_bbox[0]
    logo_line_h = bbox[3] - bbox[1]
    logo_x = int(cx - logo_w / 2) - logo_bbox[0]
    logo_y = int(y + bbox[1] - logo_line_h * 1.3) - logo_bbox[1]
    ldraw.text((logo_x, logo_y), brand_word, font=logo_font, fill=roles.dominant)

    return out


# ── collection_footer ──────────────────────────────────────────────────────────

def _footer_font_role(mood: str) -> str:
    return "gothic" if mood == "pop_trash" else "caps"


def _compose_collection_footer(canvas: Image.Image, fx0: int, fy0: int, fx1: int,
                                content_bottom_y: int, fig_w: int,
                                brand_label: str, title_en: str, character_en: str,
                                roles: "palette.PaletteRoles", mood: str,
                                extra_quote_line: str = "") -> Image.Image:
    """Раздел 3.4 — три капс-блока BRAND | TITLE | CHARACTER, чередование по словам
    внутри блока (первое dominant/второе accent), по номеру блока для одиночных слов.
    extra_quote_line — раздел 4.3 (pop_trash): цитата как четвёртая строка подвала."""
    draw = ImageDraw.Draw(canvas)
    font_role = _footer_font_role(mood)

    blocks = [brand_label.strip().upper()]
    if title_en.strip():
        blocks.append(title_en.strip().upper())
    if character_en.strip():
        blocks.append(character_en.strip().upper())
    blocks = [b for b in blocks if b]
    if not blocks:
        return canvas

    size = max(1, int(fig_w * 0.028))
    min_size = max(1, int(fig_w * 0.018))
    spacing = max(0, int(fig_w * 0.004))
    # Раздел 3.4: разделитель " | ". Одинарный пробел ПЕРЕД/ПОСЛЕ пайпа визуально
    # сливается с соседним словом на мелком подвальном кегле (advance width пробела
    # у caps/gothic шрифтов ~27% от кегля — на печати читается как слипание типа
    # "COLLECTION|NARUTO"); двойной пробел даёт различимый зазор без изменения формата.
    sep = "  |  "
    # Аналогичная проблема ВНУТРИ многословных блоков ("ITACHI UCHIHA" читалось почти
    # слитно на мелком подвальном кегле — letter-spacing между буквами визуально
    # сопоставим с word-gap одинарного пробела) — используем тот же приём word-gap.
    word_gap = "  "

    def _line_width(sz, sp):
        font = _font_v3(font_role, sz)
        total = 0
        for bi, block in enumerate(blocks):
            words = block.split()
            for wi, word in enumerate(words):
                total += _measure_piece(draw, word, font, sp)
                if wi < len(words) - 1:
                    total += _measure_piece(draw, word_gap, font)
            if bi < len(blocks) - 1:
                # sep = "  |  " начинается/заканчивается пробелами — _measure_piece
                # (не _typo._measure/textbbox) не обрезает крайние пробелы.
                total += _measure_piece(draw, sep, font)
        return total

    max_w = fig_w * 0.95
    tw = _line_width(size, spacing)
    if tw > max_w:
        spacing = 0
        tw = _line_width(size, spacing)
    if tw > max_w and tw > 0:
        size = max(min_size, int(size * max_w / tw))
        tw = _line_width(size, spacing)

    font = _font_v3(font_role, size)
    local_lum = palette.local_luminance(
        canvas, (fx0, content_bottom_y, fx1, min(canvas.height, content_bottom_y + size * 3)))
    sep_fill, sep_stroke = palette.contrast_fill_stroke(roles, local_lum)
    stroke_w = max(1, int(size * 0.03))

    x_cursor = int(fx0 + fig_w / 2 - tw / 2)
    y = content_bottom_y

    block_colors_list = palette.block_colors(len(blocks), roles)
    for bi, block in enumerate(blocks):
        words = block.split()
        if len(words) <= 1:
            colors = [block_colors_list[bi]]
        else:
            colors = palette.alternate_word_colors(words, roles)
        for wi, word in enumerate(words):
            _typo._draw_spaced(canvas, (x_cursor, y), word, font, colors[wi], spacing,
                                stroke_width=stroke_w, stroke_fill=sep_stroke)
            x_cursor += _measure_piece(draw, word, font, spacing)
            if wi < len(words) - 1:
                # word_gap (двойной пробел) — иначе word-разрыв визуально теряется
                # среди letter-spacing на мелком подвальном кегле (см. комментарий
                # у word_gap выше).
                x_cursor += _measure_piece(draw, word_gap, font)
        if bi < len(blocks) - 1:
            _typo._draw_spaced(canvas, (x_cursor, y), sep, font, sep_fill, 0,
                                stroke_width=stroke_w, stroke_fill=sep_stroke)
            x_cursor += _measure_piece(draw, sep, font)

    y_after = y + int(size * 1.25)

    if extra_quote_line.strip():
        # Раздел 4.3: цитата как 4-я строка подвала, шрифт gothic, кегль 0.032 fig_w
        # (крупнее строк подвала 0.028, мельче самостоятельного quote_bottom 0.10).
        q_size = max(min_size, int(fig_w * 0.032))
        q_font = _font_v3("gothic", q_size)
        q_text = f"“{extra_quote_line.strip()}”"
        q_tw = _typo._measure(draw, q_text, q_font)
        q_max_w = fig_w * 0.95
        if q_tw > q_max_w and q_tw > 0:
            q_size = max(min_size, int(q_size * q_max_w / q_tw))
            q_font = _font_v3("gothic", q_size)
            q_tw = _typo._measure(draw, q_text, q_font)
        q_x = int(fx0 + fig_w / 2 - q_tw / 2)
        _typo._draw_spaced(canvas, (q_x, y_after), q_text, q_font, roles.dominant, 0,
                            stroke_width=max(1, int(q_size * 0.03)),
                            stroke_fill=sep_stroke)

    return canvas


# ── Оркестрация ────────────────────────────────────────────────────────────────

def _sanitize_modes(modes: list) -> list:
    """Защитный код (антиправило 7 стайлгайда): editorial приоритизируется и
    отбрасывает остальные режимы, если пришла невалидная комбинация."""
    modes = [m for m in (modes or []) if m in TEXT_MODES_V3]
    if "editorial" in modes:
        return ["editorial"]
    # Уникализируем, сохраняя порядок появления.
    seen = set()
    out = []
    for m in modes:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def compose_text_v3(figure_rgba: Image.Image, modes: list, design: dict,
                     brand_label: str = "ANICLOT COLLECTION") -> Image.Image:
    """Точка входа типографики v3 (раздел 3/6.1 стайлгайда). figure_rgba — прозрачная
    вырезка; modes — список из TEXT_MODES_V3 (может комбинироваться, кроме editorial);
    design — dict из art_director.make_ideas (включая v3-поля quote/name_jp/mood/
    text_modes_v3); brand_label — конфиг-константа (config.BRAND_LABEL).

    Возвращает RGBA, кропнутый до контента (текст+фигура) с полями."""
    rgba = figure_rgba.convert("RGBA")
    modes = _sanitize_modes(modes)
    if not modes:
        return rgba.copy()

    figure_palette = palette.extract_palette(rgba, n=4)
    roles = palette.PaletteRoles(figure_palette)
    mood = str(design.get("mood") or "").strip().lower()

    if modes == ["editorial"]:
        composed = _compose_editorial(rgba, design, roles)
        return _typo._crop_to_content(composed)

    fx0, fy0, fx1, fy1 = _typo._alpha_bbox(rgba)
    fig_w, fig_h = fx1 - fx0, fy1 - fy0

    quote = str(design.get("quote") or "").strip()
    name_jp = str(design.get("name_jp") or design.get("kana") or "").strip()
    title_en = str(design.get("title_en") or "").strip()
    character_en = str(design.get("character_en") or "").strip()

    # Холст с запасом (цитата может расширить высоту вниз, kanji_on рисуется прямо в
    # bbox — запас снизу с большим отступом).
    pad_bottom = int(fig_h * 0.35)
    pad_side = int(fig_w * 0.1)
    W = rgba.width + 2 * pad_side
    H = rgba.height + pad_bottom
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    fx0c, fy0c, fx1c, fy1c = fx0 + pad_side, fy0, fx1 + pad_side, fy1
    fig_origin = (pad_side, 0)

    quote_bottom_active = "quote_bottom" in modes
    kanji_on_active = "kanji_on" in modes
    footer_active = "collection_footer" in modes

    ghost = mood == "pop_trash"
    font_role_map = _mood_font_pair(mood)
    quote_role = font_role_map.get("quote", "quote")

    # kanji_on: раздел 6.4 — порядок фигура -> кандзи ПОВЕРХ неё (обратный порядок
    # относительно editorial, допустим единственный проход).
    if kanji_on_active:
        canvas.alpha_composite(rgba, fig_origin)
        canvas = _compose_kanji_on(canvas, fx0c, fy0c, fig_w, fig_h, name_jp, roles,
                                    ghost=ghost)
    else:
        canvas.alpha_composite(rgba, fig_origin)

    content_bottom_y = fy1c

    # pop_trash с цитатой, но БЕЗ отдельного quote_bottom (правило 4.3): цитата уходит
    # внутрь подвала как 4-я строка, а не отдельным блоком.
    footer_quote_line = ""
    if quote_bottom_active and quote:
        canvas = _compose_quote_bottom(canvas, fx0c, fy0c, fx1c, fy1c, fig_w, fig_h,
                                        quote, roles, quote_role)
        # Пересчитать нижнюю границу контента после цитаты для позиционирования
        # подвала (footer должен идти НИЖЕ цитаты, раздел 3.4).
        alpha_bbox_now = _typo._alpha_bbox(canvas)
        content_bottom_y = alpha_bbox_now[3]
    elif mood == "pop_trash" and quote and not quote_bottom_active:
        footer_quote_line = quote

    if footer_active:
        footer_y = content_bottom_y + int(canvas.height * 0.03) if (
            quote_bottom_active and quote) else fy1c + int(fig_h * 0.06)
        canvas = _compose_collection_footer(
            canvas, fx0c, fy0c, fx1c, footer_y, fig_w, brand_label, title_en,
            character_en, roles, mood, extra_quote_line=footer_quote_line)

    return _typo._crop_to_content(canvas)


def preview_all_v3(rgba: Image.Image, design: dict,
                    brand_label: str = "ANICLOT COLLECTION") -> dict:
    """Регресс-тест/калибровка (раздел 6.5) — прогоняет все допустимые комбинации
    режимов v3 из раздела 3.6 на одном диекате. Возвращает dict combo_name -> Image."""
    combos = {
        "quote_bottom": ["quote_bottom"],
        "kanji_on": ["kanji_on"],
        "collection_footer": ["collection_footer"],
        "quote_bottom+kanji_on+collection_footer": [
            "quote_bottom", "kanji_on", "collection_footer"],
        "quote_bottom+collection_footer": ["quote_bottom", "collection_footer"],
        "kanji_on_ghost+collection_footer": ["kanji_on", "collection_footer"],
        "editorial": ["editorial"],
    }
    out = {}
    for name, modes in combos.items():
        d = dict(design)
        if name == "kanji_on_ghost+collection_footer":
            d["mood"] = "pop_trash"
        out[name] = compose_text_v3(rgba, modes, d, brand_label)
    return out


# ── ring_text — медальон-гибрид: кольцевой текст КОДОМ вокруг фигуры ────────────
#
# nano-banana просят рисовать ПУСТОЕ декоративное кольцо (без букв) вокруг фигуры
# (docs/STYLE_BANK.json "09_ring_medallion", hybrid_ring_text=true — промпт-часть
# строит art_director.build_prompt/_style_bank_prompt_block, см. batch_print.
# render_design/_is_hybrid_ring_style) — после вырезки ring_text кладёт САМ ТЕКСТ
# поверх готового кольца посимвольным поворотом по дуге (PIL), кегль/радиус от bbox
# фигуры, цвета из палитры иллюстрации (palette.extract_palette, тот же источник
# цвета, что остальная типографика v3 — раздел 0 стайлгайда). Гибрид, потому что
# декоративное кольцо-рамка рисуется генерацией (nanobanana хорошо держит орнамент/
# металлик), а ТОЧНЫЙ СПЕЛЛИНГ текста — код (то, что генерация путает на кривой дуге).

_RING_FONT_ROLE = "gothic"  # Cinzel — конденсированные условные капсы, ровный штрих
# Минимальный кегль как доля ширины фигуры — 0.058, докс/STYLE_BANK.json
# "09_ring_medallion".constraints ("мин. кегль кода-типографики по кольцу 0.058").
_RING_MIN_FONT_FRAC = 0.058


def _detect_ring_radius(rgba: Image.Image, cx_local: float, cy_local: float,
                        fallback_radius: float) -> float:
    """Находит фактический радиус НАРИСОВАННОГО декоративного кольца по радиальному
    угловому профилю альфа-канала (регресс-фикс — см. tests/test_ring_text_regression_
    qa.py). cx_local/cy_local — центр в координатах САМОЙ rgba (без canvas_pad).

    Идея: декоративное кольцо (STYLE_BANK.json "09_ring_medallion") — тонкая ПОЧТИ
    ЗАМКНУТАЯ окружность, покрывающая большую часть из 360 угловых градусов на своём
    радиусе. Аура/молнии/спецэффекты, торчащие ЗА пределы кольца ("crackling neon aura
    energy pulses... streaking upward" — штатная часть essence этого стиля), — узкие
    лучи в НЕСКОЛЬКИХ направлениях, на любом отдельном радиусе они покрывают лишь малую
    долю из 360°. Поэтому для каждого радиального бина считаем долю занятых угловых
    градусов (не площадь/долю пикселей) — у кольца эта доля резко выше, чем у ауры на
    любом её радиусе, что даёт чёткий пик ровно на радиусе кольца, даже если аура
    физически покрывает больше суммарной площади/тянется дальше от центра.

    Возвращает fallback_radius (старая формула — половина диагонали bbox * 1.08), если
    ни один радиальный бин не даёт заметно кольцеобразного покрытия (например обычная
    фигура без нарисованного кольца) — не ломает дизайны без style_id=09_ring_medallion."""
    alpha = np.array(rgba.getchannel("A"))
    h, w = alpha.shape
    ys, xs = np.nonzero(alpha > 40)
    if xs.size == 0:
        return fallback_radius

    max_r = int(min(fallback_radius * 1.6, (max(w, h)) * 0.75))
    if max_r < 4:
        return fallback_radius

    dx = xs.astype(np.float64) - cx_local
    dy = ys.astype(np.float64) - cy_local
    r = np.sqrt(dx * dx + dy * dy)
    theta_deg = (np.degrees(np.arctan2(dy, dx)) + 360.0) % 360.0

    bin_w = 2.0  # px на радиальный бин
    n_bins = max(1, int(max_r / bin_w))
    # Порог 0.55 (кольцо занимает БОЛЬШУЮ часть окружности, даже с зазубринами/швом на
    # верхней точке) — заметно выше того, что могут дать несколько лучей ауры, даже
    # раздув их ширину; см. синтетику в регресс-тесте (8 лучей по 45° друг от друга
    # покрывают лишь малую долю угла на своём радиусе, далеко ниже 0.55).
    _COVERAGE_THRESHOLD = 0.55
    n_angle_bins = 180  # 2-градусные секторы — устойчивее к дырам от анти-алиасинга
    ring_bins = []
    for b in range(n_bins):
        r_lo, r_hi = b * bin_w, (b + 1) * bin_w
        sel = (r >= r_lo) & (r < r_hi)
        cnt = int(sel.sum())
        if cnt < 8:
            continue
        angle_bins = np.unique((theta_deg[sel] / (360.0 / n_angle_bins)).astype(int))
        coverage = angle_bins.size / n_angle_bins
        if coverage >= _COVERAGE_THRESHOLD:
            ring_bins.append(b)

    if not ring_bins:
        return fallback_radius

    # Кольцо — САМАЯ ВНЕШНЯЯ сплошная структура композиции (декоративная рамка вокруг
    # фигуры), отделённая от основной массы фигуры ВИДИМЫМ РАЗРЫВОМ (хромакей-полем)
    # — так реально рисует nano-banana для style_id=09_ring_medallion (кольцо на
    # "равном радиальном расстоянии вокруг всей фигуры", essence STYLE_BANK.json).
    # Находим последнюю непрерывную группу "кольцевых" бинов и смотрим, отделена ли
    # она разрывом от центра/предыдущей структуры.
    last_group_start = ring_bins[-1]
    i = len(ring_bins) - 1
    while i > 0 and ring_bins[i - 1] == ring_bins[i] - 1:
        last_group_start = ring_bins[i - 1]
        i -= 1
    last_group_end = ring_bins[-1]
    thickness = (last_group_end - last_group_start + 1) * bin_w
    detected_edge = (last_group_end + 1.0) * bin_w

    gap_before = ring_bins[i - 1] if i > 0 else -1  # последний "кольцевой" бин ДО группы
    gap_bins = last_group_start - gap_before - 1 if gap_before >= 0 else last_group_start
    has_visible_gap = gap_bins * bin_w >= max(20.0, thickness)

    if not has_visible_gap:
        # Нет разрыва перед последней группой (типично: обычная сплошная фигура/силуэт
        # БЕЗ отдельно нарисованного декоративного кольца, группа тянется почти от
        # центра фигуры до её края) — используем ту же формулу, что раньше давала
        # корректный результат для этого случая (половина диагонали bbox * 1.08).
        return fallback_radius

    # Реальное отдельное кольцо, отделённое разрывом от фигуры — небольшой запас за
    # фактический внешний край кольца (буквы ложатся чуть снаружи, не впритык на
    # самый крайний пиксель контура).
    return detected_edge * 1.10


def _ring_radius_and_center(rgba: Image.Image, fx0: int, fy0: int, fx1: int, fy1: int,
                            canvas_pad: int) -> tuple:
    """Центр и радиус кольца. Центр = центр bbox альфы фигуры (устойчив — фигура всегда
    в середине композиции). Радиус — сначала пытаемся определить ПО ФАКТИЧЕСКИ
    нарисованному кольцу (_detect_ring_radius, радиальный угловой профиль — не путает
    кольцо с аурой/молниями, торчащими за его пределы). Если кольцо не обнаружено
    (design без style_id=09_ring_medallion, обычная фигура) — старая формула: половина
    диагонали bbox * 1.08 (небольшой запас за пределы силуэта)."""
    fig_w, fig_h = fx1 - fx0, fy1 - fy0
    cx = canvas_pad + fx0 + fig_w / 2.0
    cy = canvas_pad + fy0 + fig_h / 2.0
    fallback_radius = 0.5 * (fig_w ** 2 + fig_h ** 2) ** 0.5 * 1.08
    radius = _detect_ring_radius(rgba, cx - canvas_pad, cy - canvas_pad, fallback_radius)
    return cx, cy, radius


def ring_text(rgba: Image.Image, phrase: str, roles: "palette.PaletteRoles" = None,
              font_role: str = _RING_FONT_ROLE, start_angle_deg: float = -90.0,
              clockwise: bool = True) -> Image.Image:
    """Кольцевой текст ВОКРУГ фигуры, кодом (PIL, посимвольный поворот по дуге) —
    точка входа медальон-гибрида (см. блок-комментарий выше).

    rgba: PIL.Image RGBA — прозрачная вырезка (фигура + ПУСТОЕ декоративное кольцо,
    нарисованное генерацией, без букв). phrase: строка без пробелов на концах — КАЖДЫЙ
    символ (включая пробелы между словами) кладётся РОВНО ОДИН РАЗ по окружности,
    равномерным угловым шагом 360°/len(phrase) (полный проход по кольцу, не дуга).
    roles: palette.PaletteRoles — если не передан, извлекается из rgba автоматически
    (palette.extract_palette). font_role: роль шрифта typography_v3 (дефолт "gothic" —
    Cinzel, конденсированные капсы с ровным штрихом, стабильно читаемые под поворотом).
    start_angle_deg: угол первого символа (дефолт -90° = "12 часов", верх кольца).
    clockwise: направление обхода (дефолт True — по часовой стрелке, как на монете).

    Кегль и радиус — ОТ BBOX ФИГУРЫ (не от размера холста): радиус = половина
    диагонали bbox * 1.08 (кольцо чуть шире силуэта), кегль = fig_w * доля, уменьшается
    при необходимости, чтобы суммарная угловая ширина всех символов (с зазором) не
    превышала 360° (иначе буквы налезали бы друг на друга на плотной фразе).

    Возвращает RGBA того же размера, что rgba (без доп. кропа — вызывающий код
    batch_print уже работает с готовым diecut-холстом, на котором есть место под
    кольцо, раз кольцо туда попросили нарисовать)."""
    rgba = rgba.convert("RGBA")
    phrase = str(phrase or "").strip()
    if not phrase:
        return rgba.copy()

    if roles is None:
        figure_palette = palette.extract_palette(rgba, n=4)
        roles = palette.PaletteRoles(figure_palette)

    fx0, fy0, fx1, fy1 = _typo._alpha_bbox(rgba)
    fig_w, fig_h = fx1 - fx0, fy1 - fy0

    canvas_pad = int(max(fig_w, fig_h) * 0.15)
    W, H = rgba.width + 2 * canvas_pad, rgba.height + 2 * canvas_pad
    cx, cy, radius = _ring_radius_and_center(rgba, fx0, fy0, fx1, fy1, canvas_pad)

    n = len(phrase)
    min_size = max(1, int(fig_w * _RING_MIN_FONT_FRAC))
    size = max(min_size, int(fig_w * 0.06))

    def _max_angular_width(sz: int) -> float:
        """Суммарный угол (градусы), который займут все n символов на радиусе radius
        при кегле sz — используется чтобы ужать кегль, если фраза слишком длинная для
        одного полного прохода по кольцу."""
        probe = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        pdraw = ImageDraw.Draw(probe)
        font = _font_v3(font_role, sz)
        total_deg = 0.0
        for ch in phrase:
            bbox = pdraw.textbbox((0, 0), ch, font=font)
            ch_w = max(1, bbox[2] - bbox[0])
            # Длина дуги ch_w (с небольшим зазором) -> угол в радианах -> градусы.
            arc_len = ch_w * 1.35
            total_deg += math.degrees(arc_len / max(1.0, radius))
        return total_deg

    # Ужимаем кегль, пока полный проход по кольцу (360°, с запасом 5% на визуальный
    # зазор в точке начала/конца) не влезает — иначе символы налезали бы друг на друга.
    while size > min_size and _max_angular_width(size) > 342.0:
        size -= 1
    font = _font_v3(font_role, size)

    ring_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    probe = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(probe)

    # Угловой шаг РАВНОМЕРНЫЙ — ровно 360°/n, гарантирует РОВНО ОДИН проход по кольцу
    # независимо от фактической ширины отдельных символов (в отличие от жадной
    # раскладки "ширина символа -> угол", которая могла бы не дойти/перекрыть полный
    # круг на неравномерных по ширине глифах).
    step_deg = 360.0 / n
    direction = 1.0 if clockwise else -1.0

    word_colors_cycle = [roles.accent, roles.dominant]
    color_idx = 0
    prev_was_space = True  # первый "видимый" символ после начала/пробела красится accent
    for i, ch in enumerate(phrase):
        angle_deg = start_angle_deg + direction * step_deg * i
        angle_rad = math.radians(angle_deg)

        if ch == " ":
            prev_was_space = True
            continue
        if prev_was_space:
            color_idx = 0
        color = word_colors_cycle[color_idx % 2]
        prev_was_space = False
        color_idx += 1

        bbox = pdraw.textbbox((0, 0), ch, font=font)
        ch_w = bbox[2] - bbox[0]
        ch_h = bbox[3] - bbox[1]

        # Символ рисуется на маленьком прозрачном тайле, поворачивается вокруг
        # СВОЕГО ЦЕНТРА на угол (90 + angle_deg) — так глиф остаётся "ногами к центру
        # кольца" (перпендикулярен радиусу) на любой позиции окружности.
        tile_pad = max(ch_w, ch_h) + 4
        tile = Image.new("RGBA", (tile_pad * 2, tile_pad * 2), (0, 0, 0, 0))
        tdraw = ImageDraw.Draw(tile)
        stroke_w = max(1, int(size * 0.06))
        tdraw.text((tile_pad - bbox[0] - ch_w / 2, tile_pad - bbox[1] - ch_h / 2), ch,
                   font=font, fill=color, stroke_width=stroke_w, stroke_fill=roles.dark)

        rot_angle = -(angle_deg + 90.0)
        rotated = tile.rotate(rot_angle, resample=Image.BICUBIC, expand=False)

        px = cx + radius * math.cos(angle_rad)
        py = cy + radius * math.sin(angle_rad)
        paste_x = int(px - tile_pad)
        paste_y = int(py - tile_pad)
        ring_layer.alpha_composite(rotated, (paste_x, paste_y))

    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.alpha_composite(ring_layer)
    out.alpha_composite(rgba, (canvas_pad, canvas_pad))
    return out
