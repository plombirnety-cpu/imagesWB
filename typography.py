# -*- coding: utf-8 -*-
"""typography.py — нанесение слогана на diecut-принт (текст КОДОМ, не диффузией).

Причина отдельного модуля: владелец забраковал текущий текст в batch_print.py
(Impact + мягкое свечение-glow) как "мемный/дешёвый" при живой приёмке батча из
8 диекатов. Здесь — 5 альтернативных стилей (v1) под дорогой аниме-стритвир мерч:
жёсткая офсет-тень/обводка ВМЕСТО glow, наклон/скос вместо прямого текста,
поддержка катаканы для имени персонажа (Noto Sans JP Black).

Палитра цветов ЗАДУБЛИРОВАНА из batch_print._TITLE_COLORS (импортировать
batch_print отсюда нельзя — batch_print уже импортирует другие модули этого же
проекта и получится цикл; значения нужно синхронизировать руками, если владелец
поменяет палитру в batch_print.py).

Логика разбивки слогана на 1-2 строки скопирована из batch_print._split_slogan
(та же эвристика: <=14 символов - одна строка, иначе делим по словам ~40/60).

ТИПОГРАФИКА v2 (`compose_text`, `TEXT_MODES`, ближе к концу файла): вторая правка
владельца — v1 всё равно ужимала слоган в ОТДЕЛЬНУЮ строку в пустой полосе снизу,
оторванную от дизайна. `compose_text` позиционирует текст ОТ BOUNDING BOX'А АЛЬФЫ
ФИГУРЫ (реальные края персонажа, не проценты холста) и физически ПЕРЕКРЫВАЕТ силуэт —
текст становится частью композиции, как на референсных стритвир-принтах. Режимы:
"none" (дизайну не нужен текст), "under" (слово(а) за фигурой), "punch" (короткий
слоган внахлёст снизу, наклон), "kana_side" (вертикальная катакана вдоль края фигуры).
Старые STYLES/apply_style НЕ удалены и не изменены — обратная совместимость
`daily_prints.py`/CLI `--text-style`.
"""
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONTS_DIR = HERE / "fonts"

STYLES = ("none", "anton", "kana", "comic", "tag")

# Дубликат _TITLE_COLORS из batch_print.py (заливка, обводка/тень) — синхронизировать
# руками при правке палитры в batch_print.py.
_TITLE_COLORS = {
    "red": ((196, 30, 45), (15, 5, 20)),
    "orange": ((255, 140, 30), (20, 8, 5)),
    "white": ((240, 240, 240), (10, 10, 10)),
    "yellow": ((250, 200, 40), (40, 20, 0)),
    "purple": ((165, 70, 220), (20, 5, 30)),
    "black": ((25, 20, 25), (230, 230, 230)),
}

# Файлы шрифтов в fonts/ (скачаны с google/fonts, лицензия OFL) с fallback-цепочкой
# на системные шрифты Windows, чтобы модуль не падал при отсутствии файла.
_FONT_FILES = {
    "anton": ["Anton-Regular.ttf"],
    "archivo": ["ArchivoBlack-Regular.ttf"],
    "bangers": ["Bangers-Regular.ttf"],
    "notojp": ["NotoSansJP[wght].ttf"],
}
_FALLBACK_CHAIN = ["arialbd.ttf", "C:/Windows/Fonts/arialbd.ttf"]
_FALLBACK_JP = ["C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/msgothic.ttc"]

_font_cache = {}


def _font(name: str, size: int) -> "ImageFont.FreeTypeFont":
    """Загрузить шрифт по логическому имени (ключ _FONT_FILES) с кэшем и fallback-
    цепочкой: файл в fonts/ -> системный bold -> arial (гарантированно есть на Windows).
    Для 'notojp' дополнительно ставит variable-ось Weight=900 (Black) — variable-шрифт
    без явной установки оси рендерится в Thin (проверено: get_variation_axes показывает
    default=100)."""
    cache_key = (name, size)
    if cache_key in _font_cache:
        return _font_cache[cache_key]

    candidates = [str(FONTS_DIR / fn) for fn in _FONT_FILES.get(name, [])]
    is_jp = name == "notojp"
    candidates += _FALLBACK_JP if is_jp else _FALLBACK_CHAIN

    font = None
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            if is_jp and "NotoSansJP" in path:
                try:
                    font.set_variation_by_axes([900])  # Black
                except Exception:  # noqa: BLE001 — не variable-шрифт/нет оси, ок
                    pass
            break
        except OSError:
            continue
    if font is None:  # последний рубеж — дефолтный растровый шрифт PIL
        font = ImageFont.load_default()
    _font_cache[cache_key] = font
    return font


