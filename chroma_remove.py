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

# Эталонные RGB хромакея проекта — те же значения, что art_director._chroma_bg
# подставляет в промпт (hex 00B140 / 0047FF) и batch_print._CHROMA_REF_RGB
# использует в QC-гейте цвета рамки. Дублируются здесь (а не импортируются из
# batch_print), чтобы не заводить циклический импорт batch_print <-> chroma_remove.
CHROMA_REF_RGB = {"green": (0, 177, 64), "blue": (0, 71, 255)}


def _border_key(rgb: np.ndarray) -> np.ndarray:
    """Медианный цвет рамки кадра = цвет фона-ключа."""
    h, w = rgb.shape[:2]
    b = max(2, min(h, w) // 50)
    px = np.concatenate([
        rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
        rgb[:, :b].reshape(-1, 3), rgb[:, -b:].reshape(-1, 3),
    ])
    return np.median(px, axis=0)


def _ycc_of(rgb_tuple) -> np.ndarray:
    """RGB-тройка -> YCrCb (float32, форма (3,)) — общий помощник recolor-пути."""
    arr = np.array(rgb_tuple, dtype=np.uint8).reshape(1, 1, 3)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2YCrCb)[0, 0].astype(np.float32)


def _robust_bg_key(rgb: np.ndarray, ref_rgb: tuple,
                   prefilter_tol: float = 40.0, min_frac: float = 0.05) -> np.ndarray:
    """Цвет ФАКТИЧЕСКОГО фона-хромакея кадра: медиана пикселей рамки, ПРЕДВАРИТЕЛЬНО
    отфильтрованных близостью (CrCb) к ЭТАЛОННОМУ цвету запрошенного хромакея ref_rgb.

    Отличие от _border_key (голая медиана ВСЕЙ рамки): декор стиля, дотянувшийся до
    истинного края холста (инцидент «Зоро» 28_metal_cover, см. GOTCHAS команды),
    утаскивает голую медиану в грязный серо-синий тон декора — на реальном raw того
    инцидента _border_key даёт (19,82,152) при настоящем фоне (0,82,254): CrCb-дистанция
    ключа до фона ~57, перекраска по такому ключу не нашла бы фон вовсе. Фильтр по
    близости к эталону (prefilter_tol=40 — реальный фон nano-banana варьирует, но
    остаётся в пределах ~40 CrCb от эталона; белый/серый/чёрный декор лежит на 90-115)
    отбрасывает пиксели декора ДО взятия медианы. Если близких к эталону пикселей на
    рамке меньше min_frac (фон вообще не дотянулся до рамки — весь периметр в декоре)
    — возвращаем сам эталон ref_rgb как лучший доступный ключ."""
    h, w = rgb.shape[:2]
    b = max(2, min(h, w) // 50)
    px = np.concatenate([
        rgb[:b].reshape(-1, 3), rgb[-b:].reshape(-1, 3),
        rgb[:, :b].reshape(-1, 3), rgb[:, -b:].reshape(-1, 3),
    ]).astype(np.uint8)
    ycc = cv2.cvtColor(px.reshape(-1, 1, 3), cv2.COLOR_RGB2YCrCb)[:, 0, :].astype(np.float32)
    ref = _ycc_of(ref_rgb)
    dist = np.sqrt((ycc[:, 1] - ref[1]) ** 2 + (ycc[:, 2] - ref[2]) ** 2)
    near = px[dist < prefilter_tol]
    if len(near) < max(1, int(min_frac * len(px))):
        return np.array(ref_rgb, dtype=np.float64)
    return np.median(near.astype(np.float64), axis=0)


def recolor_bg(img_pil: Image.Image, src_chroma: str = "blue",
               target_rgb: tuple = CHROMA_REF_RGB["green"],
               tol_a: float = 12.0, tol_b: float = 48.0) -> Image.Image:
    """Перекрасить ТОЛЬКО фон-хромакей в target_rgb (эталонный зелёный по умолчанию),
    НЕ трогая пиксели фигуры/текста — режим green_only mega_batch_run: генерация шла
    на СИНЕМ хромакее (предохранитель art_director._chroma_bg для зелёных персонажей),
    а на выходе нужен единый эталонный зелёный фон БЕЗ вырезки.

    Маска — тот же колор-кей, что remove_chroma/cutout_green: CrCb-расстояние пикселя
    до ключа фона -> альфа фигуры a=0..1 по тем же калиброванным допускам (tol_a=12
    «чистый фон», tol_b=48 «уверенная фигура» — НЕ менять, калибровка проекта под
    nano-banana). Ключ — _robust_bg_key (см. выше): устойчив к декору, дотянувшемуся
    до рамки (голая медиана _border_key на таком кадре съезжает с фона).

    Итог по зонам альфы:
      a==1 (фигура)  — пиксель БИТ-В-БИТ исходный (не участвует ни в одном пересчёте);
      a==0 (фон)     — ровно target_rgb (эталон, без шума/градиента исходного фона);
      0<a<1 (кромка) — un-mix: из полупрозрачного пикселя ВЫЧИТАЕТСЯ вклад старого
                       ключа (пиксель = fg*a + key*(1-a) -> fg восстанавливается), и
                       той же долей подмешивается target_rgb — полутоновый край
                       переходит в зелёный БЕЗ синего ореола (простое смешение
                       «пиксель*а + зелёный*(1-a)» оставляло бы старый синий вклад
                       внутри пикселя).

    Никакого despill/чистки/морфологии alpha: это НЕ вырезка, фигура не редактируется.
    src_chroma — какой хромакей просил design (ключ эталона для _robust_bg_key)."""
    rgb = np.array(img_pil.convert("RGB"))
    ref_rgb = CHROMA_REF_RGB.get(src_chroma, CHROMA_REF_RGB["blue"])
    key_rgb = _robust_bg_key(rgb, ref_rgb)

    ycc = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb).astype(np.float32)
    key = _ycc_of(tuple(int(round(v)) for v in key_rgb))
    d = np.sqrt((ycc[:, :, 1] - key[1]) ** 2 + (ycc[:, :, 2] - key[2]) ** 2)
    a = np.clip((d - tol_a) / float(tol_b - tol_a), 0.0, 1.0)[:, :, None]

    src = rgb.astype(np.float32)
    key_f = key_rgb.astype(np.float32).reshape(1, 1, 3)
    target = np.array(target_rgb, dtype=np.float32).reshape(1, 1, 3)

    # un-mix кромки: восстановить цвет фигуры без вклада старого ключа, подмешать
    # target той же долей (a>0 гарантировано делителю зоной применения ниже).
    fg = np.clip((src - key_f * (1.0 - a)) / np.maximum(a, 1e-6), 0.0, 255.0)
    out = fg * a + target * (1.0 - a)
    out = np.where(a >= 1.0, src, out)      # фигура — бит-в-бит исходник
    out = np.where(a <= 0.0, np.broadcast_to(target, src.shape), out)  # фон — эталон
    return Image.fromarray(np.clip(np.round(out), 0, 255).astype(np.uint8), "RGB")


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

    a_color = np.clip((dist - 12.0) / 36.0, 0.0, 1.0)  # 1=далеко от зелёного (дизайн)
    a_rembg = None
    try:
        a_rembg = np.array(cut_out(img_pil).convert("RGBA"))[:, :, 3].astype(np.float32) / 255.0
    except Exception as e:  # noqa: BLE001 — ONNX может упасть по памяти (bad allocation)
        import gc
        gc.collect()
        try:  # повтор на уменьшенной копии: маска мягкая, даунскейл почти не вредит
            small = img_pil.resize((w * 3 // 5, h * 3 // 5), Image.LANCZOS)
            m = np.array(cut_out(small).convert("RGBA"))[:, :, 3]
            a_rembg = np.array(Image.fromarray(m).resize((w, h), Image.BILINEAR),
                               dtype=np.float32) / 255.0
            print("  ! rembg: полный кадр не влез в память, маска с даунскейла", flush=True)
        except Exception as e2:  # noqa: BLE001
            print(f"  ! rembg недоступен ({str(e2)[:100]}) — вырезка чистым колор-кеем "
                  f"(тёмные области у края допуска могут пострадать)", flush=True)
    if a_rembg is not None:
        a = np.maximum(a_rembg, a_color)               # союз: держим руки/эффекты
        # Жёсткое обнуление по близости к ключу выедало ТЁМНУЮ одежду со спиллом
        # (Кенпачи), хотя rembg уверенно держал тело. Поэтому: чистый ключ (карманы
        # фона, dist<16) — всегда фон; спорная зона (16..tol) — фон только там, где
        # rembg НЕ уверен, что это объект.
        a[(dist < tol) & (a_rembg < 0.6)] = 0.0
        a[dist < 16.0] = 0.0
    else:
        a = a_color
        a[dist < tol] = 0.0                            # убрать зелёный (фон + карманы)

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
