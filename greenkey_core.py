# -*- coding: utf-8 -*-
"""GreenKey core — удаление зелёного или синего хромакея.

Это встроенная копия ядра GreenKey Web, синхронизированная с обновлённой
моделью Ruslanglb/GREEN_BLUE от 2026-07-21. Она живёт в Print Factory, чтобы
финальная подготовка принта не зависела от доступности отдельного HTTP-сервиса.

Особенности актуальной модели:
  - автоматический выбор зелёного/синего ключа по рамке;
  - отдельный Clip Black для синего экрана;
  - защита пурпурных/фиолетовых деталей;
  - правильный despill синего (подавляются и G, и B);
  - резкий режим без апскейла по умолчанию.

``process()`` возвращает ``(rgba, screen_colour_rgb255, key)``, где
``key``: 1=зелёный, 2=синий.
"""

import numpy as np
from PIL import Image, ImageFilter


IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")

SCREEN_COLOUR_GREEN = (11 / 255.0, 163 / 255.0, 77 / 255.0)
SCREEN_COLOUR_BLUE = (0 / 255.0, 89 / 255.0, 255 / 255.0)
SCREEN_COLOUR = SCREEN_COLOUR_GREEN
CLIP_BLACK = 0.09
CLIP_WHITE = 0.76
CLIP_BLACK_BLUE = 0.20
CLIP_BLACK_BLUE_SHARP = 0.36
BLUE_MAGENTA_MARGIN = 0.06
BLUE_MAGENTA_SOFT = 0.15
SPILL_SUPPRESSION = 1.00
DECON_MIN = 0.4
SPILL_DARK = 0.30
SPILL_LIGHT = 0.48
ANTIALIAS = True
AA_SS = 4
AA_TARGET = 6000
AA_MAX_SS_SIDE = 6300
ALPHA_FEATHER = 0.3
OUTPUT_MAX = 8192
PREVIEW_MAX_SIDE = 4096
OUTPUT_MAX_SCALE = 4.0
CONTOUR_CHOKE_SRC = 0.8
UPSCALE_SHARPEN = True
SHARPEN_PERCENT = 110
SHARP_MODE_DEFAULT = True


