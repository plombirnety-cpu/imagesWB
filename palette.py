# -*- coding: utf-8 -*-
"""palette.py — извлечение палитры цветов из иллюстрации для типографики v3
(docs/PRINT_STYLE_GUIDE.md, раздел 1).

Зачем отдельный модуль (не внутри typography.py, см. раздел 6.2 стайлгайда): чтобы не
тянуть numpy-тяжёлую k-means-логику в typography.py, от которого зависит стабильный
v1/v2-путь, и чтобы палитру можно было переиспользовать вне типографики (например для
подбора цвета мокап-фона в других модулях студии).

sklearn НЕ используется (не в requirements.txt проекта) — свой Lloyd's k-means на
numpy, детерминированный (фиксированный random_state=42), как явно разрешает стайлгайд.
"""
import colorsys

import numpy as np
from PIL import Image

# Fallback-палитра при полном отказе извлечения (пустая альфа, k-means не сошёлся) —
# нейтральный тёплый белый / тёмно-фиолетовый дуотон (раздел 6.3 стайлгайда). НЕ падаем
# конвейер целиком из-за одного диеката.
_FALLBACK_PALETTE = [(235, 235, 230), (35, 30, 40), (235, 235, 230), (20, 18, 24)]

# Тёплый белый (не чистый #FFFFFF — резче на печати), фиксированный fallback для роли
# light/dark, если в самой палитре нет достаточно светлого/тёмного цвета (раздел 1.2).
_LIGHT_FIXED = (245, 245, 240)
_DARK_FIXED = (18, 14, 20)

_ALPHA_THRESHOLD = 20  # порог alpha>20 — убирает мусорные полупрозрачные края (1.1.1)
_MIN_AREA_FRAC = 0.04  # отсев шумовых кластеров < 4% площади (1.1.5)
_MIN_AREA_FRAC_RELAXED = 0.02  # ослабленный порог, если после отсева осталось < 3 (1.1.5)
_KMEANS_SEED = 42
_KMEANS_N_INIT = 4
_KMEANS_MAX_ITER = 15
_KMEANS_TOL = 1e-2