def _split_slogan(slogan: str) -> list:
    """Скопировано из batch_print._split_slogan: 1 строка если <=14 символов,
    иначе делим по словам ~40/60 (первая строка короче/крупнее)."""
    slogan = slogan.strip()
    if len(slogan) <= 14:
        return [slogan]
    words, line1 = slogan.split(), []
    while words and len(" ".join(line1 + [words[0]])) <= max(8, int(len(slogan) * 0.42)):
        line1.append(words.pop(0))
    if not line1:  # одно очень длинное слово
        line1.append(words.pop(0))
    return [" ".join(line1), " ".join(words)] if words else [" ".join(line1)]


def _colors(color_key: str) -> tuple:
    return _TITLE_COLORS.get(color_key, _TITLE_COLORS["orange"])


def _fit_font(draw: "ImageDraw.ImageDraw", text: str, font_name: str, size: int,
             max_width: float, spacing: int = 0) -> tuple:
    """Подобрать размер шрифта так, чтобы текст (с учётом letter-spacing, если задан)
    уместился в max_width. Возвращает (font, text_width, bbox)."""
    font = _font(font_name, size)
    tw = _measure(draw, text, font, spacing)
    if tw > max_width and tw > 0:
        size = max(10, int(size * max_width / tw))
        font = _font(font_name, size)
        tw = _measure(draw, text, font, spacing)
    return font, tw


def _measure(draw: "ImageDraw.ImageDraw", text: str, font, spacing: int = 0) -> int:
    """Ширина строки с учётом ручного letter-spacing (сумма ширин глифов + зазоры)."""
    if spacing == 0:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    total = 0
    for ch in text:
        bbox = draw.textbbox((0, 0), ch, font=font)
        total += (bbox[2] - bbox[0]) + spacing
    return max(0, total - spacing)


def _draw_spaced(base: Image.Image, xy: tuple, text: str, font, fill, spacing: int,
                 stroke_width: int = 0, stroke_fill=None) -> None:
    """Отрисовать строку с letter-spacing (PIL сам разрядку не умеет)."""
    x, y = xy
    draw = ImageDraw.Draw(base)
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        bbox = draw.textbbox((0, 0), ch, font=font)
        x += (bbox[2] - bbox[0]) + spacing


def _hard_shadow_text(rgba: Image.Image, xy: tuple, text: str, font, fill: tuple,
                      shadow_color: tuple, offset: tuple, stroke_width: int = 0,
                      stroke_fill=None) -> None:
    """Жёсткая офсет-тень (сплошной силуэт текста, сдвинутый на offset) БЕЗ размытия —
    замена мягкому glow, который владелец забраковал как "мемный/дешёвый"."""
    x, y = xy
    draw = ImageDraw.Draw(rgba)
    draw.text((x + offset[0], y + offset[1]), text, font=font, fill=shadow_color + (255,),
              stroke_width=stroke_width, stroke_fill=shadow_color)
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width,
              stroke_fill=stroke_fill)


def _rotate_paste(rgba: Image.Image, layer: Image.Image, angle: float,
                  center_y_frac: float) -> Image.Image:
    """Повернуть прозрачный слой layer (тот же размер, что rgba) вокруг его центра
    и наложить на rgba через alpha_composite."""
    rotated = layer.rotate(angle, resample=Image.BICUBIC, expand=False,
                           center=(layer.width / 2, layer.height * center_y_frac))
    out = rgba.copy()
    out.alpha_composite(rotated)
    return out


def _style_none(rgba: Image.Image, slogan: str, color_key: str, kana: str) -> Image.Image:
    return rgba.copy()


def _style_anton(rgba: Image.Image, slogan: str, color_key: str, kana: str) -> Image.Image:
    """Anton UPPERCASE, курсивный шер по X, сплошная заливка + жёсткая офсет-тень,
    2 строки плотным интерлиньяжем. Стритвир-плакат вместо мемного glow."""
    if not slogan:
        return rgba.copy()
    fill, shadow = _colors(color_key)
    lines = [ln.upper() for ln in _split_slogan(slogan)]
    W, H = rgba.size

    # Рисуем текст на отдельном прозрачном холсте побольше (с запасом под шер/тень),
    # затем шерим (аффинный сдвиг по X) и вклеиваем в нижнюю зону исходника.
    pad = int(W * 0.08)
    layer = Image.new("RGBA", (W + 2 * pad, H + 2 * pad), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    shadow_off = (int(W * 0.012), int(W * 0.012))

    y = int(H * 0.80)
    max_w = W * 0.90
    for i, line in enumerate(lines):
        size = int(W * (0.135 if i == 0 else 0.11))
        font, tw = _fit_font(draw, line, "anton", size, max_w)
        bbox = draw.textbbox((0, 0), line, font=font)
        x = pad + (W - tw) // 2 - bbox[0]
        yy = pad + y - bbox[1]
        _hard_shadow_text(layer, (x, yy), line, font, fill, shadow, shadow_off,
                          stroke_width=max(2, int(size * 0.03)), stroke_fill=shadow)
        y += int((bbox[3] - bbox[1]) * 1.05)

    # Курсивный шер: аффинное преобразование x' = x + k*(y - y0) — визуально наклон
    # ~-11 градусов (k=-0.18), центр сдвига — верх текстового блока, чтобы низ не
    # улетал за пределы канваса.
    k = -0.18
    y0 = int(H * 0.78) + pad
    sheared = layer.transform(
        layer.size, Image.AFFINE,
        (1, k, -k * y0, 0, 1, 0),
        resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0),
    )
    out = rgba.copy()
    out.alpha_composite(sheared, (-pad, -pad))
    return out


