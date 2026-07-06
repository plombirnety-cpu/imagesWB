# -*- coding: utf-8 -*-
"""typography.py — стили нанесения слогана на diecut-принт (текст КОДОМ, не диффузией).

Причина отдельного модуля: владелец забраковал текущий текст в batch_print.py
(Impact + мягкое свечение-glow) как "мемный/дешёвый" при живой приёмке батча из
8 диекатов. Здесь — 5 альтернативных стилей под дорогой аниме-стритвир мерч:
жёсткая офсет-тень/обводка ВМЕСТО glow, наклон/скос вместо прямого текста,
поддержка катаканы для имени персонажа (Noto Sans JP Black).

Палитра цветов ЗАДУБЛИРОВАНА из batch_print._TITLE_COLORS (импортировать
batch_print отсюда нельзя — batch_print уже импортирует другие модули этого же
проекта и получится цикл; значения нужно синхронизировать руками, если владелец
поменяет палитру в batch_print.py).

Логика разбивки слогана на 1-2 строки скопирована из batch_print._split_slogan
(та же эвристика: <=14 символов - одна строка, иначе делим по словам ~40/60).
"""
import math
from pathlib import Path

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
