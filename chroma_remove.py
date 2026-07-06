# -*- coding: utf-8 -*-
"""chroma_remove.py — быстрый хромакей-вырез фона для SaaS (вместо тяжёлого AI).

Фон у нас ИЗВЕСТНЫЙ, плоский, синтетический (генерим дизайн на чистой МАДЖЕНТЕ
#FF00FF). Для такого фона классический color-difference хромакей и чище, и в
30-100 раз быстрее AI-вырезки: ~0.05-0.2с на CPU, без модели, ~50МБ RAM.

Алгоритм (по ресёрчу): цвет фона берём из рамки кадра -> расстояние пикселя до
него в плоскости CrCb -> плавная альфа по двум допускам -> Gaussian-перо ->
деспилл (гасим маджента-кайму) только в полупрозрачной зоне края.

Маджента, а не зелёный — чтобы НЕ вырезать зелёные элементы аниме (волосы, аура,
неон). Страховка: если внутри субъекта вылезла большая прозрачная дыра (редкий
маджента-в-дизайне) — откат на rembg (isnet-anime).
"""
import cv2
import numpy as np
from PIL import Image


def _border_key(rgb: np.ndarray) -> np.ndarray:
    """Медианный цвет рамки кадра = цвет фона-ключа."""
    h, w = rgb.shape[:2]
    b = max(2, min(h, w) // 50)
    px = np.concatenate([
        rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
        rgb[:, :b].reshape(-1, 3), rgb[:, -b:].reshape(-1, 3),
    ])
    return np.median(px, axis=0)


def _clean_alpha(a01: np.ndarray, min_area: int, smooth: float = 1.0) -> np.ndarray:
    """Чистка маски с СОХРАНЕНИЕМ мягкого края (для хвостов/пламени/волос — без
    рваных зубцов). Фон строго прозрачный (дымка -> 0), спеклы и их полупрозрачное
    гало убираются, мелкие дырки заливаются, но РЕАЛЬНЫЙ край остаётся с мягким АА
    (НЕ бинаризуем в 0/255)."""
    h, w = a01.shape
    a = a01.astype(np.float32).copy()
    a[a < 0.12] = 0.0  # убрать фон-дымку (полупрозрачный фон -> 0)

    # 1) despeckle: оставить только крупные компоненты (по ядру), вместе с их мягким
    # краем (dilate); всё прочее (мелкие куски + их гало) -> 0.
    core = (a >= 0.4).astype(np.uint8)
    n, lbl, st, _ = cv2.connectedComponentsWithStats(core, 8)
    keep = np.zeros((h, w), np.uint8)
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] >= min_area:
            keep[lbl == i] = 1
    keepd = cv2.dilate(keep, np.ones((9, 9), np.uint8)) > 0
    a[~keepd] = 0.0

    # 2) залить мелкие дырки внутри субъекта
    inv = (a < 0.4).astype(np.uint8)
    n2, lbl2, st2, _ = cv2.connectedComponentsWithStats(inv, 8)
    border = set(np.unique(np.concatenate(
        [lbl2[0, :], lbl2[-1, :], lbl2[:, 0], lbl2[:, -1]])).tolist())
    for i in range(1, n2):
        if i not in border and st2[i, cv2.CC_STAT_AREA] < min_area * 6:
            a[lbl2 == i] = 1.0

    # 3) ЗАЛИВКА КРАЁВ: дизайн НЕПРОЗРАЧНЫЙ до контура (порог 0.4), полупрозрачным
    # остаётся лишь тончайший АА (~0.6px) на самом краю — убирает «туманность»/дымку
    # по краям, но без зубцов.
    solid = (a >= 0.40).astype(np.float32)
    a = cv2.GaussianBlur(solid, (0, 0), 0.6)
    return np.clip(a * 255.0, 0, 255).astype(np.uint8)


def remove_chroma(img_pil: Image.Image, tol_a: float = 12.0, tol_b: float = 48.0,
                  smooth: float = 1.3, despill: bool = True,
                  min_area_frac: float = 3e-5) -> Image.Image:
    """RGB на чистом фоне-ключе -> RGBA. Жёсткая вырезка по контурам + сглаживание."""
    rgb = np.array(img_pil.convert("RGB"))
    h, w = rgb.shape[:2]
    key_rgb = _border_key(rgb).astype(np.uint8)
    ycc = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb).astype(np.float32)
    key = cv2.cvtColor(key_rgb.reshape(1, 1, 3), cv2.COLOR_RGB2YCrCb)[0, 0].astype(np.float32)

    d = np.sqrt((ycc[:, :, 1] - key[1]) ** 2 + (ycc[:, :, 2] - key[2]) ** 2)
    a = np.clip((d - tol_a) / float(tol_b - tol_a), 0.0, 1.0)  # 0=фон, 1=субъект

    alpha = _clean_alpha(a, max(12, int(min_area_frac * h * w)), smooth)

    out = rgb.astype(np.int16)
    if despill:
        # Деспилл маджента-каймы только в зоне края: маджента = высокие R и B при
        # низком G. Гасим избыток min(R,B)-G. Нутро не трогаем.
        band = (alpha > 10) & (alpha < 245)
        r, g, bl = out[:, :, 0], out[:, :, 1], out[:, :, 2]
        s = np.clip(np.minimum(r, bl) - g, 0, 255)
        m = band & (s > 0)
        r[m] = np.clip(r[m] - s[m], 0, 255)
        bl[m] = np.clip(bl[m] - s[m], 0, 255)

    rgba = np.dstack([np.clip(out, 0, 255).astype(np.uint8), alpha])
    return Image.fromarray(rgba, "RGBA")