def _rgb_to_hls(rgb: tuple) -> tuple:
    """RGB (0-255) -> (H в градусах 0-360, L 0-1, S 0-1) через colorsys (HLS)."""
    r, g, b = (c / 255.0 for c in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360.0, l, s


def _kmeans_lloyd(points: np.ndarray, k: int, seed: int = _KMEANS_SEED,
                   n_init: int = _KMEANS_N_INIT, max_iter: int = _KMEANS_MAX_ITER,
                   tol: float = _KMEANS_TOL) -> tuple:
    """Ручная реализация Lloyd's k-means (без sklearn), детерминированная при
    фиксированном seed. points: (N, 3) float32 RGB. Возвращает (centers, labels) —
    центры (k, 3) и метки кластера на точку (N,), от лучшего из n_init прогонов
    (наименьшая инерция = сумма квадратов расстояний до своего центра)."""
    n = points.shape[0]
    rng = np.random.RandomState(seed)
    best_centers, best_labels, best_inertia = None, None, None

    for init_i in range(n_init):
        # k-means++ -подобная инициализация было бы точнее, но для 3-4 кластеров и
        # детерминизма достаточно случайной выборки точек с фиксированным seed на
        # каждый прогон n_init (разные seed -> разные старты -> берём лучший).
        idx = rng.choice(n, size=min(k, n), replace=False)
        centers = points[idx].copy()

        for _ in range(max_iter):
            dists = np.linalg.norm(points[:, None, :] - centers[None, :, :], axis=2)
            labels = np.argmin(dists, axis=1)
            new_centers = centers.copy()
            for ci in range(centers.shape[0]):
                mask = labels == ci
                if mask.any():
                    new_centers[ci] = points[mask].mean(axis=0)
            shift = np.linalg.norm(new_centers - centers)
            centers = new_centers
            if shift < tol:
                break

        dists = np.linalg.norm(points[:, None, :] - centers[None, :, :], axis=2)
        labels = np.argmin(dists, axis=1)
        inertia = float(np.sum((points - centers[labels]) ** 2))
        if best_inertia is None or inertia < best_inertia:
            best_inertia, best_centers, best_labels = inertia, centers, labels

    return best_centers, best_labels


def extract_palette(rgba: "Image.Image", n: int = 4) -> list:
    """Извлечь до `n` доминантных цветов из RGBA-иллюстрации, отсортированных по
    убыванию занимаемой площади (docs/PRINT_STYLE_GUIDE.md раздел 1.1).

    Возвращает список RGB-кортежей int (3-4 цвета обычно). При любом сбое (пустая
    альфа, k-means не сошёлся) — фиксированная нейтральная fallback-палитра, warning
    в консоль, НЕ бросает исключение (раздел 6.3)."""
    try:
        rgba = rgba.convert("RGBA")
        w, h = rgba.size
        if w == 0 or h == 0:
            raise ValueError("пустое изображение")

        # Понизить разрешение перед кластеризацией (раздел 1.1.2) — не гонять k-means
        # по полноразмерному изображению.
        target_w = 128
        target_h = max(1, int(h * target_w / w)) if w > target_w else h
        small = rgba.resize((min(target_w, w), max(1, target_h))) if w > target_w else rgba

        arr = np.array(small)
        alpha = arr[:, :, 3]
        mask = alpha > _ALPHA_THRESHOLD
        pixels = arr[mask][:, :3].astype(np.float32)

        if pixels.shape[0] < max(3, n):
            raise ValueError("недостаточно непрозрачных пикселей для кластеризации")

        centers, labels = _kmeans_lloyd(pixels, k=n)
        total = labels.shape[0]

        counts = np.bincount(labels, minlength=centers.shape[0])
        order = np.argsort(-counts)  # по убыванию площади

        def _build(min_frac: float) -> list:
            out = []
            for ci in order:
                frac = counts[ci] / total
                if frac < min_frac:
                    continue
                rgb = tuple(int(round(v)) for v in np.clip(centers[ci], 0, 255))
                out.append(rgb)
            return out

        result = _build(_MIN_AREA_FRAC)
        if len(result) < 3:
            result = _build(_MIN_AREA_FRAC_RELAXED)
        if len(result) < 3:
            # Совсем плоская/однородная картинка — вернуть что есть, дополнив fallback.
            result = (result + _FALLBACK_PALETTE)[:max(3, len(result))]

        return result[:n] if len(result) >= 3 else _FALLBACK_PALETTE
    except Exception as e:  # noqa: BLE001 — извлечение палитры не должно ронять конвейер
        print(f"!! extract_palette упал: {e} — использую нейтральный fallback",
              flush=True)
        return list(_FALLBACK_PALETTE)


class PaletteRoles:
    """Роли палитры (docs/PRINT_STYLE_GUIDE.md раздел 1.2): dominant/accent/light/dark.
    Вычисляются один раз из результата extract_palette, дальше переиспользуются всеми
    режимами typography_v3, чтобы не пересчитывать роли в каждой функции отдельно."""

    __slots__ = ("dominant", "accent", "light", "dark", "palette")

    def __init__(self, palette: list):
        self.palette = list(palette) if palette else list(_FALLBACK_PALETTE)
        self.dominant, self.accent = _pick_dominant_accent(self.palette)
        self.light = _pick_light(self.palette)
        self.dark = _pick_dark(self.palette)


def _hue_dist(h1: float, h2: float) -> float:
    """Циклическое расстояние по HUE в градусах (0-180)."""
    d = abs(h1 - h2) % 360.0
    return min(d, 360.0 - d)


def _pick_dominant_accent(palette: list) -> tuple:
    """dominant = самый крупный по площади (palette[0], список уже отсортирован).
    accent = второй по площади, максимально отличный по HUE от dominant; если второй
    слишком близок (Hue<25° И L<20% разницы одновременно) — берём третий (раздел 1.2)."""
    if not palette:
        return _FALLBACK_PALETTE[0], _FALLBACK_PALETTE[1]
    dominant = palette[0]
    if len(palette) == 1:
        return dominant, dominant

    dom_h, dom_l, _ = _rgb_to_hls(dominant)
    candidates = palette[1:]
    accent = candidates[0]
    for cand in candidates:
        ch, cl, _ = _rgb_to_hls(cand)
        too_close = _hue_dist(dom_h, ch) < 25.0 and abs(cl - dom_l) < 0.20
        if not too_close:
            accent = cand
            break
    else:
        # Все кандидаты слишком близки — берём третий, если есть, иначе второй как есть.
        accent = candidates[min(1, len(candidates) - 1)]
    return dominant, accent


def _pick_light(palette: list) -> tuple:
    """Самый светлый цвет палитры (L>70%); если нет — фиксированный тёплый белый."""
    best, best_l = None, -1.0
    for c in palette:
        _, l, _ = _rgb_to_hls(c)
        if l > best_l:
            best, best_l = c, l
    if best is not None and best_l > 0.70:
        return best
    return _LIGHT_FIXED


def _pick_dark(palette: list) -> tuple:
    """Самый тёмный цвет палитры (L<20%); если нет — фиксированный почти-чёрный."""
    best, best_l = None, 2.0
    for c in palette:
        _, l, _ = _rgb_to_hls(c)
        if l < best_l:
            best, best_l = c, l
    if best is not None and best_l < 0.20:
        return best
    return _DARK_FIXED


def _luminance(rgb: tuple) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def local_luminance(rgba: "Image.Image", box: tuple) -> float:
    """Средняя яркость (luminance) НЕПРОЗРАЧНЫХ пикселей области rgba внутри
    box=(x0,y0,x1,y1) — используется для правила контраста 1.3 (заливка текста vs.
    область фигуры/фона ПОД текстом).

    ВАЖНО: пиксели с alpha=0 ИСКЛЮЧАЮТСЯ из расчёта, а не считаются чёрными. Текст
    (цитата/подвал) обычно рисуется НИЖЕ фигуры, на прозрачном фоне диеката — после
    печати на футболке там будет цвет ткани (обычно светлый), не чёрный. PIL
    Image.convert('RGB') для RGBA даёт (0,0,0) на полностью прозрачных пикселях
    (реальный баг, пойман визуально: подвал под Итачи, где под текстом нет фигуры,
    считался 'тёмным местом' с luminance~0 и получал светлую заливку light —
    формально по правилу контраста верно для ЧЁРНОГО фона, но неверно для
    прозрачного фона диеката/светлой футболки). Если ВСЕ пиксели области прозрачны
    — считаем область светлой (нейтральный дефолт "светлая футболка", 235.0)."""
    x0, y0, x1, y1 = box
    x0 = max(0, min(x0, rgba.width))
    x1 = max(x0 + 1, min(x1, rgba.width))
    y0 = max(0, min(y0, rgba.height))
    y1 = max(y0 + 1, min(y1, rgba.height))
    crop = rgba.crop((x0, y0, x1, y1))
    arr = np.array(crop).astype(np.float32)
    if arr.size == 0 or arr.shape[2] < 4:
        return 255.0
    alpha_mask = arr[:, :, 3] > 20
    if not alpha_mask.any():
        return 235.0  # нейтральный дефолт "светлая футболка" — не считаем прозрачное чёрным
    rgb = arr[:, :, :3]
    lum = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    return float(lum[alpha_mask].mean())


def contrast_fill_stroke(roles: "PaletteRoles", local_lum: float) -> tuple:
    """Правило контраста 1.3: выбор (fill, stroke) для текста над областью локальной
    яркости `local_lum`. Возвращает (fill_rgb, stroke_rgb) — ВСЕГДА из палитры (или её
    fixed-fallback ролей light/dark), никогда захардкоженный чёрный/белый напрямую."""
    if local_lum > 140:
        # Светло -> текст тёмный (dark, либо самый тёмный из dominant/accent).
        fill_candidates = [roles.dark, roles.dominant, roles.accent]
        fill = min(fill_candidates, key=_luminance)
        stroke = roles.light
    else:
        # Темно -> текст светлый (light, либо самый светлый из dominant/accent).
        fill_candidates = [roles.light, roles.dominant, roles.accent]
        fill = max(fill_candidates, key=_luminance)
        stroke = roles.dark

    # Никогда не заливать цветом с разницей luminance < 35 от локального фона —
    # принудительно взять оставшийся крайний цвет палитры (light/dark фикс).
    if abs(_luminance(fill) - local_lum) < 35:
        fill = roles.dark if local_lum > 140 else roles.light
    return fill, stroke


def alternate_word_colors(words: list, roles: "PaletteRoles") -> list:
    """Чередование цветов слов ПО СЛОВАМ (раздел 1.4): [accent, dominant, accent, ...],
    индекс = i % 2, начиная с accent (i=0 -> accent). Возвращает список RGB-кортежей
    параллельный `words` (даже для < 3 слов — чередование применяется всегда)."""
    palette_pair = [roles.accent, roles.dominant]
    return [palette_pair[i % 2] for i in range(len(words))]


def block_colors(n_blocks: int, roles: "PaletteRoles") -> list:
    """Чередование цветов БЛОКОВ подвала (раздел 1.5): [dominant, accent, dominant, ...]
    — начинается с dominant (в отличие от alternate_word_colors, который начинается с
    accent — так задаёт стайлгайд отдельно для блоков подвала)."""
    palette_pair = [roles.dominant, roles.accent]
    return [palette_pair[i % 2] for i in range(n_blocks)]