def _style_kana(rgba: Image.Image, slogan: str, color_key: str, kana: str) -> Image.Image:
    """Крупное имя катаканой (Noto Sans JP Black) + мелкий английский слоган с
    letter-spacing под ним. Без kana — деградирует в 'anton'."""
    if not kana:
        return _style_anton(rgba, slogan, color_key, kana)
    fill, shadow = _colors(color_key)
    W, H = rgba.size
    out = rgba.copy()
    draw = ImageDraw.Draw(out)

    max_w = W * 0.90
    kana_size = int(W * 0.19)
    font_kana, tw = _fit_font(draw, kana, "notojp", kana_size, max_w)
    bbox = draw.textbbox((0, 0), kana, font=font_kana)
    y_kana = int(H * 0.775)
    x_kana = (W - tw) // 2 - bbox[0]
    shadow_off = (int(W * 0.011), int(W * 0.011))
    _hard_shadow_text(out, (x_kana, y_kana - bbox[1]), kana, font_kana, fill, shadow,
                      shadow_off, stroke_width=max(2, int(kana_size * 0.025)),
                      stroke_fill=shadow)

    if slogan:
        slogan_line = slogan.strip().upper()
        spacing = max(2, int(W * 0.006))
        size = int(W * 0.05)
        font_sub = _font("anton", size)
        tw_sub = _measure(draw, slogan_line, font_sub, spacing)
        max_w_sub = W * 0.88
        if tw_sub > max_w_sub and tw_sub > 0:
            size = max(10, int(size * max_w_sub / tw_sub))
            font_sub = _font("anton", size)
            spacing = max(1, int(spacing * max_w_sub / tw_sub))
            tw_sub = _measure(draw, slogan_line, font_sub, spacing)
        y_sub = y_kana - bbox[1] + int((bbox[3] - bbox[1]) * 1.12)
        x_sub = (W - tw_sub) // 2
        _draw_spaced(out, (x_sub, y_sub), slogan_line, font_sub, fill, spacing,
                    stroke_width=max(1, int(size * 0.05)), stroke_fill=shadow)
    return out