def _interior_hole(rgba: Image.Image, frac: float = 0.02) -> bool:
    """Большая прозрачная «дыра» ВНУТРИ субъекта (не касается краёв) = ключ выел
    элемент дизайна -> сигнал на откат к rembg."""
    a = np.array(rgba)[:, :, 3]
    transp = (a < 40).astype(np.uint8)
    num, lbl = cv2.connectedComponents(transp)
    border = set(np.unique(np.concatenate([lbl[0, :], lbl[-1, :], lbl[:, 0], lbl[:, -1]])).tolist())
    thr = frac * a.shape[0] * a.shape[1]
    for c in range(1, num):
        if c in border:
            continue
        if int((lbl == c).sum()) > thr:
            return True
    return False


def remove_bg_floodfill(img_pil: Image.Image, tol: float = 42.0,
                        smooth: float = 1.3, min_area_frac: float = 3e-5) -> Image.Image:
    """Дизайн на БЕЛОМ -> убираем только белый фон, СВЯЗАННЫЙ с краями кадра
    (заливка от рамки). Всё внутри дизайна (голова, белые детали) остаётся —
    не выедается, в отличие от rembg. Цветной каймы нет (фон белый)."""
    rgb = np.array(img_pil.convert("RGB"))
    h, w = rgb.shape[:2]
    key = _border_key(rgb).astype(np.uint8)
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    keylab = cv2.cvtColor(key.reshape(1, 1, 3), cv2.COLOR_RGB2LAB)[0, 0].astype(np.float32)
    dist = np.sqrt(((lab - keylab) ** 2).sum(axis=2))
    bgish = (dist < tol).astype(np.uint8)  # «как фон» (близко к белому из рамки)

    # оставить фоном только белое, связанное с краями кадра
    num, lbl = cv2.connectedComponents(bgish, connectivity=8)
    border = set(np.unique(np.concatenate(
        [lbl[0, :], lbl[-1, :], lbl[:, 0], lbl[:, -1]])).tolist()) - {0}
    bg = np.isin(lbl, list(border)) if border else np.zeros((h, w), bool)

    a01 = (~bg).astype(np.float32)  # 1 = дизайн
    alpha = _clean_alpha(a01, max(12, int(min_area_frac * h * w)), smooth)
    return Image.fromarray(np.dstack([rgb, alpha]).astype(np.uint8), "RGBA")


def harden(rgba_pil: Image.Image, smooth: float = 1.3, min_area_frac: float = 3e-5) -> Image.Image:
    """Жёсткая чистка готовой RGBA (напр. от rembg): бинаризация по контурам,
    убрать спеклы/мелкие дырки, фон строго прозрачный (без дымки), АА края."""
    arr = np.array(rgba_pil.convert("RGBA"))
    h, w = arr.shape[:2]
    a01 = arr[:, :, 3].astype(np.float32) / 255.0
    cleaned = _clean_alpha(a01, max(12, int(min_area_frac * h * w)), smooth)
    return Image.fromarray(np.dstack([arr[:, :, :3], cleaned]), "RGBA")