def detect_bg(arr):
    """Определить фон по рамке; вернуть (screen_colour, key)."""
    h, w = arr.shape[:2]
    m = max(3, min(h, w) // 25)
    ring = np.concatenate(
        [
            arr[:m].reshape(-1, 3),
            arr[-m:].reshape(-1, 3),
            arr[:, :m].reshape(-1, 3),
            arr[:, -m:].reshape(-1, 3),
        ]
    )
    med = np.median(ring, axis=0)
    r, g, b = float(med[0]), float(med[1]), float(med[2])
    greenness = g - max(r, b)
    blueness = b - max(r, g)
    if blueness > 0.04 and blueness >= greenness:
        return (r, g, b), 2
    if greenness > 0.04:
        return (r, g, b), 1
    return SCREEN_COLOUR_GREEN, 1


def key_from_colour(screen_colour):
    """По цвету экрана определить доминантный канал: 1=зелёный, 2=синий."""
    r, g, b = screen_colour
    return 2 if (b - max(r, g)) > (g - max(r, b)) else 1


def process(pil_rgb, override_bg=None, preview=False, sharp=SHARP_MODE_DEFAULT):
    """Keylight + Advanced Spill Suppressor.

    ``sharp=True`` сохраняет исходный размер и резкость. ``sharp=False``
    апскейлит до 8K и сглаживает край.
    """
    pil_rgb = pil_rgb.convert("RGB")
    w0, h0 = pil_rgb.size
    output_max = PREVIEW_MAX_SIDE if preview else OUTPUT_MAX
    output_scale = (
        1.0
        if sharp
        else max(1.0, min(OUTPUT_MAX_SCALE, output_max / max(w0, h0)))
    )
    if output_scale > 1.001:
        pil_rgb = pil_rgb.resize(
            (round(w0 * output_scale), round(h0 * output_scale)), Image.LANCZOS
        )
        if UPSCALE_SHARPEN and SHARPEN_PERCENT > 0:
            pil_rgb = pil_rgb.filter(
                ImageFilter.UnsharpMask(
                    radius=output_scale, percent=SHARPEN_PERCENT, threshold=0
                )
            )

    w, h = pil_rgb.size
    arr = np.asarray(pil_rgb, dtype=np.float32) / 255.0
    red, green, blue = arr[..., 0], arr[..., 1], arr[..., 2]

    if override_bg:
        screen = np.array(override_bg, dtype=np.float32)
        key = key_from_colour(override_bg)
    else:
        screen_tuple, key = detect_bg(arr)
        screen = np.array(screen_tuple, dtype=np.float32)

    supersample = 1
    if not sharp and ANTIALIAS and AA_SS > 1:
        supersample = min(AA_SS, max(1, round(AA_TARGET / max(w, h))))
        if max(w, h) * supersample > AA_MAX_SS_SIDE:
            supersample = max(1, AA_MAX_SS_SIDE // max(w, h))
    if supersample > 1:
        big = np.asarray(
            pil_rgb.resize((w * supersample, h * supersample), Image.LANCZOS)
        )
        alpha_big = _screen_matte(
            big[..., 0].astype(np.float32) / 255.0,
            big[..., 1].astype(np.float32) / 255.0,
            big[..., 2].astype(np.float32) / 255.0,
            screen,
            key,
            sharp,
        )
        alpha_img = Image.fromarray(
            (alpha_big * 255.0 + 0.5).astype(np.uint8), "L"
        ).resize((w, h), Image.BOX)
    else:
        alpha = _screen_matte(red, green, blue, screen, key, sharp)
        alpha_img = Image.fromarray(
            (alpha * 255.0 + 0.5).astype(np.uint8), "L"
        )

    choke = 0 if sharp else int(round(CONTOUR_CHOKE_SRC * output_scale))
    if choke > 0:
        alpha_img = _fast_erode(alpha_img, choke)
    feather = 0.0 if sharp else ALPHA_FEATHER
    if feather > 0:
        alpha_img = alpha_img.filter(ImageFilter.GaussianBlur(radius=feather))

    dominance = _key_dom(red, green, blue, key, screen)
    keyish = dominance > 0.0
    standard = arr.copy()
    if key == 1:
        average = 0.5 * (red + blue)
        standard[..., 1] = np.where(
            keyish,
            np.clip(
                green - SPILL_SUPPRESSION * (green - average), 0.0, 1.0
            ),
            green,
        )
    else:
        standard[..., 1] = np.where(
            keyish,
            np.clip(green - SPILL_SUPPRESSION * (green - red), 0.0, 1.0),
            green,
        )
        standard[..., 2] = np.where(
            keyish,
            np.clip(blue - SPILL_SUPPRESSION * (blue - red), 0.0, 1.0),
            blue,
        )

    key_strength = max(
        float(
            screen[key]
            - max(screen[(key + 1) % 3], screen[(key + 2) % 3])
        ),
        1e-3,
    )
    screen_weight = np.clip(dominance / key_strength, 0.0, 1.0)[..., None]
    foreground = np.clip(1.0 - screen_weight, DECON_MIN, 1.0)
    decontaminated = np.clip(
        (arr - screen_weight * screen) / foreground, 0.0, 1.0
    )
    luma = (0.299 * red + 0.587 * green + 0.114 * blue)[..., None]
    weight = np.clip(
        (luma - SPILL_DARK) / max(SPILL_LIGHT - SPILL_DARK, 1e-3),
        0.0,
        1.0,
    )
    blended = standard * (1.0 - weight) + decontaminated * weight
    work = np.where(keyish[..., None], blended, arr)

    rgb8 = (np.clip(work, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    output = Image.fromarray(rgb8, mode="RGB").convert("RGBA")
    output.putalpha(alpha_img)
    return output, tuple(int(value * 255) for value in screen), key


def _key_dom(red, green, blue, key, screen=None):
    """Доминирование ключевого канала; для синего защищает magenta."""
    del screen  # совместимость сигнатуры с исходным GreenKey Web
    if key == 2:
        dominance = blue - np.maximum(red, green)
        magenta = np.minimum(red, blue) - green
        guard = np.clip(
            (magenta - BLUE_MAGENTA_MARGIN) / max(BLUE_MAGENTA_SOFT, 1e-3),
            0.0,
            1.0,
        )
        return dominance * (1.0 - guard)
    return green - np.maximum(red, blue)


def _fast_erode(alpha_img, radius):
    """Быстрая эрозия квадратом (2r+1)x(2r+1) с edge-clamp."""
    alpha = np.asarray(alpha_img, dtype=np.uint8)
    for axis in (0, 1):
        accumulator = alpha.copy()
        for shift_size in range(1, radius + 1):
            for direction in (shift_size, -shift_size):
                shifted = np.roll(alpha, direction, axis=axis)
                if axis == 0:
                    if direction > 0:
                        shifted[:direction, :] = alpha[0:1, :]
                    else:
                        shifted[direction:, :] = alpha[-1:, :]
                elif direction > 0:
                    shifted[:, :direction] = alpha[:, 0:1]
                else:
                    shifted[:, direction:] = alpha[:, -1:]
                accumulator = np.minimum(accumulator, shifted)
        alpha = accumulator
    return Image.fromarray(alpha, "L")


def _screen_matte(red, green, blue, screen, key, sharp=SHARP_MODE_DEFAULT):
    """Keylight screen matte: 1=объект, 0=фон."""
    dominance = _key_dom(red, green, blue, key, screen)
    key_strength = max(
        float(
            screen[key]
            - max(screen[(key + 1) % 3], screen[(key + 2) % 3])
        ),
        1e-3,
    )
    screen_amount = np.clip(dominance / key_strength, 0.0, 1.0)
    matte = 1.0 - screen_amount
    clip_black = (
        CLIP_BLACK_BLUE_SHARP if sharp else CLIP_BLACK_BLUE
    ) if key == 2 else CLIP_BLACK
    return np.clip(
        (matte - clip_black) / (CLIP_WHITE - clip_black), 0.0, 1.0
    )


def composite_checker(rgba, cell=12):
    """Наложить RGBA на шахматку для диагностического превью."""
    w, h = rgba.size
    arr = np.asarray(rgba, dtype=np.float32)
    rgb = arr[..., :3] / 255.0
    alpha = arr[..., 3:4] / 255.0
    yy, xx = np.mgrid[0:h, 0:w]
    checker = (((xx // cell) + (yy // cell)) % 2)[..., None].astype(np.float32)
    background = np.repeat(0.6 + 0.25 * checker, 3, axis=2)
    composite = rgb * alpha + background * (1.0 - alpha)
    return Image.fromarray((composite * 255).astype(np.uint8), "RGB")