def _style_comic(rgba: Image.Image, slogan: str, color_key: str, kana: str) -> Image.Image:
    """Bangers, лёгкий поворот -3 градуса, толстая белая обводка + цветная заливка —
    манга-звук/комикс-эффект без мемного glow."""
    if not slogan:
        return rgba.copy()
    fill, _shadow = _colors(color_key)
    lines = [ln.upper() for ln in _split_slogan(slogan)]
    W, H = rgba.size
    pad = int(W * 0.10)
    layer = Image.new("RGBA", (W + 2 * pad, H + 2 * pad), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    y = int(H * 0.80)
    max_w = W * 0.84
    for i, line in enumerate(lines):
        size = int(W * (0.145 if i == 0 else 0.115))
        font, tw = _fit_font(draw, line, "bangers", size, max_w)
        bbox = draw.textbbox((0, 0), line, font=font)
        x = pad + (W - tw) // 2 - bbox[0]
        yy = pad + y - bbox[1]
        # Толстая белая обводка снизу, затем цветная заливка с чёрной тонкой обводкой
        # поверх — держит контраст на любом фоне (манга-стиль звукового эффекта).
        draw.text((x, yy), line, font=font, fill=(255, 255, 255, 255),
                  stroke_width=max(4, int(size * 0.16)), stroke_fill=(255, 255, 255, 255))
        draw.text((x, yy), line, font=font, fill=fill,
                  stroke_width=max(2, int(size * 0.045)), stroke_fill=(15, 15, 15, 255))
        y += int((bbox[3] - bbox[1]) * 1.08)

    rotated = layer.rotate(-3, resample=Image.BICUBIC, expand=False,
                           center=(layer.width / 2, int(H * 0.78) + pad))
    out = rgba.copy()
    out.alpha_composite(rotated, (-pad, -pad))
    return out


def _parallelogram(w: int, h: int, skew: int, color: tuple) -> Image.Image:
    """Скошенный прямоугольник (параллелограмм) w x h, заливка color, сдвиг верхней
    грани на skew px — плашка японского уличного лейбла."""
    tag = Image.new("RGBA", (w + skew, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(tag)
    d.polygon([(skew, 0), (w + skew, 0), (w, h), (0, h)], fill=color + (255,))
    return tag


def _style_tag(rgba: Image.Image, slogan: str, color_key: str, kana: str) -> Image.Image:
    """Компактная плашка-параллелограмм цветом палитры, внутри слоган контрастным
    (чёрным/белым) Archivo Black в 1-2 строки — японский уличный лейбл."""
    if not slogan:
        return rgba.copy()
    fill, _shadow = _colors(color_key)
    # Контрастный текст внутри плашки: чёрный если заливка светлая, иначе белый.
    luminance = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
    text_color = (20, 20, 20, 255) if luminance > 140 else (245, 245, 245, 255)

    lines = _split_slogan(slogan.strip().upper())
    W, H = rgba.size
    tmp_draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))

    pad_x, pad_y = int(W * 0.05), int(H * 0.018)
    max_text_w = W * 0.80
    sizes, fonts, widths, line_heights = [], [], [], []
    for i, line in enumerate(lines):
        size = int(W * (0.075 if i == 0 else 0.065))
        font, tw = _fit_font(tmp_draw, line, "archivo", size, max_text_w)
        bbox = tmp_draw.textbbox((0, 0), line, font=font)
        sizes.append(size)
        fonts.append(font)
        widths.append(tw)
        line_heights.append(bbox[3] - bbox[1])

    text_w = max(widths)
    text_h = sum(line_heights) + int(H * 0.01) * (len(lines) - 1)
    tag_w = int(text_w + pad_x * 2)
    tag_h = int(text_h + pad_y * 2)
    skew = int(tag_h * 0.35)

    tag_layer = _parallelogram(tag_w, tag_h, skew, fill)
    out = rgba.copy()
    tag_x = (W - tag_layer.width) // 2
    tag_y = int(H * 0.82) - tag_layer.height // 2
    out.alpha_composite(tag_layer, (tag_x, tag_y))

    draw = ImageDraw.Draw(out)
    y_cursor = tag_y + pad_y
    for line, font, tw, lh in zip(lines, fonts, widths, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        x = tag_x + skew // 2 + (tag_w - tw) // 2 - bbox[0]
        draw.text((x, y_cursor - bbox[1]), line, font=font, fill=text_color)
        y_cursor += lh + int(H * 0.01)
    return out


_STYLE_FUNCS = {
    "none": _style_none,
    "anton": _style_anton,
    "kana": _style_kana,
    "comic": _style_comic,
    "tag": _style_tag,
}


def apply_style(rgba: Image.Image, style: str, slogan: str, color_key: str,
                kana: str = "") -> Image.Image:
    """rgba: PIL.Image RGBA (прозрачный диекат). Возвращает RGBA того же размера с
    нанесённым текстом. style — один из STYLES; slogan — латиница до 34 символов;
    color_key — red/orange/white/yellow/purple/black; kana — имя катаканой (может
    быть пустым)."""
    fn = _STYLE_FUNCS.get(style, _style_none)
    return fn(rgba.convert("RGBA"), slogan or "", color_key, kana or "")


def preview_all(rgba: Image.Image, slogan: str, color_key: str, kana: str = "") -> dict:
    """style -> PIL.Image для калибровки — прогоняет apply_style по всем STYLES."""
    return {style: apply_style(rgba, style, slogan, color_key, kana) for style in STYLES}


# ═══════════════════════════════════════════════════════════════════════════════
# Типографика v2 — текст КАК ЧАСТЬ КОМПОЗИЦИИ (не оторванная строка в пустой полосе).
#
# Правка владельца после приёмки референсов: типографика v1 (5 стилей выше) ужимает
# слоган в отдельную строку в пустой зоне снизу — оторвано от дизайна. Новые режимы
# TEXT_MODES позиционируются ОТ РЕАЛЬНОГО BOUNDING BOX'А АЛЬФЫ ФИГУРЫ (не от процентов
# холста) и физически ПЕРЕКРЫВАЮТ силуэт — текст становится частью печатной композиции,
# как на референсных стритвир-принтах. Старые apply_style/STYLES не удалены и не
# изменены (обратная совместимость daily_prints/CLI --text-style).
# ═══════════════════════════════════════════════════════════════════════════════

TEXT_MODES = ("none", "under", "punch", "kana_side")

# Минимальный кегль как доля от ширины фигуры (bbox альфы) — текст не ужимается тише
# этого, даже если строка длинная (лучше разбить по словам заново/сократить, см.
# _fit_lines_min_size).
_MIN_FONT_FRAC = 0.07

# Поля итогового кропа как доля от большей стороны контента (текст+фигура вместе).
_CROP_MARGIN_FRAC = 0.05


def _alpha_bbox(rgba: Image.Image) -> tuple:
    """Bounding box непрозрачных пикселей (реальные края фигуры), либо весь холст,
    если альфа пуста (не должно происходить на реальных вырезках, но не падать)."""
    bbox = rgba.getchannel("A").getbbox()
    return bbox if bbox is not None else (0, 0, rgba.width, rgba.height)


def _fit_lines_min_size(draw: "ImageDraw.ImageDraw", words: list, font_name: str,
                        start_size: int, max_width: float, min_size: int,
                        max_lines: int = 3) -> tuple:
    """Уложить words в максимум max_lines строк так, чтобы кегль не падал ниже
    min_size: сперва пробуем ВСЕ слова в одну строку с уменьшением кегля до min_size;
    если всё ещё не влезает — разбиваем на больше строк (перераспределяем слова
    поровну) вместо дальнейшего ужимания шрифта. Возвращает (lines, font, sizes) —
    sizes параллельны lines (первая строка может быть крупнее — ударное слово)."""
    text_all = " ".join(words)
    font, tw = _fit_font(draw, text_all, font_name, start_size, max_width)
    if tw > 0 and font.size >= min_size:
        return [text_all], font, [font.size]

    # Не влезает даже на min_size одной строкой — раскладываем по нескольким строкам,
    # число строк растёт, пока кегль на min_size не уложится, либо до max_lines.
    for n_lines in range(2, max_lines + 1):
        chunk = max(1, math.ceil(len(words) / n_lines))
        lines = [" ".join(words[i:i + chunk]) for i in range(0, len(words), chunk)]
        lines = [ln for ln in lines if ln]
        widest = max((_measure(draw, ln, _font(font_name, min_size)) for ln in lines),
                     default=0)
        if widest <= max_width or n_lines == max_lines:
            font_min = _font(font_name, min_size)
            return lines, font_min, [min_size] * len(lines)
    # Не должно достигаться (цикл всегда возвращает на n_lines==max_lines), но держим
    # безопасный дефолт.
    font_min = _font(font_name, min_size)
    return [text_all], font_min, [min_size]


def _crop_to_content(rgba: Image.Image, margin_frac: float = _CROP_MARGIN_FRAC) -> Image.Image:
    """Кроп холста до bbox непрозрачных пикселей + равномерные поля margin_frac от
    большей стороны итогового содержимого — убирает мёртвую пустую полосу, которую
    оставляла типографика v1."""
    bbox = _alpha_bbox(rgba)
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    margin = int(max(w, h) * margin_frac)
    nx0 = max(0, x0 - margin)
    ny0 = max(0, y0 - margin)
    nx1 = min(rgba.width, x1 + margin)
    ny1 = min(rgba.height, y1 + margin)
    return rgba.crop((nx0, ny0, nx1, ny1))


def _best_under_row_frac(rgba: Image.Image, fx0: int, fy0: int, fig_w: int, fig_h: int,
                         text_h: int) -> float:
    """Подбирает долю высоты фигуры (от fy0), на которой текстовая строка 'under'
    перекрывается силуэтом МЕНЬШЕ всего по бокам — иначе на широкоплечих/раскинутых
    силуэтах (руки/пламя в стороны) фиксированная высота 0.28 может целиком спрятать
    буквы по краям (реальный баг: 'M'/'E' терялись под плечом на диекате Кенпачи).
    Кандидаты — несколько высот в верхней половине bbox; побеждает та, где непрозрачная
    ширина фигуры В ПОЛОСЕ текста наименьшая (буквы больше выглядывают по бокам).
    Не идёт ниже 0.12 и выше 0.45 — остаётся в пределах верхней части фигуры, как и
    задумано изначально (голова/плечи), просто не намертво на одном значении."""
    alpha = np.array(rgba.getchannel("A"))
    candidates = [0.12, 0.18, 0.22, 0.28, 0.34, 0.40, 0.45]
    best_frac, best_width = 0.28, None
    half = max(1, text_h // 2)
    for frac in candidates:
        row_center = fy0 + int(fig_h * frac)
        y0 = max(0, row_center - half)
        y1 = min(alpha.shape[0], row_center + half)
        if y1 <= y0:
            continue
        band = alpha[y0:y1, :] > 0
        cols = np.where(band.any(axis=0))[0]
        width = int(cols[-1] - cols[0]) if cols.size else 0
        if best_width is None or width < best_width:
            best_width, best_frac = width, frac
    return best_frac


def _compose_under(rgba: Image.Image, slogan: str, color_key: str) -> Image.Image:
    """'under' — стритвир-плакат: 1-2 ударных слова ОГРОМНЫМ кеглем ЗА фигурой (текстовый
    слой рисуется ПЕРВЫМ, фигура наклеивается ПОВЕРХ него — персонаж частично перекрывает
    буквы). Ширина текста ~= ширине фигуры. Размещение — верхняя часть bbox фигуры, высота
    подбирается по фактической ширине силуэта на этой строке (см. _best_under_row_frac),
    чтобы буквы не терялись целиком под широкими плечами/раскинутыми руками/эффектами."""
    fill, shadow = _colors(color_key)
    fx0, fy0, fx1, fy1 = _alpha_bbox(rgba)
    fig_w, fig_h = fx1 - fx0, fy1 - fy0

    words = slogan.strip().upper().split()
    text = " ".join(words[:2]) if words else ""
    if not text:
        return rgba.copy()

    # Холст с запасом со всех сторон — огромный кегль легко выходит за исходные
    # границы, расширяем заранее и обрежем в конце через _crop_to_content.
    pad = int(fig_w * 0.5)
    W, H = rgba.width + 2 * pad, rgba.height + 2 * pad
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    max_w = fig_w * 1.18
    size = int(fig_w * 0.42)
    font, tw = _fit_font(draw, text, "archivo", size, max_w)
    if font.size < int(fig_w * _MIN_FONT_FRAC):
        font = _font("archivo", max(1, int(fig_w * _MIN_FONT_FRAC)))
        tw = _measure(draw, text, font)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = bbox[3] - bbox[1]

    row_frac = _best_under_row_frac(rgba, fx0, fy0, fig_w, fig_h, text_h)
    cx = pad + fx0 + fig_w / 2
    cy = pad + fy0 + fig_h * row_frac
    x = int(cx - tw / 2) - bbox[0]
    y = int(cy - text_h / 2) - bbox[1]

    shadow_off = (int(fig_w * 0.012), int(fig_w * 0.012))
    _hard_shadow_text(canvas, (x, y), text, font, fill, shadow, shadow_off,
                      stroke_width=max(2, int(font.size * 0.03)), stroke_fill=shadow)

    # Фигура наклеивается ПОВЕРХ текстового слоя — буквы частично уходят под силуэт.
    canvas.alpha_composite(rgba, (pad, pad))
    return canvas


def _compose_punch(rgba: Image.Image, slogan: str, color_key: str) -> Image.Image:
    """'punch' — короткий слоган 1-3 строки, балансная разбивка (ключевое слово крупнее
    остальных в 1.5-2x), наклон -6..-10 градусов, размещение ВПЛОТНУЮ к нижней части
    фигуры с лёгким перекрытием силуэта (текст рисуется ПОВЕРХ фигуры)."""
    fill, shadow = _colors(color_key)
    fx0, fy0, fx1, fy1 = _alpha_bbox(rgba)
    fig_w, fig_h = fx1 - fx0, fy1 - fy0

    words = slogan.strip().upper().split()
    if not words:
        return rgba.copy()

    pad = int(fig_w * 0.25)
    W, H = rgba.width + 2 * pad, rgba.height + 2 * pad
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.alpha_composite(rgba, (pad, pad))
    draw = ImageDraw.Draw(canvas)

    min_size = max(10, int(fig_w * _MIN_FONT_FRAC))
    max_w = fig_w * 0.92
    # Ключевое слово — самое длинное слово строки — крупнее остальных в 1.5-2x.
    key_idx = max(range(len(words)), key=lambda i: len(words[i]))

    lines, base_font, sizes = _fit_lines_min_size(draw, words, "anton",
                                                  int(fig_w * 0.16), max_w, min_size)

    # Позиционируем строки снизу вверх, ВПЛОТНУЮ к нижнему краю фигуры (лёгкое
    # перекрытие силуэта — верх текстового блока заходит НА фигуру).
    overlap = int(fig_h * 0.08)
    y_bottom = pad + fy1 - overlap
    line_imgs = []
    for i, line in enumerate(lines):
        is_key_line = len(lines) == 1 and key_idx < len(words)
        size = sizes[i]
        if is_key_line and len(words) > 1:
            # Одна строка со всеми словами: рисуем ключевое слово крупнее остальных.
            line_imgs.append(("mixed", words, size))
        else:
            line_imgs.append(("plain", line, size))

    # Рендерим строки на отдельном прозрачном слое размером с canvas, снизу вверх,
    # затем шерим (наклон) весь блок разом — тот же приём, что _style_anton.
    text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(text_layer)
    y_cursor = y_bottom
    rendered = []
    for kind, payload, size in reversed(line_imgs):
        font = _font("anton", size)
        if kind == "plain":
            tw = _measure(tdraw, payload, font)
            bbox = tdraw.textbbox((0, 0), payload, font=font)
            lh = bbox[3] - bbox[1]
            rendered.append((kind, payload, font, tw, bbox, lh))
        else:
            # mixed: ключевое слово в 1.7x, остальные слова — базовый size, одна строка.
            key_font = _font("anton", int(size * 1.7))
            pieces = []
            for wi, w in enumerate(payload):
                f = key_font if wi == key_idx else font
                pieces.append((w, f))
            tw = sum(_measure(tdraw, w, f) for w, f in pieces) + \
                int(fig_w * 0.02) * (len(pieces) - 1)
            bbox = tdraw.textbbox((0, 0), "".join(payload), font=key_font)
            lh = bbox[3] - bbox[1]
            rendered.append((kind, pieces, None, tw, bbox, lh))

    y_cursor = y_bottom
    for kind, payload, font, tw, bbox, lh in rendered:
        y_cursor -= lh
        x = int(pad + fx0 + fig_w / 2 - tw / 2)
        shadow_off = (max(1, int(fig_w * 0.01)), max(1, int(fig_w * 0.01)))
        if kind == "plain":
            yy = y_cursor - bbox[1]
            _hard_shadow_text(text_layer, (x, yy), payload, font, fill, shadow,
                              shadow_off, stroke_width=max(2, int(font.size * 0.035)),
                              stroke_fill=shadow)
        else:
            cursor_x = x
            yy = y_cursor - bbox[1]
            for w, f in payload:
                _hard_shadow_text(text_layer, (cursor_x, yy), w, f, fill, shadow,
                                  shadow_off, stroke_width=max(2, int(f.size * 0.035)),
                                  stroke_fill=shadow)
                cursor_x += _measure(tdraw, w, f) + int(fig_w * 0.02)
        y_cursor -= int(lh * 0.12)  # небольшой интерлиньяж между строками

    angle = -8  # в пределах -6..-10 градусов, как требует ТЗ
    pivot_y = y_bottom - int(fig_h * 0.1)
    rotated = text_layer.rotate(angle, resample=Image.BICUBIC, expand=False,
                                center=(W / 2, pivot_y))
    canvas.alpha_composite(rotated)
    return canvas


def _compose_kana_side(rgba: Image.Image, slogan: str, color_key: str,
                       kana: str) -> Image.Image:
    """'kana_side' — вертикальная катакана крупно вдоль левого/правого края фигуры,
    ПРИЖАТА к фигуре с ЧАСТИЧНЫМ перекрытием (примерно треть ширины колонки заходит
    под силуэт, не вся колонка), + маленький латинский слоган под ней СНАРУЖИ фигуры
    (не под силуэтом — иначе обрезается). Без kana — деградация в 'punch'."""
    if not kana:
        return _compose_punch(rgba, slogan, color_key)

    fill, shadow = _colors(color_key)
    fx0, fy0, fx1, fy1 = _alpha_bbox(rgba)
    fig_w, fig_h = fx1 - fx0, fy1 - fy0

    pad = int(fig_w * 0.45)
    W, H = rgba.width + 2 * pad, rgba.height + int(fig_h * 0.18) + 2 * pad
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(text_layer)

    # Сторона выбирается детерминированно от чётности длины kana — без внешнего
    # состояния/random, стабильно между вызовами для одного и того же дизайна.
    side_left = (len(kana) % 2 == 0)

    min_size = max(10, int(fig_w * _MIN_FONT_FRAC))
    kana_size = max(min_size, int(fig_w * 0.20))
    max_kana_h = fig_h * 0.8
    font_kana = _font("notojp", kana_size)
    # Вертикальная катакана рисуется по одному знаку сверху вниз (Noto Sans JP не даёт
    # честного vertical writing mode через PIL — имитируем построчной укладкой глифов).
    # Ширина колонки = максимум по всем символам (некоторые катакана-глифы уже других).
    def _glyph_dims(font):
        widths, heights = [], []
        for ch in kana:
            b = tdraw.textbbox((0, 0), ch, font=font)
            widths.append(b[2] - b[0])
            heights.append(b[3] - b[1])
        return max(widths, default=1), (max(heights, default=1) if heights else 1)

    col_w, glyph_h = _glyph_dims(font_kana)
    total_h = glyph_h * len(kana) * 1.08
    if total_h > max_kana_h:
        scale = max_kana_h / total_h
        kana_size = max(min_size, int(kana_size * scale))
        font_kana = _font("notojp", kana_size)
        col_w, glyph_h = _glyph_dims(font_kana)
        total_h = glyph_h * len(kana) * 1.08

    # Прижато к фигуре с ЧАСТИЧНЫМ перекрытием: только ~35% ширины колонки заходит
    # под силуэт (не вся колонка, иначе катакана нечитаема — проверено на смоук-тесте).
    overlap = col_w * 0.35
    if side_left:
        col_cx = pad + fx0 - col_w / 2 + overlap
    else:
        col_cx = pad + fx1 + col_w / 2 - overlap

    y = pad + fy0 + (fig_h - total_h) / 2
    shadow_off = (max(1, int(fig_w * 0.01)), max(1, int(fig_w * 0.01)))
    for ch in kana:
        bbox = tdraw.textbbox((0, 0), ch, font=font_kana)
        ch_w = bbox[2] - bbox[0]
        x = int(col_cx - ch_w / 2) - bbox[0]
        _hard_shadow_text(text_layer, (x, int(y) - bbox[1]), ch, font_kana, fill, shadow,
                          shadow_off, stroke_width=max(2, int(kana_size * 0.025)),
                          stroke_fill=shadow)
        y += glyph_h * 1.08

    # Маленький латинский слоган под колонкой каны, растёт НАРУЖУ от фигуры (не
    # центрируется на col_cx — силуэт персонажа не прямоугольный, на высоте sub-
    # текста фигура может быть ШИРЕ, чем колонка каны, и центрированный текст уйдёт
    # частично ПОД силуэт и станет нечитаемым; растим от внешнего края катаканы наружу
    # от холста, к дальнему от фигуры краю — гарантированно не под фигурой).
    if slogan:
        sub = slogan.strip().upper()
        spacing = max(2, int(fig_w * 0.006))
        # sub-слоган — ВТОРОСТЕПЕННЫЙ мелкий текст под катаканой (не главный ударный
        # элемент композиции) — свой, более мягкий минимальный кегль, НЕ _MIN_FONT_FRAC
        # (тот рассчитан на ударный текст punch/under и был бы КРУПНЕЕ стартового
        # размера sub-слогана, что ломало ужимание под доступную ширину).
        sub_min_size = max(8, int(fig_w * 0.02))
        sub_size = max(sub_min_size, int(fig_w * 0.045))
        font_sub = _font("anton", sub_size)
        tw_sub = _measure(tdraw, sub, font_sub, spacing)
        outer_edge = col_cx - col_w / 2 if side_left else col_cx + col_w / 2
        # Доступное место — от внешнего края катаканы до края холста, с запасом под
        # финальный кроп (_crop_to_content добавляет margin_frac полей — оставляем
        # столько же пространства здесь, иначе текст доходит до самого края холста
        # и после кропа выглядит обрезанным по факту прилипания к границе кадра).
        margin_side = fig_w * (_CROP_MARGIN_FRAC + 0.04)
        available_w = max(sub_min_size * 2, (outer_edge - margin_side) if side_left
                          else (W - outer_edge - margin_side))
        max_w_sub = min(fig_w * 0.85, available_w)
        if tw_sub > max_w_sub and tw_sub > 0:
            sub_size = max(sub_min_size, int(sub_size * max_w_sub / tw_sub))
            font_sub = _font("anton", sub_size)
            spacing = max(1, int(spacing * max_w_sub / tw_sub))
            tw_sub = _measure(tdraw, sub, font_sub, spacing)
        sub_x = int(outer_edge - tw_sub) if side_left else int(outer_edge)
        sub_y = int(y + glyph_h * 0.35)
        _draw_spaced(text_layer, (sub_x, sub_y), sub, font_sub, fill, spacing,
                    stroke_width=max(1, int(sub_size * 0.05)), stroke_fill=shadow)

    canvas.alpha_composite(text_layer)
    canvas.alpha_composite(rgba, (pad, pad))
    return canvas


_COMPOSE_FUNCS = {
    "none": lambda rgba, slogan, color_key, kana: rgba.copy(),
    "under": lambda rgba, slogan, color_key, kana: _compose_under(rgba, slogan, color_key),
    "punch": lambda rgba, slogan, color_key, kana: _compose_punch(rgba, slogan, color_key),
    "kana_side": _compose_kana_side,
}


def compose_text(figure_rgba: Image.Image, text_mode: str, slogan: str, color_key: str,
                 kana: str = "") -> Image.Image:
    """Типографика v2: текст КАК ЧАСТЬ КОМПОЗИЦИИ, позиционирование от bounding box'а
    альфы фигуры (не от процентов холста), холст расширяется по необходимости внутри
    этой функции, итог кропится до контента с полями 4-6% (без мёртвой пустой полосы).

    figure_rgba: PIL.Image RGBA (прозрачная вырезка). text_mode — один из TEXT_MODES
    ("none"/"under"/"punch"/"kana_side", "kana_side" без kana деградирует в "punch").
    slogan — латиница; color_key — red/orange/white/yellow/purple/black; kana — имя
    катаканой (может быть пустым).

    Возвращает RGBA, обрезанный до контента (текст+фигура) + равномерные поля."""
    rgba = figure_rgba.convert("RGBA")
    mode = text_mode if text_mode in TEXT_MODES else "none"
    fn = _COMPOSE_FUNCS.get(mode, _COMPOSE_FUNCS["none"])
    composed = fn(rgba, slogan or "", color_key, kana or "")
    if mode == "none":
        return composed
    return _crop_to_content(composed)