def cutout_rembg(img_pil: Image.Image, mag_tol: float = 52.0) -> Image.Image:
    """Дизайн на МАДЖЕНТЕ -> rembg даёт силуэт (фон контрастный, голову не спутает),
    ПЛЮС добиваем мадженту по цвету (запертые карманы), деспилл маджента-каймы/ободка
    в широкой зоне + подрезка загрязнённого внешнего пикселя края (эрозия) -> мягкая
    чистка. Без цветной каймы, розовых пятен и фиолетового ободка."""
    from bg_removal import cut_out
    rgb = np.array(img_pil.convert("RGB"))
    h, w = rgb.shape[:2]
    key = _border_key(rgb).astype(np.uint8)
    keyy = cv2.cvtColor(key.reshape(1, 1, 3), cv2.COLOR_RGB2YCrCb)[0, 0].astype(np.float32)
    ycc = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb).astype(np.float32)
    dist = np.sqrt((ycc[:, :, 1] - keyy[1]) ** 2 + (ycc[:, :, 2] - keyy[2]) ** 2)

    a_rembg = np.array(cut_out(img_pil).convert("RGBA"))[:, :, 3].astype(np.float32) / 255.0
    # КОЛОР-КЕЙ: всё, что ДАЛЕКО от мадженты — это дизайн (вытянутые руки, эффекты, клочья,
    # которые rembg отрезает как «не объект»). СОЮЗ rembg ∪ колор-кей: держим пиксель, если
    # ХОТЯ БЫ один метод считает его дизайном — поэтому конечности и эффекты НЕ теряются,
    # а глоу-края (которые чистый ключ подъел бы) держит rembg.
    a_color = np.clip((dist - 12.0) / 36.0, 0.0, 1.0)
    a = np.maximum(a_rembg, a_color)
    a[dist < mag_tol] = 0.0  # но чистую мадженту (фон + запертые карманы) всё равно убрать

    out = rgb.astype(np.int16)
    # ГЛОБАЛЬНЫЙ деспилл маджента-тинта (R,B>G) по всему видимому дизайну — гасит и
    # пинк-свечение ВНУТРИ (клякса у руки), и кайму. Тёплые цвета (оранж/красный/кожа,
    # Cb низкий) далеко от мадженты — s<=0, не трогаются.
    r, g, bl = out[:, :, 0], out[:, :, 1], out[:, :, 2]
    s = np.clip(np.minimum(r, bl) - g, 0, 255)
    m = (a > 0.02) & (s > 0)
    r[m] = np.clip(r[m] - s[m], 0, 255)
    bl[m] = np.clip(bl[m] - s[m], 0, 255)

    # Подрезать загрязнённый внешний пиксель края (~1px) — срезает остаточный ободок.
    a = cv2.erode(a, np.ones((3, 3), np.float32))

    alpha = _clean_alpha(a, max(12, int(3e-5 * h * w)), 1.0)
    return Image.fromarray(np.dstack([np.clip(out, 0, 255).astype(np.uint8), alpha]), "RGBA")


def cutout_green(img_pil: Image.Image, tol: float = 52.0) -> Image.Image:
    """Хромакей «по кнопке» (green ИЛИ blue — определяется по цвету рамки) — Keylight-
    эквивалент: screen colour key + spill suppression + matte cleanup. Союз rembg∪ключ
    бережёт руки/эффекты, despill гасит спилл ФОНОВОГО канала (G для зелёного, B для
    синего), эрозия+заливка краёв дают жёсткий контур. Отдаёт прозрачную RGBA."""
    from bg_removal import cut_out
    rgb = np.array(img_pil.convert("RGB"))
    h, w = rgb.shape[:2]
    key = _border_key(rgb).astype(np.uint8)
    keyy = cv2.cvtColor(key.reshape(1, 1, 3), cv2.COLOR_RGB2YCrCb)[0, 0].astype(np.float32)
    ycc = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb).astype(np.float32)
    dist = np.sqrt((ycc[:, :, 1] - keyy[1]) ** 2 + (ycc[:, :, 2] - keyy[2]) ** 2)

    a_rembg = np.array(cut_out(img_pil).convert("RGBA"))[:, :, 3].astype(np.float32) / 255.0
    a_color = np.clip((dist - 12.0) / 36.0, 0.0, 1.0)  # 1=далеко от зелёного (дизайн)
    a = np.maximum(a_rembg, a_color)                   # союз: держим руки/эффекты
    a[dist < tol] = 0.0                                # убрать зелёный (фон + карманы)

    # DESPILL по каналу ФОНА (адаптивно, глобально): green-экран -> гасим избыток G над
    # max(R,B); blue-экран -> избыток B над max(R,G). Канал берём по доминанте цвета рамки.
    out = rgb.astype(np.int16)
    r, g, bl = out[:, :, 0], out[:, :, 1], out[:, :, 2]
    if int(np.argmax(key)) == 2:  # СИНИЙ экран
        s = np.clip(bl - np.maximum(r, g), 0, 255)
        m = (a > 0.02) & (s > 0)
        bl[m] = np.clip(bl[m] - s[m], 0, 255)
    else:                         # ЗЕЛЁНЫЙ экран (по умолчанию)
        s = np.clip(g - np.maximum(r, bl), 0, 255)
        m = (a > 0.02) & (s > 0)
        g[m] = np.clip(g[m] - s[m], 0, 255)

    a = cv2.erode(a, np.ones((3, 3), np.float32))      # подрезать загрязнённый край
    alpha = _clean_alpha(a, max(12, int(3e-5 * h * w)), 1.0)
    return Image.fromarray(np.dstack([np.clip(out, 0, 255).astype(np.uint8), alpha]), "RGBA")


def cutout(img_pil: Image.Image) -> Image.Image:
    """Готовый прозрачный PNG. Хромакей; при выеденной дыре — откат на rembg."""
    res = remove_chroma(img_pil)
    if _interior_hole(res):
        try:
            from bg_removal import cut_out
            return harden(cut_out(img_pil))
        except Exception:  # noqa: BLE001
            pass
    return res
