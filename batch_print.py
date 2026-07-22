#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_print.py — CLI-конвейер генерации принтов для футболок через платный
облачный API nano-banana (Google Gemini Image), зеркальный аналог self-hosted
comfyui-print-server/batch_print.py.

Пайплайн: тема -> Claude-арт-директор (идея + промпт + слоган + kana + signature_props
+ text_mode) -> nano-banana рисует дизайн на хромакей-фоне -> вырезка фона кодом ->
слоган накладывается кодом (typography.compose_text — типографика v2, режим text_mode
решает арт-директор ПО КОМПОЗИЦИИ каждого дизайна) -> 4 файла на дизайн в
out_batch/<run>/:
  NN_raw.png, NN_diecut.png (прозрачный), NN_ongreen.png (на ровном зелёном),
  NN_design.json (дамп идеи для воспроизводимости).

Использование:
  python batch_print.py --file themes.txt --format diecut
  python batch_print.py --file themes.txt --format cutout --text-style none
  python batch_print.py --file themes.txt --format diecut --chroma blue --text-style kana
  python batch_print.py --file themes.txt --format diecut   # text-style=auto по умолчанию
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# cp1251-консоль Windows падает UnicodeEncodeError на кандзи в print() (например,
# OCR-транскрипт с 竈門炭治郎 в _verify_text) — стандартная защита проекта.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

import cv2                    # noqa: E402
import numpy as np            # noqa: E402
from PIL import Image, ImageEnhance  # noqa: E402

import art_director            # noqa: E402
import character_ref           # noqa: E402
import chroma_remove           # noqa: E402
import config                  # noqa: E402
import meme_ref                # noqa: E402
import providers               # noqa: E402
import typography              # noqa: E402
import typography_v3           # noqa: E402
import upscale                 # noqa: E402


# Блок промпта, форсирующий рисование ПО РЕФЕРЕНСУ (см. character_ref.get_reference) —
# добавляется СПЕРЕДИ художественного промпта, когда для темы нашёлся каноничный
# портрет персонажа. Явно запрещает копировать позу/фон референса — референс только
# про личность персонажа (лицо/причёска/костюм), не про композицию.
_REFERENCE_PREFIX = (
    "Use the reference image as the EXACT character identity: same face, same "
    "hairstyle, same iconic outfit and accessories, and the character's signature "
    "weapon exactly as in canon. Redraw this character in a NEW pose and composition "
    "as described below. Do not copy the reference pose or background. "
)

_MAGAZINE_PRINT_STYLE_ID = "34_anime_magazine_cover"
_MAGAZINE_PRINT_PROMPT_SUFFIX = (
    " STREETWEAR DIE-CUT COMPOSITION — NOT A RECTANGULAR MAGAZINE COVER: treat the "
    "magazine typography only as a graphic language, never as a full-bleed page, "
    "poster, card or closed rectangle. Keep one connected, contained print silhouette "
    "with a clearly visible 6-8% clean CHROMA MOAT between every artwork/text element "
    "and all four canvas edges. The bust must taper and END ABOVE THE BOTTOM EDGE; "
    "never crop the torso, clothes, arms or typography against the bottom/side edges. "
    "MANDATORY SIGNATURE-EFFECT CRADLE: wrap the shoulders and lower bust in one bold, "
    "character-specific power effect (flames, lightning, shadow ribbons, ice shards, "
    "wind, petals, cursed energy or another canonical motif). Its asymmetric tongues, "
    "arcs and particles must form the irregular lower die-cut contour, like the approved "
    "Tanjiro print whose flames visually finish the bottom. Keep clean chroma visible "
    "below that effect and in both lower corners. All captions and seals float as "
    "separate outlined graphic islands inside the same contained silhouette."
)


def _is_magazine_print_style(design: dict) -> bool:
    return _MAGAZINE_PRINT_STYLE_ID in {
        str(design.get("style_id") or "").strip(),
        str(design.get("style_mix") or "").strip(),
    }


def _recover_image_other(
    design: dict,
    prompt: str,
    reference: Image.Image | None,
    rejection_count: int,
) -> tuple[str, Image.Image | None]:
    """Меняет отклонённый IMAGE_OTHER payload; тот же prompt/референс не повторяем."""
    if rejection_count <= 1:
        recovered = (
            prompt
            + " Rephrase the scene as a PG-rated, non-violent editorial portrait. "
              "No combat, injury, blood, threatening action or disturbing imagery. "
              "Preserve the character identity and requested print layout."
        )
        return recovered, None

    character = str(design.get("character_en") or "the named anime character").strip()
    title = str(design.get("title_en") or "").strip()
    props = str(design.get("signature_props") or "").strip()
    kana = str(design.get("kana") or "").strip()
    name_jp = str(design.get("name_jp") or "").strip()
    slogan = str(design.get("slogan") or "").strip()
    chroma = str(design.get("chroma") or "green").strip().lower()
    identity = f"{character} from {title}" if title else character
    safe = (
        f"Create a PG-rated, non-violent editorial portrait of {identity}. "
        f"Preserve the canon face, hair, clothing and these harmless identity details: {props}. "
        "The expression is confident and calm in a peaceful fashion-editorial pose, with "
        "only abstract decorative energy. Render polished retro-anime streetwear art in a "
        f"vertical 2:3 canvas on a perfectly uniform {chroma} chroma-key background. "
        f"Use the outlined display text {kana!r}, {name_jp!r} and {slogan!r} exactly once, "
        "away from the face."
    )
    if _is_magazine_print_style(design):
        safe += _MAGAZINE_PRINT_PROMPT_SUFFIX
    safe += (
        f" Use {chroma} only for one perfectly flat, uniform chroma-key field behind "
        "the contained artwork, reaching all canvas edges without scenery, gradients, "
        "paper, frames, halos or texture. Keep the artwork palette clearly different "
        "from the key color for clean removal."
    )
    return safe, None

# Блок промпта, форсирующий рисование ПО РЕФЕРЕНСУ ОРИГИНАЛА МЕМА (жалоба владельца
# 2026-07-11 — интернет-мемы, см. meme_ref.get_reference) — добавляется СПЕРЕДИ
# художественного промпта, когда для темы нашёлся файл data/meme_refs/<slug>.png.
# ПРИОРИТЕТНЕЕ character_ref (см. выбор reference ниже в render_design) — meme_ref
# указывает на ТОЧНУЮ картинку оригинала, а не на общий канон-портрет персонажа,
# формулировка сама требует превосходство референса над текстовым описанием.
_MEME_REFERENCE_PREFIX = (
    "Reproduce the meme subject EXACTLY as shown in the reference image — identical "
    "character/creature design, same colours, same proportions, same art style; this "
    "is a known viral meme, stay faithful to the reference, do NOT invent your own "
    "version. The reference image takes PRIORITY over the text description below "
    "wherever they might disagree — reinterpret the scene/composition/style requested "
    "in the text, but keep the meme subject itself identical to the reference. "
)

# Медальон-гибрид (typography_v3.ring_text, четвёртая задача) — ДВА независимых
# источника, которые оба включают режим:
#
# 1) БАНК СТИЛЕЙ (docs/STYLE_BANK.json, обычный путь через art_director.make_ideas):
#    design["style_id"]/["style_mix"] указывает на стиль с hybrid_ring_text=true
#    (сейчас единственный такой — "09_ring_medallion") — эту часть ПРОМПТА
#    (декоративное ПУСТОЕ кольцо без единой буквы) УЖЕ полностью строит
#    art_director.build_prompt/_style_bank_prompt_block (см. art_director.py, НЕ
#    дублируется здесь) — batch_print.render_design только детектирует режим (через
#    art_director._style_by_id) и накладывает КОЛЬЦЕВОЙ ТЕКСТ КОДОМ после вырезки.
#
# 2) ПРЯМЫЕ ПОЛЯ design (альтернативный источник ЗА ПРЕДЕЛАМИ банка стилей — напр.
#    style_madara.py-подобные скрипты, которые строят design-dict напрямую, минуя
#    art_director.make_ideas): design["style_id"]=="ring_medallion" (БЕЗ цифрового
#    префикса банка) ИЛИ непустое design["hybrid_ring_text"] — ЭТИ design НЕ проходят
#    через _style_bank_prompt_block (тот знает только банковские id вида "09_..."),
#    поэтому _RING_MEDALLION_PROMPT_SUFFIX добавляется здесь ЯВНО (см. ниже), чтобы
#    промпт всё равно попросил пустое кольцо — иначе для этих design фича была бы
#    наполовину рабочей (код рисует кольцевой текст, но генерация не была бы
#    предупреждена оставить кольцо пустым).
_RING_MEDALLION_PROMPT_SUFFIX = (
    " Around the entire figure, at an even radial distance, runs a thin decorative "
    "circular ring or medallion border (like a coin rim or emblem seal) framing the "
    "whole composition — ornamental, with fine engraved or embossed detail (beading, "
    "filigree, small studs or notches), matching the artwork's color palette. The ring "
    "is PURELY DECORATIVE AND CONTAINS NO LETTERING, NO WORDS, NO TEXT OF ANY KIND — "
    "leave the inside band of the ring visually clean (pattern/ornament only), any text "
    "for this design is added separately afterward. Do not write any words anywhere on "
    "the ring or the composition."
)

# Максимум ДОПОЛНИТЕЛЬНЫХ (fallback, БЕЗ текст-блока) генераций в text-fallback ветке
# render_design (пятнадцатый заход, регресс-фикс двойного текста) — каждая попытка
# проверяется _verify_no_text (OCR-контроль ОТСУТСТВИЯ значимого текста), не только
# цветовым QC-гейтом рамки. 2 — по прямому требованию задачи ("ещё одна фолбэк-
# попытка (максимум 2)"): первая fallback-генерация уже была неявной частью старого
# поведения, здесь этот бюджет посчитан явно как отдельный цикл.
_FALLBACK_NO_TEXT_MAX_ATTEMPTS = 2


def _hybrid_ring_via_style_bank(design: dict) -> bool:
    """design["style_id"]/["style_mix"] указывает на банковский стиль (docs/
    STYLE_BANK.json) с hybrid_ring_text=true — art_director.build_prompt УЖЕ сам
    добавляет инструкцию про пустое кольцо для этого пути, здесь только детекция."""
    for sid in (str(design.get("style_id") or "").strip(),
                str(design.get("style_mix") or "").strip()):
        if not sid:
            continue
        try:
            style = art_director._style_by_id(sid)
        except Exception:  # noqa: BLE001 — банк недоступен/битый, не должен ронять генерацию
            style = None
        if style and style.get("hybrid_ring_text"):
            return True
    return False


def _hybrid_ring_via_direct_fields(design: dict) -> bool:
    """design["style_id"]=="ring_medallion" (без банковского префикса) ИЛИ непустое
    design["hybrid_ring_text"] — альтернативный источник ВНЕ банка стилей (см. блок-
    комментарий выше), для которого промпт-суффикс добавляется явно в render_design."""
    return (design.get("style_id") == "ring_medallion"
            or bool(str(design.get("hybrid_ring_text") or "").strip()))


def _is_hybrid_ring_style(design: dict) -> bool:
    """design реально просит медальон-гибрид — банковский путь ИЛИ прямые поля
    (см. _hybrid_ring_via_style_bank/_hybrid_ring_via_direct_fields)."""
    return _hybrid_ring_via_style_bank(design) or _hybrid_ring_via_direct_fields(design)


# ── QC-гейт границ кадра (перенесено дословно из comfyui-print-server/batch_print.py) ──

# Эталонные RGB хромакея (те же значения, что art_director._chroma_bg подставляет в
# промпт и что batch_print.render_design кладёт под финальный ongreen-composite) в
# YCrCb — используются _border_chroma_coverage, чтобы отличить "рамка однотонная, но
# НЕ того цвета" (например модель нарисовала белую подложку/стикер-рамку вместо
# хромакея) от реального хромакея. CrCb, не RGB — устойчивее к яркостным вариациям
# самого хромакея между генерациями (тень/грейн), которых нанобанана иногда чуть
# подсвечивает светлее/темнее, но по цветности остаётся зелёным/синим.
_CHROMA_REF_RGB = {"green": (0, 177, 64), "blue": (0, 71, 255)}


def _ycrcb(rgb: tuple) -> np.ndarray:
    arr = np.array(rgb, dtype=np.uint8).reshape(1, 1, 3)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2YCrCb)[0, 0].astype(np.float32)


_CHROMA_REF_YCC = {c: _ycrcb(v) for c, v in _CHROMA_REF_RGB.items()}

# Порог расстояния (CrCb-плоскость) рамки кадра до эталонного цвета хромакея —
# КАЛИБРОВАНО на живых raw (2026-07-08, одиннадцатый заход): реальные "правильные"
# зелёные raw прошлых прогонов дают дистанцию рамки до эталона green RGB(0,177,64) в
# диапазоне ~9..63 (медиана рамки чуть темнее/desaturated эталона — обычная вариация
# nanobanana), белофонный кейс (модель нарисовала БЕЛЫЙ фон вместо хромакея,
# out_batch/20260708_205007/01_raw.png) даёт ~83.5 — зазор ~20 единиц. 70 — с запасом
# выше максимума реальных зелёных (63), заметно ниже белого outlier (83.5).
_CHROMA_COLOR_TOL = 70.0


def _border_chroma_coverage(img_rgb: Image.Image, tol: float = 52.0,
                            chroma: str = "green") -> float:
    """Доля пикселей рамки кадра (ширина 1% от min(W,H), сэмплинг каждые 4px),
    близких к цвету хромакея — QC-гейт против «персонаж/эффекты упираются в край
    кадра, хромакей-фона не осталось». tol=52 — то же калиброванное значение, что
    и в cutout_green (НЕ менять — подобрано под nanobanana).

    ВТОРОЙ гейт (одиннадцатый заход): даже если рамка ОДНОТОННАЯ (высокое coverage
    по расстоянию до МЕДИАНЫ САМОЙ рамки), она может быть однотонной НЕ ТОГО цвета —
    например модель нарисовала белую подложку/стикер-рамку вместо хромакея, а
    хромакей-цвет (green) просочился ВНУТРЬ дизайна как аура/свечение вокруг фигуры.
    Проверяем: расстояние МЕДИАНЫ рамки до ЭТАЛОННОГО RGB запрошенного chroma
    (_CHROMA_REF_YCC) в CrCb < _CHROMA_COLOR_TOL. Не сошлось -> coverage=0.0 (весь
    обычный ретрай-механизм render_design перегенерит попытку как есть, ничего
    дополнительно чинить не нужно)."""
    rgb = np.array(img_rgb.convert("RGB"))
    h, w = rgb.shape[:2]
    key = chroma_remove._border_key(rgb).astype(np.uint8)
    keyy = cv2.cvtColor(key.reshape(1, 1, 3), cv2.COLOR_RGB2YCrCb)[0, 0].astype(np.float32)

    ref_ycc = _CHROMA_REF_YCC.get(chroma, _CHROMA_REF_YCC["green"])
    color_dist = float(np.sqrt(
        (keyy[1] - ref_ycc[1]) ** 2 + (keyy[2] - ref_ycc[2]) ** 2))
    if color_dist >= _CHROMA_COLOR_TOL:
        print(f"  !! фон не хромакей: рамка ~{tuple(int(v) for v in key)} "
              f"(dist={color_dist:.1f} >= {_CHROMA_COLOR_TOL}), ожидался {chroma}",
              flush=True)
        return 0.0

    ycc = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb).astype(np.float32)
    dist = np.sqrt((ycc[:, :, 1] - keyy[1]) ** 2 + (ycc[:, :, 2] - keyy[2]) ** 2)

    b = max(1, int(round(min(h, w) * 0.01)))
    border_mask = np.zeros((h, w), dtype=bool)
    border_mask[:b, :] = True
    border_mask[-b:, :] = True
    border_mask[:, :b] = True
    border_mask[:, -b:] = True
    sample_mask = np.zeros((h, w), dtype=bool)
    sample_mask[::4, ::4] = True
    mask = border_mask & sample_mask

    total = int(mask.sum())
    if total == 0:
        return 0.0
    close = int((dist[mask] < tol).sum())
    return close / total


def _magazine_print_layout_quality(
    img_rgb: Image.Image,
    chroma: str = "green",
    tol: float = 52.0,
) -> tuple[bool, dict[str, float]]:
    """Style 34: хромакей должен отделять принт от КАЖДОГО края, особенно снизу.

    Общий `_border_chroma_coverage` усредняет четыре стороны: живые full-bleed
    «обложки» имели bottom=0, но aggregate≈0.62-0.72 и проходили hard-min=0.5.
    Здесь измеряем стороны отдельно на 2% полосе. Эталон Тандзиро даёт 1.0 по всем
    сторонам; дефектная партия 1116ed9302ac — 0.00-0.14 снизу.
    """
    rgb = np.array(img_rgb.convert("RGB"))
    h, w = rgb.shape[:2]
    key = chroma_remove._border_key(rgb).astype(np.uint8)
    keyy = cv2.cvtColor(key.reshape(1, 1, 3), cv2.COLOR_RGB2YCrCb)[0, 0].astype(np.float32)
    ref_ycc = _CHROMA_REF_YCC.get(chroma, _CHROMA_REF_YCC["green"])
    color_dist = float(np.sqrt(
        (keyy[1] - ref_ycc[1]) ** 2 + (keyy[2] - ref_ycc[2]) ** 2))
    if color_dist >= _CHROMA_COLOR_TOL:
        metrics = {"top": 0.0, "bottom": 0.0, "left": 0.0, "right": 0.0}
        return False, metrics

    ycc = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb).astype(np.float32)
    dist = np.sqrt((ycc[:, :, 1] - keyy[1]) ** 2 + (ycc[:, :, 2] - keyy[2]) ** 2)
    close = dist < tol
    band = max(1, int(round(min(h, w) * 0.02)))
    metrics = {
        "top": float(close[:band, :].mean()),
        "bottom": float(close[-band:, :].mean()),
        "left": float(close[:, :band].mean()),
        "right": float(close[:, -band:].mean()),
    }
    ok = (
        metrics["top"] >= 0.80
        and metrics["bottom"] >= 0.85
        and metrics["left"] >= 0.80
        and metrics["right"] >= 0.80
    )
    return ok, metrics


# ── QC-гейт масштаба фигуры (урок на мелких Маки/этикетке/Люси — персонаж терялся в
# углу/центре кадра, занимая малую долю высоты, диекат выглядел "мелким принтом на
# футболке") ──────────────────────────────────────────────────────────────────────

def _figure_bbox_height_frac(img_rgb: Image.Image, tol: float = 52.0,
                              chroma: str = "green") -> float:
    """Доля ВЫСОТЫ КАДРА, которую занимает bbox «не-фон» маски (то же расстояние в
    CrCb до цвета хромакея рамки, tol=52 — калибровка, общая с cutout_green/
    _border_chroma_coverage, НЕ дублирует тяжёлую rembg-вырезку — быстрая цветовая
    оценка ДОСТАТОЧНА для QC-гейта размера, точная альфа с rembg считается один раз
    позже в самой вырезке). Работает на СЫРОМ raw (RGB, до фактического
    chroma_remove.cutout_green) — вызывается внутри QC-цикла генерации на каждой
    попытке, поэтому должна быть дешёвой (без ML-вызова).

    Возвращает 0.0, если «не-фон» маска пуста (весь кадр — фон, разрешать не
    должны — но не делить на ноль)."""
    rgb = np.array(img_rgb.convert("RGB"))
    h, w = rgb.shape[:2]
    key = chroma_remove._border_key(rgb).astype(np.uint8)
    keyy = cv2.cvtColor(key.reshape(1, 1, 3), cv2.COLOR_RGB2YCrCb)[0, 0].astype(np.float32)
    ycc = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb).astype(np.float32)
    dist = np.sqrt((ycc[:, :, 1] - keyy[1]) ** 2 + (ycc[:, :, 2] - keyy[2]) ** 2)

    fg = dist >= tol
    rows = np.where(fg.any(axis=1))[0]
    if rows.size == 0:
        return 0.0
    bbox_h = int(rows[-1] - rows[0] + 1)
    return bbox_h / float(h)


def _figure_fills_frame(img_rgb: Image.Image, min_frac: float = None,
                         chroma: str = "green") -> tuple:
    """QC-гейт: (ok: bool, frac: float) — ok=False, если высота bbox фигуры МЕНЬШЕ
    config.FIGURE_MIN_FRAC (дефолт 0.55) доли высоты кадра — "фигура слишком мелкая",
    та же попытка бракуется и уходит в общий ретрай-цикл render_design, наравне с
    провалом border coverage / OCR."""
    frac_thr = config.FIGURE_MIN_FRAC if min_frac is None else min_frac
    frac = _figure_bbox_height_frac(img_rgb, chroma=chroma)
    return frac >= frac_thr, frac


def juice(rgba: Image.Image) -> Image.Image:
    """Сочность цвета: Color x1.15 + Contrast x1.05 ТОЛЬКО по RGB-каналам, альфа-канал
    сохраняется как есть (не участвует в enhance). Перенесено дословно из
    comfyui-print-server/batch_print.py."""
    rgb = rgba.convert("RGB")
    rgb = ImageEnhance.Color(rgb).enhance(1.15)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.05)
    out = rgb.convert("RGBA")
    out.putalpha(rgba.getchannel("A"))
    return out


# Доля площади КАДРА (не главного силуэта), при которой остров ЗАЩИЩЁН от удаления
# при protect_text_islands=True — буквы встроенного текста (TEXT_RENDER=image) у
# нижнего/бокового края фигуры часто рисуются как ОТДЕЛЬНЫЕ альфа-острова (не
# касаются основного силуэта персонажа), включая совсем мелкие (апостроф, точка,
# тонкая засечка) — старый порог min_frac считался ОТ areas.max() (главного силуэта),
# из-за чего целые буквы могли попасть под удаление как "мусор". 0.04% кадра —
# калибровка из задачи лида.
_TEXT_ISLAND_PROTECT_FRAC = 0.0004


def drop_small_islands(rgba: Image.Image, min_frac: float = 0.002,
                        protect_text_islands: bool = False) -> Image.Image:
    """Убрать мусорные острова после хромакей-вырезки (водяные знаки, крапинки по
    углам) — остаётся главный силуэт и всё крупнее min_frac от него. Перенесено
    дословно из comfyui-print-server/batch_print.py.

    protect_text_islands (десятый заход, TEXT_RENDER=image): когда встроенный текст
    ожидается на диекате, буквы у края фигуры могут лежать ОТДЕЛЬНЫМИ альфа-островами
    (не связаны с основным силуэтом) — при True остров НЕ удаляется, если его площадь
    >= _TEXT_ISLAND_PROTECT_FRAC ДОЛИ ВСЕГО КАДРА (не areas.max(), в отличие от
    обычного min_frac-порога), даже если по обычному критерию (< areas.max()*min_frac)
    он попал бы под удаление — иначе целые буквы/апостроф могли попасть под удаление
    как "мусорный остров" наравне с водяными знаками."""
    a = np.array(rgba.getchannel("A"))
    n, labels, stats, _ = cv2.connectedComponentsWithStats((a > 0).astype(np.uint8), 8)
    if n <= 2:
        return rgba
    areas = stats[1:, cv2.CC_STAT_AREA]
    frame_area = a.shape[0] * a.shape[1]
    protect_px = frame_area * _TEXT_ISLAND_PROTECT_FRAC

    def _should_kill(area_px: int) -> bool:
        if area_px >= areas.max() * min_frac:
            return False
        if protect_text_islands and area_px >= protect_px:
            return False
        return True

    kill = [i + 1 for i, ar in enumerate(areas) if _should_kill(ar)]
    if not kill:
        return rgba
    a[np.isin(labels, kill)] = 0
    out = rgba.copy()
    out.putalpha(Image.fromarray(a))
    return out


# ── OCR-контроль спеллинга (TEXT_RENDER=image, десятый заход) ───────────────────

def _normalize_for_compare(text: str) -> str:
    """Нормализация для сравнения OCR-транскрипта с ожидаемой фразой: верхний регистр,
    убрать пунктуацию/апострофы/переводы строк, схлопнуть повторные пробелы. Кандзи/
    катакана сравниваются КАК ЕСТЬ (regex ниже не трогает диапазон CJK/катакана —
    только убирает ASCII-пунктуацию и whitespace, не буквы любого алфавита).

    Юникод-нормализация NFC ПЕРВЫМ шагом (тринадцатый заход, строгая кана): OCR-модель
    иногда отдаёт дакутэн/хандакутэн КАК ОТДЕЛЬНЫЙ комбинирующий кодпоинт (U+3099/
    U+309A) вместо предкомпозированного глифа (например 'ハ'+U+3099 вместо готовой
    'バ') — оба визуально и семантически ОДНА И ТА ЖЕ кана, но как raw-строки они НЕ
    равны без NFC. Без нормализации это ложно проваливало бы сверку на графемах,
    которые реально совпадают. NFC НЕ схлопывает РАЗНЫЕ базовые каны (ハ и バ остаются
    разными символами после NFC — не тот же кейс, что "визуальная похожесть")."""
    import re as _re
    import unicodedata as _ud
    text = _ud.normalize("NFC", text)
    text = text.upper()
    text = _re.sub(r"[\r\n\t]+", " ", text)
    # Убираем пунктуацию/апострофы (ASCII), но НЕ буквы/цифры/CJK/катакану/пробелы.
    text = _re.sub(r"[^\w\s]", "", text, flags=_re.UNICODE)
    text = _re.sub(r"_", "", text)  # \w включает "_", слоганы его не содержат
    text = _re.sub(r"\s+", " ", text).strip()
    return text


# Диапазоны юникода японских глифов (кандзи CJK Unified + катакана + хирагана) —
# используется, чтобы отличить "фраза содержит японские глифы" (нужна поглифная
# строгая сверка дакутэн/хандакутэн) от обычной латиницы (substring-сверки достаточно).
_JP_GLYPH_RE_STR = r"[一-鿿぀-ゟ゠-ヿ]"


def _is_japanese_phrase(phrase: str) -> bool:
    """Фраза содержит хотя бы один японский глиф (кандзи/катакана/хирагана)."""
    import re as _re
    return bool(_re.search(_JP_GLYPH_RE_STR, phrase or ""))


def _glyph_by_glyph_match(expected: str, transcript: str) -> bool:
    """Строгая ПОГЛИФНАЯ сверка японской фразы: expected должна встречаться в
    transcript КАК ТОЧНАЯ ПОДПОСЛЕДОВАТЕЛЬНОСТЬ символов (после NFC-нормализации
    обеих строк) — дакутэн/хандакутэн различаются автоматически, потому что 'ハ' и
    'バ' остаются РАЗНЫМИ кодпоинтами после NFC (см. _normalize_for_compare докстринг).
    Пробелы из expected убираются перед сравнением (вертикальная колонка кандзи не
    содержит пробелов по построению art_director/style_madara, но защитный код на
    случай пробела в OCR-транскрипте не помешает). Пустая expected -> True (нечего
    сверять)."""
    import unicodedata as _ud
    exp = _ud.normalize("NFC", expected or "").replace(" ", "").replace("\n", "")
    tr = _ud.normalize("NFC", transcript or "").replace(" ", "").replace("\n", "")
    if not exp:
        return True
    return exp in tr


def _count_nonoverlapping(haystack: str, needle: str) -> int:
    """Число НЕПЕРЕСЕКАЮЩИХСЯ вхождений needle в haystack (str.count делает ровно
    это — считает от найденной позиции + len(needle), не пересекающиеся вхождения не
    складываются в завышенное число для коротких/повторяющихся фраз)."""
    if not needle:
        return 0
    return haystack.count(needle)


# Второй, узко сфокусированный OCR-вопрос ТОЛЬКО для японских фраз (тринадцатый
# заход, строгая кана) — общий _OCR_PROMPT ("transcribe ALL text") даёт модели
# слишком широкую задачу, дакутэн/хандакутэн на мелких глифах вертикальной колонки
# отдельно от общего транскрипта распознаются НЕНАДЁЖНО (модель сама не отдаёт их
# стабильно на общем проходе). Явно просим ТОЛЬКО вертикальную колонку, посимвольно.
_JP_COLUMN_PROMPT = (
    "Transcribe ONLY the vertical Japanese column of text visible in this image, "
    "glyph by glyph, top to bottom. Pay close attention to dakuten (゛) and "
    "handakuten (゜) diacritical marks — transcribe them accurately (for example "
    "distinguish ハ from バ from パ). Reply with the Japanese characters only, no "
    "romanization, no translation, no other text."
)


def _verify_text(image: Image.Image, expected_phrases: list) -> bool:
    """OCR-контроль спеллинга (TEXT_RENDER=image): дешёвая текстовая модель Gemini
    (providers.verify_text_in_image) транскрибирует ВЕСЬ текст на картинке, нормализует
    и сверяет — КАЖДАЯ непустая фраза из expected_phrases должна ВХОДИТЬ в транскрипт
    (нормализованное substring-вхождение) РОВНО ОДИН РАЗ. expected_phrases без непустых
    элементов -> True (нечего проверять, деградация не нужна). Любой сбой самого
    OCR-вызова (сеть/HTTP/нет ключа) -> False (трактуем как непройденную проверку — не
    блокируем ретрай/фолбэк, лучше лишний раз перегенерировать/откатиться на код, чем
    молча выпустить принт с потенциально неверным текстом).

    Одиннадцатый заход, живой баг (out_batch/20260708_211920/02): модель нарисовала
    фразу слогана ДВАЖДЫ (мелкая копия под фигурой + крупная копия внизу) — старая
    версия проверяла ТОЛЬКО substring-вхождение (`in`), дубль в транскрипте формально
    "содержит" ожидаемую фразу и проходил как OK, хотя реальный визуальный дефект
    (тот самый, который _text_render_block уже пытается запретить промптом) остаётся
    на финальной картинке. Теперь считаем непересекающиеся вхождения нормализованной
    фразы в транскрипте — больше одного вхождения -> провал (тот же ретрай/фолбэк
    механизм, что и при отсутствии фразы вовсе, ничего дополнительно чинить не
    нужно).

    Тринадцатый заход, строгая кана: КАЖДАЯ японская фраза (кандзи/катакана/хирагана,
    см. _is_japanese_phrase) дополнительно проверяется ВТОРЫМ, узко сфокусированным
    OCR-вызовом (_JP_COLUMN_PROMPT — "transcribe ONLY the vertical column, glyph by
    glyph"), поглифно сверенным (_glyph_by_glyph_match — дакутэн/хандакутэн различают
    ハ от バ автоматически после NFC). Общий транскрипт (первый вызов, substring-сверка
    выше) остаётся ПЕРВОЙ линией защиты для всех фраз (включая японские — если
    несовпадение уже видно в общем транскрипте, второй вызов всё равно потом уточнит);
    второй вызов — ДОПОЛНИТЕЛЬНАЯ, более надёжная сверка именно для японской графики,
    несовпадение по нему тоже проваливает всю проверку (уходит в тот же ретрай-цикл)."""
    phrases = [p for p in (expected_phrases or []) if str(p or "").strip()]
    if not phrases:
        return True
    try:
        transcript = providers.verify_text_in_image(image)
    except Exception as e:  # noqa: BLE001
        print(f"  !! OCR-контроль спеллинга упал: {e}", flush=True)
        return False
    norm_transcript = _normalize_for_compare(transcript)
    missing = [p for p in phrases if _normalize_for_compare(p) not in norm_transcript]
    duplicated = [p for p in phrases
                  if _count_nonoverlapping(norm_transcript, _normalize_for_compare(p)) > 1]

    # Строгая кана: японские фразы (кандзи/катакана/хирагана) проходят ДОПОЛНИТЕЛЬНУЮ
    # поглифную сверку через отдельный сфокусированный OCR-вызов — общий транскрипт
    # выше (single-pass "transcribe ALL text") не даёт достаточной надёжности на
    # дакутэн/хандакутэн мелких вертикальных глифов.
    jp_mismatched = []
    jp_phrases = [p for p in phrases if _is_japanese_phrase(p)]
    if jp_phrases:
        try:
            jp_transcript = providers.verify_text_in_image(image, prompt=_JP_COLUMN_PROMPT)
        except Exception as e:  # noqa: BLE001
            print(f"  !! OCR-контроль каны (второй вызов) упал: {e}", flush=True)
            jp_mismatched = list(jp_phrases)  # сбой вызова -> не подтверждено, провал
        else:
            jp_mismatched = [p for p in jp_phrases
                             if not _glyph_by_glyph_match(p, jp_transcript)]
            if jp_mismatched:
                print(f"  OCR-контроль каны: колонка-транскрипт={jp_transcript!r} — "
                      f"не сошлось поглифно (дакутэн/хандакутэн?) {jp_mismatched!r}",
                      flush=True)

    ok = not missing and not duplicated and not jp_mismatched
    if not ok:
        reasons = []
        if missing:
            reasons.append(f"отсутствуют фразы {missing!r}")
        if duplicated:
            reasons.append(f"фразы повторены ДВАЖДЫ+ (дубль на картинке) {duplicated!r}")
        if jp_mismatched:
            reasons.append(f"кана не сошлась поглифно {jp_mismatched!r}")
        print(f"  OCR-контроль: транскрипт={transcript!r} — {'; '.join(reasons)}",
              flush=True)
    return ok


def _transcript_has_no_significant_text(transcript: str) -> bool:
    """Эвристика "OCR-транскрипт говорит, что на картинке текста нет" — используется
    _verify_no_text для text-fallback ветки (пятнадцатый заход поймал регресс: фолбэк-
    генерация без текст-блока в промпте может ВСЁ РАВНО прийти С художественным текстом,
    если design["prompt"] уже описывает встроенную типографику как часть сцены, см.
    tests/test_ux_fallback_double_text_regression_qa.py). providers.verify_text_in_image
    с ДЕФОЛТНЫМ _OCR_PROMPT ("transcribe ALL text ... exactly as written") на картинке
    БЕЗ текста типично отвечает короткой служебной фразой ("No text", "No text visible
    in this image", "There is no text", пустая строка и т.п.) — не пытаемся угадать
    ВСЕ формулировки модели, вместо этого: (а) явные отрицающие ключевые слова ловятся
    по подстроке (englishский "no text"/"no visible text"/"none"/"no readable text"), а
    (б) как более надёжный универсальный сигнал — короткий ответ (после нормализации
    пробелов/пунктуации, <= _NO_TEXT_MAX_CHARS символов) без единой буквы/цифры/CJK-
    глифа трактуется как "текста нет" (реальный текст на принте — минимум одно слово
    из нескольких букв, служебный ответ модели без текста обычно короче и/или не
    содержит алфавитных символов вовсе, если она просто пишет "-" или пустую строку)."""
    import re as _re
    t = (transcript or "").strip()
    if not t:
        return True
    low = t.lower()
    _NEGATIVE_PHRASES = (
        "no text", "no visible text", "no readable text", "none visible",
        "there is no text", "i don't see any text", "i do not see any text",
        "no words", "no lettering",
    )
    if any(neg in low for neg in _NEGATIVE_PHRASES):
        return True
    # Убираем пунктуацию/пробелы, оставляем только буквы/цифры/CJK — если после этого
    # пусто ИЛИ строка сама по себе "none"/"n/a"-подобный служебный ответ без единой
    # содержательной буквы естественного алфавита, считаем что текста нет.
    letters_only = _re.sub(r"[^\w]", "", t, flags=_re.UNICODE)
    if not letters_only:
        return True
    return False


_NO_TEXT_MAX_CHARS = 40  # см. _transcript_has_no_significant_text — не используется
                          # напрямую (эвристика отрицания/пустоты выше самодостаточна),
                          # оставлено как документированный порог на случай будущего
                          # ужесточения (например ограничить длину "почти пустого" ответа).


def _verify_no_text(image: Image.Image) -> bool:
    """Проверка ОБРАТНАЯ _verify_text — используется ТОЛЬКО в text-fallback ветке
    render_design (design["type_spec"]="" запросил картинку БЕЗ текста): подтверждает,
    что на fallback-картинке ДЕЙСТВИТЕЛЬНО нет значимого встроенного текста — модель
    (nano-banana) может проигнорировать безусловный запрет _NO_TEXT_TAIL, особенно
    когда design["prompt"] (основное художественное описание сцены, ДО text-fallback
    веток — не трогается) уже подробно описывает типографику как часть композиции
    (см. docs/PROJECT_STATE.md, пятнадцатый заход, живой баг daily_2026-07-09/
    0007_payback). Любой сбой самого OCR-вызова -> False (трактуем как "не
    подтверждено, что текста нет" — тот же консервативный принцип, что и _verify_text:
    лучше лишняя fallback-попытка, чем молча пропустить брак)."""
    try:
        transcript = providers.verify_text_in_image(image)
    except Exception as e:  # noqa: BLE001
        print(f"  !! OCR-контроль отсутствия текста (fallback) упал: {e}", flush=True)
        return False
    no_text = _transcript_has_no_significant_text(transcript)
    if not no_text:
        print(f"  OCR-контроль отсутствия текста (fallback): транскрипт={transcript!r} "
              f"— НА FALLBACK-КАРТИНКЕ ЕСТЬ ТЕКСТ (модель проигнорировала запрет)",
              flush=True)
    return no_text


def _expected_text_phrases(design: dict) -> list:
    """Какие фразы ДОЛЖНЫ появиться на картинке при TEXT_RENDER=image (для OCR-
    контроля) — та же логика выбора, что art_director._exact_spelling_phrase (quote
    приоритетнее slogan), плюс name_jp/kana отдельным элементом (кандзи-колонка).
    Пустой список — тексту на этом дизайне не место (type_spec пуст), OCR не нужен."""
    type_spec = str(design.get("type_spec") or "").strip()
    if not type_spec:
        return []
    quote = str(design.get("quote") or "").strip()
    slogan = str(design.get("slogan") or "").strip()
    phrase = quote or slogan
    out = [p for p in (phrase,) if p]
    name_jp = str(design.get("name_jp") or design.get("kana") or "").strip()
    if name_jp:
        out.append(name_jp)
    return out


# ── vision-QC-гейт анатомии рук (жалоба владельца на 3-рукие/безрукие персонажи, ────
# 2026-07-11) ─────────────────────────────────────────────────────────────────────

def _verify_anatomy(design: dict, image: Image.Image) -> tuple:
    """QC-гейт vision-контроля анатомии рук: для ФИГУРАТИВНЫХ персонажей
    (design.get("has_human_figure", True) — дефолт True, см. art_director._parse/
    _ANATOMY_BLOCK) дешёвая текстовая модель Gemini (providers.verify_anatomy_in_image,
    ТА ЖЕ _OCR_MODEL, что OCR-контроль спеллинга) считает руки/кисти ГЛАВНОГО персонажа
    и оценивает аномалии (лишняя рука, отсутствующая рука, сросшиеся/неверные пальцы).

    Возвращает (ok: bool, info: dict) — info пробрасывается наверх для логирования
    ({"arms_visible", "anomaly", "reason"} при успешном вызове, {"error": ...} при сбое
    самого вызова, {} — гейт не применялся вовсе, см. ниже).

    Гейт НЕ применяется (ok=True, info={} — ПУСТОЙ dict, БЕЗ vision-вызова, экономия
    RPD-квоты) в ДВУХ случаях:
    - design реально НЕ фигуративен (has_human_figure=False, явно проставлено
      art_director — животное-мем/машина/дорожный знак и т.п.);
    - config.ANATOMY_QC=off (владелец выключил гейт вручную — дневная квота Gemini на
      исходе, см. .env.example).

    Любой сбой САМОГО vision-вызова (нет ключа/сеть/HTTP/битый ответ) -> ok=False (тот
    же консервативный принцип, что _verify_text: лучше лишний ретрай, чем молча
    пропустить возможную аномалию) — вызывающий код (render_design) ретраит наравне с
    OCR/масштабом, а при исчерпании попыток принимает лучшую как есть (best-effort, см.
    докстринг render_design)."""
    if not design.get("has_human_figure", True):
        return True, {}
    if not config.ANATOMY_QC:
        return True, {}
    try:
        info = providers.verify_anatomy_in_image(image)
    except Exception as e:  # noqa: BLE001
        print(f"  !! vision-QC анатомии упал: {e}", flush=True)
        return False, {"error": str(e)}
    ok = not info.get("anomaly")
    if not ok:
        print(f"  vision-QC анатомии: аномалия — arms_visible="
              f"{info.get('arms_visible')!r}, reason={info.get('reason')!r}", flush=True)
    return ok, info


# ── Генерация одного дизайна (переиспользуемое ядро пайплайна) ──────────────────

def render_design(design: dict, tag: str, outdir: Path, timeout_retries: int = 2,
                   text_style: str = "auto", no_juice: bool = False,
                   log_prefix: str = "", green_only: bool = False,
                   design_json_path: Path = None) -> dict:
    """Полный цикл ОДНОГО дизайна: генерация (с QC-гейтом границ + масштаба фигуры) ->
    вырезка -> типографика -> апскейл -> 4-5 файлов (tag_raw.png/tag_diecut.png/
    tag_ongreen.png/tag_print.png/tag_design.json) в outdir. Переиспользуется и CLI-
    батчем (run_one ниже), и daily_prints.py.

    design: dict из art_director.make_ideas (prompt/chroma/slogan/slogan_color/kana/
    character_en/title_en/signature_props/text_mode). Если character_en непусто —
    перед генерацией достаётся каноничный референс-портрет персонажа
    (character_ref.get_reference) и рисование идёт ПО РЕФЕРЕНСУ (см.
    providers.generate_image(reference=...)), иначе как раньше.

    design["meme_ref"] (опционально, жалоба владельца 2026-07-11 — интернет-мемы
    сова на скакалке/кот со слюной/Backrooms генерятся НЕ похожими на оригинал) —
    slug РУЧНОГО референса data/meme_refs/<slug>.png (см. meme_ref.py): если
    задан И файл существует — ПРИОРИТЕТНЕЕ character_en (мемы почти всегда без
    character_en, но если оба заданы — meme_ref побеждает, т.к. указывает на
    ТОЧНУЮ картинку оригинала мема). Не Claude-поле — art_director про meme_ref
    ничего не знает, это владельческая привязка slug->файл, прокидывается ИЗВНЕ
    (mega_batch_run._process_one читает его из записи плана trends_plan.json и
    кладёт в design ПОСЛЕ art_director.make_ideas). Если slug задан, но файла нет
    (владелец ещё не положил референс) — НЕ падает, генерация идёт по текстовому
    описанию, КАК СЕЙЧАС (обычный character_ref-путь, если character_en тоже
    задан, иначе просто по тексту), с предупреждением в лог. Если design реально
    просит стиль с hybrid_ring_text=true (docs/STYLE_BANK.json "09_ring_medallion",
    через design["style_id"]/["style_mix"], см. _is_hybrid_ring_style) — медальон-
    гибрид: art_director.build_prompt сам просит ПУСТОЕ декоративное кольцо (без
    текста), кольцевой текст (quote/slogan) накладывается КОДОМ после вырезки
    (typography_v3.ring_text), OCR-контроль спеллинга для этого дизайна не участвует.
    tag: базовое имя файлов без расширения (напр. "01" или "0137_kenpachi").

    text_style: "auto" (дефолт) — типографика v2 (typography.compose_text), режим
    берётся из design["text_mode"] (Claude решает ПО КОМПОЗИЦИИ конкретного дизайна:
    none/under/punch/kana_side). Если передан ЯВНЫЙ стиль из typography.STYLES
    (none/anton/kana/comic/tag, напр. через CLI --text-style) — он ПРИОРИТЕТНЕЕ
    text_mode (обратная совместимость v1). Игнорируется для ring_medallion (свой
    единственный текстовый путь, ring_text).

    Возвращает {"ok": bool, "attempts": int, "coverage": float, "error": str|None,
    "raw": path|None, "diecut": path|None, "ongreen": path|None, "print_png":
    path|None, "design_json": path, "text_fallback": bool, "single_text_no_overlay":
    bool, "print_fallback": bool, "anatomy_warning": bool} — attempts нужен вызывающему
    коду для точного учёта стоимости (QC-ретраи И OCR-ретраи спеллинга считаются в один
    общий счётчик).
    text_fallback=True — TEXT_RENDER=image не сошёлся по OCR-контролю спеллинга за
    все попытки основного цикла, откат на fallback-генерацию БЕЗ текст-блока (см.
    раздел «Текст в генерации» в README). single_text_no_overlay=True (пятнадцатый
    заход, регресс-фикс двойного текста) — ВСЕ fallback-попытки (до
    _FALLBACK_NO_TEXT_MAX_ATTEMPTS) ВСЁ РАВНО пришли с художественным текстом
    (модель проигнорировала запрет) — принт выпущен С ОДНИМ уже нарисованным
    текстовым слоем, кодовая typography_v3/typography НЕ накладывается поверх (иначе
    двойной текст); в этом случае text_fallback=True, но typography НЕ применена.
    print_png — адаптивный апскейл (x4 realesrgan + Lanczos-досчёт до
    config.PRINT_MIN_SIDE, см. upscale.upscale_to_print_min) поверх diecut
    (config.UPSCALE=on, дефолт), None если апскейл отключён вовсе — НЕ считается
    ошибкой дизайна, result["ok"] может быть True с print_png=None. print_fallback=
    True — realesrgan был недоступен/упал/таймаутировал, print_png получен ЦЕЛИКОМ
    через Lanczos с исходника (хуже качеством, но печатный размер гарантирован).
    anatomy_warning=True (жалоба владельца, 2026-07-11) — vision-QC-гейт анатомии
    (_verify_anatomy, только для design["has_human_figure"] и только при
    config.ANATOMY_QC=on) НЕ подтвердил чистую анатомию НИ НА ОДНОЙ попытке основного
    цикла — best-effort, дизайн ВСЁ РАВНО выпускается (не блокируем, та же логика, что
    border coverage < 0.90/фигура мелкая на всех попытках), просто честное
    предупреждение в результат и в лог. anatomy_warning=False — либо гейт не
    применялся вовсе (не фигуративный дизайн/ANATOMY_QC=off), либо хотя бы одна
    попытка подтвердилась как анатомически чистая.

    green_only (шестнадцатый+1 заход, заказ владельца для мега-партии D:\\800): на
    успешный дизайн сохраняется РОВНО ОДИН файл <tag>.png — исходная генерация
    СОХРАНЯЕТСЯ КАК ЕСТЬ, вообще БЕЗ обработки пикселей (правка владельца
    2026-07-10, дословно: "правило синего фона если зеленые элементы на персонаже,
    сохраняем. Просто не вырезай фон и все, мне нужны файлы с фоном Зеленый или
    синий, в зависимости от принта") — фон остаётся ТЕМ хромакеем, что реально
    сгенерился (зелёным ИЛИ синим), НИКАКОЙ перекраски синего в зелёный больше НЕТ
    в этом пути (chroma_remove.recolor_bg сюда не вызывается; сама функция и её
    тесты в chroma_remove.py оставлены как рабочая утилита на будущее). Вырезка
    (diecut), апскейл (print) и ongreen-превью НЕ выполняются и НЕ сохраняются
    (экономия времени и денег Replicate), _raw.png отдельно тоже не пишется —
    <tag>.png И ЕСТЬ raw. Правило арт-директора "зелёные элементы у персонажа ->
    синий хромакей" (art_director._chroma_bg) работает КАК РАНЬШЕ и здесь не
    трогается — оно определяет, каким хромакеем ИДЁТ генерация, а не что делает
    green_only с уже сгенерённым файлом. ВСЕ QC-гейты цикла генерации (цвет рамки
    против эталона хромакея, масштаб фигуры, OCR-контроль текста, vision-QC анатомии
    рук, text-fallback)
    работают КАК ЕСТЬ — они от вырезки не зависят. Нюанс ring_medallion: кольцевой
    текст в этом режиме НЕ наносится (он накладывался кодом ПОСЛЕ вырезки) — кольцо
    остаётся пустым, фраза сохранена в design.json-паспорте для дообработки. Путь к
    <tag>.png возвращается в result["green"].

    design_json_path: куда писать паспорт design.json (дефолт None — рядом с
    картинкой, <outdir>/<tag>_design.json, старое поведение). mega_batch_run в
    режиме green_only передаёт зеркальный путь D:\\800\\_meta\\<category>\\... —
    тематическая папка остаётся с одной картинкой на принт.

    result["images"] — сколько картинок ФАКТИЧЕСКИ отдал провайдер за все попытки
    (main-цикл + text-fallback): попытки, где Gemini НЕ отдал картинку (429/500 —
    реально не списываются с баланса), в attempts входят, а в images НЕТ — точный
    учёт сметы ведётся по images (mega_batch_run --budget-cap)."""
    p = log_prefix or f"[{tag}]"
    if design_json_path is None:
        design_json_path = outdir / f"{tag}_design.json"
    design_json_path = Path(design_json_path)
    design_json_path.parent.mkdir(parents=True, exist_ok=True)
    design_json_path.write_text(
        json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {"ok": False, "attempts": 0, "images": 0, "coverage": 0.0, "error": None,
              "raw": None, "diecut": None, "ongreen": None, "print_png": None,
              "green": None,
              "design_json": str(design_json_path), "text_fallback": False,
              "single_text_no_overlay": False, "print_fallback": False,
              "anatomy_warning": False}

    # Медальон-гибрид (typography_v3.ring_text): design реально просит стиль с
    # hybrid_ring_text=true — ЛИБО банковский путь (docs/STYLE_BANK.json, "09_ring_
    # medallion" через style_id/style_mix — ПРОМПТ-часть, пустое кольцо, ПОЛНОСТЬЮ
    # строит art_director.build_prompt/_style_bank_prompt_block САМ, не дублируется
    # здесь), ЛИБО прямые поля design ВНЕ банка (_hybrid_ring_via_direct_fields) — для
    # НИХ суффикс промпта добавляется явно ниже (art_director про них не знает).
    # В обоих случаях batch_print только кладёт САМ ТЕКСТ кольца КОДОМ (ring_text)
    # после вырезки — фраза кольца та же логика выбора, что _expected_text_phrases
    # (quote приоритетнее slogan).
    ring_via_bank = _hybrid_ring_via_style_bank(design)
    ring_via_direct = _hybrid_ring_via_direct_fields(design)
    ring_medallion = ring_via_bank or ring_via_direct
    ring_phrase = str(design.get("quote") or design.get("slogan") or "").strip() \
        if ring_medallion else ""

    magazine_print = _is_magazine_print_style(design)
    prompt = art_director.build_prompt(design)
    if magazine_print:
        # Code-level контракт после арт-директора: LLM не может забыть фигурный низ.
        prompt = prompt + _MAGAZINE_PRINT_PROMPT_SUFFIX
    if ring_via_direct and not ring_via_bank:
        # Прямой путь (design НЕ прошёл через банк стилей) — art_director не знает про
        # этот design, значит промпт-инструкцию про пустое кольцо добавляем сами.
        prompt = prompt + _RING_MEDALLION_PROMPT_SUFFIX
    seed = random.randint(0, 2**31 - 1)

    # Рисование ПО РЕФЕРЕНСУ — ДВА независимых источника, ОБА кладут результат в
    # один и тот же `reference` (PIL.Image) и уходят в generate_image ТЕМ ЖЕ
    # механизмом (providers.generate_image(reference=...), inline_data ПЕРЕД
    # текстом) — ничего нового в providers.py не добавлено:
    #
    # 1) meme_ref (design["meme_ref"], жалоба владельца 2026-07-11 — интернет-мемы
    #    сова на скакалке/кот со слюной/Backrooms генерятся НЕ похожими на
    #    оригинал) — РУЧНОЙ файл data/meme_refs/<slug>.png (см. meme_ref.py).
    #    ПРИОРИТЕТНЕЕ character_ref: указывает на ТОЧНУЮ картинку оригинала мема,
    #    а не на общий канон-портрет вымышленного персонажа. Проверяется ПЕРВЫМ.
    # 2) character_ref (design["character_en"], см. докстринг выше) — только если
    #    meme_ref не задан ВООБЩЕ, либо задан, но файла нет/он битый (graceful:
    #    "генерация по описанию как сейчас" — тот же путь, что был бы БЕЗ этой
    #    задачи вовсе, meme_ref.get_reference уже напечатала своё предупреждение).
    #
    # Любой сбой поиска референса (любого из двух источников) -> None, генерация
    # идёт по чистому тексту, как раньше (не падаем).
    reference = None
    meme_ref_slug = str(design.get("meme_ref") or "").strip()
    if meme_ref_slug:
        try:
            reference = meme_ref.get_reference(meme_ref_slug)
        except Exception as e:  # noqa: BLE001 — поиск референса не должен ронять генерацию
            print(f"{p} !! meme_ref упал для {meme_ref_slug!r}: {e} — без референса мема",
                  flush=True)
            reference = None
        if reference is not None:
            prompt = _MEME_REFERENCE_PREFIX + prompt
            print(f"{p} meme_ref найден: {meme_ref_slug!r} — рисуем ПО РЕФЕРЕНСУ мема",
                  flush=True)
        # reference is None здесь (файла нет/битый/недопустимый slug) — падаем
        # НИЖЕ на обычный character_ref-путь, как будто meme_ref не было вовсе.

    character_en = str(design.get("character_en") or "").strip()
    if reference is None and character_en:
        try:
            reference = character_ref.get_reference(character_en, design.get("title_en", ""))
        except Exception as e:  # noqa: BLE001 — поиск референса не должен ронять генерацию
            print(f"{p} !! character_ref упал для {character_en!r}: {e} — без референса",
                  flush=True)
            reference = None
        if reference is not None:
            prompt = _REFERENCE_PREFIX + prompt
            print(f"{p} референс найден: {character_en!r} — рисуем ПО РЕФЕРЕНСУ", flush=True)
        else:
            print(f"{p} референс для {character_en!r} не найден — генерация по тексту, "
                  f"как раньше", flush=True)

    attempt_prompt = prompt
    attempt_reference = reference
    image_other_rejections = 0

    # TEXT_RENDER=image (десятый заход): встроенная типографика ожидает конкретные
    # фразы на картинке — OCR-контроль спеллинга (_verify_text) проверяет каждую
    # попытку в ТОМ ЖЕ цикле, что QC-гейт границ. Если design не просит текст
    # (type_spec пуст) — expected_phrases пуст, OCR всегда True (нечего проверять).
    # ring_medallion: OCR НЕ ждёт текст на raw (кольцо по инструкции стиля рисуется
    # ПУСТЫМ — см. art_director._style_bank_prompt_block), кольцевая фраза проверяется
    # только офлайн-геометрией самого ring_text (typography_v3), не OCR-вызовом.
    text_render_image = config.TEXT_RENDER == "image"
    expected_phrases = ([] if ring_medallion else
                        (_expected_text_phrases(design) if text_render_image else []))

    # QC-гейт границ кадра + OCR-контроль спеллинга + QC-гейт масштаба фигуры: до
    # timeout_retries доп. попыток генерации (итого максимум 1+timeout_retries попыток),
    # берём попытку с максимальным coverage рамки хромакеем СРЕДИ попыток, прошедших И
    # OCR (если expected_phrases непуст), И масштаб фигуры (bbox высоты >=
    # config.FIGURE_MIN_FRAC) — попытка, провалившая любой из гейтов, не выбирается
    # лучшей, пока есть хоть одна прошедшая оба; если НИ ОДНА не прошла OCR — берём
    # лучшую по coverage как есть (сработает text-fallback ниже). Гейт масштаба фигуры
    # НЕ участвует в text-fallback механизме (тот только про спеллинг) — если фигура
    # мелкая на ВСЕХ попытках, используется лучшая по coverage/OCR как есть с явным
    # предупреждением (не блокируем выпуск дизайна целиком, как и остальные QC-гейты).
    best_img, best_cov, best_seed = None, -1.0, seed
    best_img_ocr_ok, best_cov_ocr_ok = None, -1.0
    best_img_ocr_figure_ok, best_cov_ocr_figure_ok = None, -1.0
    best_img_figure_ok, best_cov_figure_ok = None, -1.0
    best_layout_img, best_layout_cov = None, -1.0
    # vision-QC анатомии рук (см. _verify_anatomy) — НЕ отдельная тир-лестница, а
    # ДОПОЛНИТЕЛЬНОЕ AND-условие, слитое С СУЩЕСТВУЮЩИМ гейтом масштаба фигуры
    # (best_img_ocr_figure_ok/best_img_figure_ok ниже теперь требуют "фигура нормального
    # размера И анатомия чистая" одновременно) — лучшая попытка должна быть физически
    # корректной по ОБОИМ критериям сразу, если такая попытка вообще нашлась. Отдельно
    # (для честного best-effort предупреждения, не влияет на выбор лучшей попытки)
    # отслеживаем: был ли гейт вообще применим (anatomy_gate_applicable, т.е.
    # design реально фигуративен И config.ANATOMY_QC=on) и была ли ХОТЬ ОДНА попытка
    # анатомически чистой (any_attempt_anatomy_ok).
    anatomy_gate_applicable = False
    any_attempt_anatomy_ok = False
    attempts_used = 0
    images_received = 0  # попытки, где провайдер РЕАЛЬНО отдал картинку (только они
                         # списываются с баланса — 429/500 без изображения бесплатны),
                         # учёт сметы mega_batch_run ведётся по этому счётчику.
    for attempt in range(1 + timeout_retries):
        try_seed = seed + 101 * attempt
        attempts_used += 1
        try:
            print(f"{p} генерация... seed={try_seed} chroma={design['chroma']} "
                  f"slogan={design.get('slogan')!r}", flush=True)
            attempt_img = providers.generate_image(
                attempt_prompt,
                seed=try_seed,
                reference=attempt_reference,
            )
        except providers.GeminiImageRejected as e:
            image_other_rejections += 1
            result["error"] = str(e)
            attempt_prompt, attempt_reference = _recover_image_other(
                design,
                prompt,
                reference,
                rejection_count=image_other_rejections,
            )
            recovery = "без референса" if image_other_rejections == 1 else "безопасный краткий промпт"
            print(f"{p} !! {e.finish_reason}: следующий повтор изменён ({recovery})",
                  flush=True)
            continue
        except Exception as e:  # noqa: BLE001
            print(f"{p} !! попытка {attempt + 1} упала: {e}", flush=True)
            result["error"] = str(e)
            continue
        images_received += 1
        cov = _border_chroma_coverage(attempt_img, chroma=design["chroma"])
        print(f"{p} border coverage={cov:.2f}", flush=True)
        if cov > best_cov:
            best_img, best_cov, best_seed = attempt_img, cov, try_seed

        layout_ok = True
        layout_metrics = {}
        if magazine_print:
            layout_ok, layout_metrics = _magazine_print_layout_quality(
                attempt_img,
                chroma=design["chroma"],
            )
            print(f"{p} style34 die-cut: {'OK' if layout_ok else 'full-bleed'} "
                  f"(top={layout_metrics['top']:.2f}, bottom={layout_metrics['bottom']:.2f}, "
                  f"left={layout_metrics['left']:.2f}, right={layout_metrics['right']:.2f})",
                  flush=True)
            if layout_ok and cov > best_layout_cov:
                best_layout_img, best_layout_cov = attempt_img, cov

        ocr_ok = True
        if expected_phrases:
            # OCR-вызов — отдельный факт, НЕ платный image-вызов; НЕ увеличивает
            # attempts_used (тот считает именно image-генерации для сметы
            # daily_prints/COST_PER_IMAGE_USD, OCR копеечный текстовый вызов
            # gemini-2.5-flash) — сам факт вызова залогирован print()'ом ниже.
            ocr_ok = _verify_text(attempt_img, expected_phrases)
            print(f"{p} OCR-контроль спеллинга: {'OK' if ocr_ok else 'провал'} "
                  f"(факт вызова залогирован)", flush=True)
        if ocr_ok and layout_ok and cov > best_cov_ocr_ok:
            best_img_ocr_ok, best_cov_ocr_ok = attempt_img, cov

        figure_ok, figure_frac = _figure_fills_frame(attempt_img, chroma=design["chroma"])
        print(f"{p} масштаб фигуры: высота bbox={figure_frac:.2f} кадра "
              f"({'OK' if figure_ok else 'фигура слишком мелкая'}, порог "
              f"{config.FIGURE_MIN_FRAC})", flush=True)

        # vision-QC анатомии рук (жалоба владельца, см. _verify_anatomy докстринг) —
        # ТОЛЬКО фигуративные персонажи (has_human_figure), ТОЛЬКО если config.ANATOMY_QC
        # включён; иначе ok=True без вызова (info={} — печатать нечего, гейт не
        # применялся). Отдельный дешёвый текстовый vision-вызов, НЕ платный image-вызов
        # — не увеличивает attempts_used, наравне с OCR-контролем спеллинга выше.
        anatomy_ok, anatomy_info = _verify_anatomy(design, attempt_img)
        if anatomy_info:
            anatomy_gate_applicable = True
            print(f"{p} vision-QC анатомии: {'OK' if anatomy_ok else 'аномалия'} "
                  f"(arms_visible={anatomy_info.get('arms_visible')!r}, "
                  f"reason={anatomy_info.get('reason', '')!r})", flush=True)
        if anatomy_ok:
            any_attempt_anatomy_ok = True

        # figure_ok и anatomy_ok идут В ОДНОЙ связке ("физическая корректность фигуры")
        # для целей выбора ЛУЧШЕЙ попытки — лучшая попытка должна одновременно иметь
        # адекватный масштаб И чистую анатомию, если хоть одна попытка даёт оба сразу
        # (anatomy_ok тривиально True, когда гейт неприменим — фигуративность/флаг —
        # тогда это условие полностью эквивалентно старому "просто figure_ok").
        figure_anatomy_ok = figure_ok and anatomy_ok and layout_ok
        if ocr_ok and figure_anatomy_ok and cov > best_cov_ocr_figure_ok:
            best_img_ocr_figure_ok, best_cov_ocr_figure_ok = attempt_img, cov
        if figure_anatomy_ok and cov > best_cov_figure_ok:
            best_img_figure_ok, best_cov_figure_ok = attempt_img, cov

        if cov >= 0.90 and ocr_ok and figure_ok and anatomy_ok and layout_ok:
            break
        if attempt < timeout_retries:
            reason = []
            if cov < 0.90:
                reason.append(f"coverage {cov:.2f} < 0.90")
            if not ocr_ok:
                reason.append("OCR не сошёлся")
            if not figure_ok:
                reason.append(f"фигура слишком мелкая ({figure_frac:.2f} < "
                              f"{config.FIGURE_MIN_FRAC})")
            if not anatomy_ok:
                reason.append(f"аномалия анатомии рук "
                              f"({anatomy_info.get('reason') or anatomy_info.get('error') or 'см. лог'})")
            if not layout_ok:
                reason.append(
                    f"style34 упирается в край (bottom={layout_metrics.get('bottom', 0.0):.2f})"
                )
            print(f"{p} {' и '.join(reason)} -> retry", flush=True)

    result["attempts"] = attempts_used
    result["images"] = images_received
    result["coverage"] = best_cov

    if best_img is None:
        print(f"{p} !! ни одна попытка не дала картинку — пропуск", flush=True)
        result["error"] = result["error"] or "нет изображения ни на одной попытке"
        return result

    if magazine_print and best_layout_img is None:
        print(f"{p} !! HARD-reject style34: все попытки упираются в края как "
              f"прямоугольная обложка — нужен фигурный print contour", flush=True)
        result["error"] = (
            "стиль 34: прямоугольная/full-bleed композиция без чистого нижнего "
            "хромакейного поля — нужна перегенерация"
        )
        return result

    # Выбор финального изображения + решение про text-fallback. Приоритет: попытка,
    # прошедшая И OCR, И масштаб фигуры > попытка, прошедшая только OCR (масштаб мелкий
    # на всех попытках — не блокируем выпуск, только предупреждаем) > text-fallback.
    text_fallback = False
    # single_text_no_overlay (пятнадцатый заход, регресс-фикс двойного текста): ВСЕ
    # fallback-попытки (см. ветку ниже) пришли С художественным текстом несмотря на
    # запрет — код НЕ должен накладывать typography_v3/typography поверх уже готового
    # текста (иначе двойной нечитаемый принт). Остаётся False во всех остальных
    # случаях (обычная ветка "текст встроен и OCR подтвердил" уже обрабатывается через
    # apply_code_typography/expected_phrases, этот флаг — только для нового кейса).
    single_text_no_overlay = False
    if expected_phrases and best_img_ocr_figure_ok is not None:
        raw_img, best_cov = best_img_ocr_figure_ok, best_cov_ocr_figure_ok
    elif expected_phrases and best_img_ocr_ok is not None:
        # Хотя бы одна попытка прошла OCR — используем ЛУЧШУЮ ИЗ НИХ (не просто лучшую
        # по coverage, чтобы не выпустить принт с неверным спеллингом ради чуть более
        # ровной рамки). Масштаб фигуры мелкий на ВСЕХ попытках — предупреждаем, но не
        # блокируем (тот же принцип, что и коммент border coverage < 0.90 ниже).
        raw_img, best_cov = best_img_ocr_ok, best_cov_ocr_ok
        _, worst_figure_frac = _figure_fills_frame(raw_img, chroma=design["chroma"])
        print(f"{p} !! warning: масштаб фигуры и/или анатомия рук не сошлись ни на "
              f"одной попытке, прошедшей OCR (высота bbox={worst_figure_frac:.2f} < "
              f"{config.FIGURE_MIN_FRAC} и/или anatomy_warning, см. отдельный лог "
              f"vision-QC анатомии выше) — используем лучшую по OCR/coverage как есть",
              flush=True)
    elif expected_phrases:
        # НИ ОДНА попытка не прошла OCR за все timeout_retries+1 генераций — честный
        # откат: до _FALLBACK_NO_TEXT_MAX_ATTEMPTS ДОПОЛНИТЕЛЬНЫХ генераций БЕЗ текст-
        # блока (design с пустым type_spec/quote -> art_director.build_prompt добавит
        # обычный запрет букв). Текст наносится кодовой типографикой (typography_v3/
        # typography) ниже, как раньше при TEXT_RENDER=code — НО ТОЛЬКО если сама
        # fallback-картинка ПОДТВЕРЖДЕНА OCR-ом как реально без текста (см. ниже,
        # пятнадцатый заход поймал регресс: живой daily_2026-07-09/0007_payback —
        # design["prompt"] уже описывает встроенную типографику как часть сцены,
        # модель проигнорировала безусловный запрет _NO_TEXT_TAIL в fallback-промпте и
        # нарисовала художественный текст ВСЁ РАВНО; код безусловно применял typography_
        # v3/typography ПОВЕРХ уже нарисованного текста -> двойной нечитаемый принт,
        # см. tests/test_ux_fallback_double_text_regression_qa.py).
        print(f"{p} !! текст в генерации не сошёлся за {attempts_used} попыток "
              f"(OCR) — откат на кодовый путь (доп. генерация БЕЗ текст-блока)",
              flush=True)
        fallback_design = dict(design)
        fallback_design["type_spec"] = ""
        fallback_prompt = art_director.build_prompt(fallback_design)
        if magazine_print:
            fallback_prompt = fallback_prompt + _MAGAZINE_PRINT_PROMPT_SUFFIX
        fallback_reference = attempt_reference
        # best_cov/best_img на этом месте — лучшая попытка ИЗ ОСНОВНОГО цикла (по
        # цвету рамки хромакея, ДО этой fallback-ветки; см. присвоение внутри цикла
        # выше, строка ~608) — используются как safety-net ниже, если сама fallback-
        # генерация придёт с чужим фоном.
        main_loop_best_img = best_layout_img if magazine_print else best_img
        main_loop_best_cov = best_layout_cov if magazine_print else best_cov

        fb_best_img, fb_best_cov = None, -1.0  # лучшая fallback-попытка ПО ЦВЕТУ (может
                                                # содержать текст — safety-net, если ни
                                                # одна fallback-попытка не подтвердится
                                                # как "без текста" ни через OCR.
        fb_confirmed_no_text_img, fb_confirmed_no_text_cov = None, -1.0  # лучшая
                                                # fallback-попытка, OCR-подтверждённая
                                                # как РЕАЛЬНО без текста — приоритетный
                                                # выбор, безопасно накладывать код поверх.
        for fb_attempt in range(_FALLBACK_NO_TEXT_MAX_ATTEMPTS):
            fb_seed = seed + 101 * (1 + timeout_retries + fb_attempt)
            try:
                attempts_used += 1
                fb_img = providers.generate_image(
                    fallback_prompt,
                    seed=fb_seed,
                    reference=fallback_reference,
                )
            except providers.GeminiImageRejected as e:
                image_other_rejections += 1
                fallback_prompt, fallback_reference = _recover_image_other(
                    fallback_design,
                    fallback_prompt,
                    fallback_reference,
                    rejection_count=image_other_rejections,
                )
                print(f"{p} !! fallback {e.finish_reason}: изменяю запрос", flush=True)
                continue
            except Exception as e:  # noqa: BLE001
                print(f"{p} !! фолбэк-генерация {fb_attempt + 1}/"
                      f"{_FALLBACK_NO_TEXT_MAX_ATTEMPTS} упала: {e}", flush=True)
                continue
            images_received += 1
            fb_cov = _border_chroma_coverage(fb_img, chroma=design["chroma"])
            print(f"{p} фолбэк-генерация {fb_attempt + 1}/"
                  f"{_FALLBACK_NO_TEXT_MAX_ATTEMPTS} (без текста) border "
                  f"coverage={fb_cov:.2f}", flush=True)
            if magazine_print:
                fb_layout_ok, fb_layout_metrics = _magazine_print_layout_quality(
                    fb_img,
                    chroma=design["chroma"],
                )
                if not fb_layout_ok:
                    print(f"{p} !! fallback style34 full-bleed: bottom="
                          f"{fb_layout_metrics['bottom']:.2f} — не кандидат", flush=True)
                    continue
            if fb_cov > fb_best_cov:
                fb_best_img, fb_best_cov = fb_img, fb_cov

            if fb_cov < main_loop_best_cov:
                # РЕГРЕСС (живой баг daily_2026-07-09/0008_рудеус_грейрат__mushoku,
                # двенадцатый заход) — fallback-генерация не проходит тот же цветовой
                # QC-гейт хромакея, что и основные попытки (чужой фон, например белый
                # вместо запрошенного design["chroma"]). Картинка с ЗАВЕДОМО плохим
                # цветом рамки бессмысленна как кандидат независимо от того, есть на
                # ней текст или нет — не тратим доп. OCR-вызов/следующую fallback-
                # попытку, сразу выходим из цикла (main_loop_best используется ниже).
                print(f"{p} !! fallback-фон хуже основной попытки (fb_cov="
                      f"{fb_cov:.2f} < best_cov={main_loop_best_cov:.2f}) — "
                      f"пропускаем OCR-контроль отсутствия текста для этой попытки, "
                      f"прерываем fallback-цикл", flush=True)
                break

            fb_no_text_ok = _verify_no_text(fb_img)
            print(f"{p} OCR-контроль отсутствия текста (fallback): "
                  f"{'подтверждено — текста нет' if fb_no_text_ok else 'провал — текст ЕСТЬ'}",
                  flush=True)
            if fb_no_text_ok and fb_cov > fb_confirmed_no_text_cov:
                fb_confirmed_no_text_img = fb_img
                fb_confirmed_no_text_cov = fb_cov
            if fb_no_text_ok and fb_cov >= 0.90:
                break  # хорошая безтекстовая попытка найдена — не тратим лишние вызовы

        if fb_confirmed_no_text_img is not None:
            # Лучший случай: fallback реально пришла без текста (OCR подтвердил) —
            # безопасно накладывать typography_v3/typography поверх ниже.
            if fb_confirmed_no_text_cov < main_loop_best_cov:
                # РЕГРЕСС (живой баг daily_2026-07-09/0008_рудеус_грейрат__mushoku):
                # fallback-генерация не проходит тот же цветовой QC-гейт хромакея
                # (_border_chroma_coverage), что и основные попытки — например модель
                # нарисовала БЕЛЫЙ фон вместо запрошенного design["chroma"]. Слепое
                # принятие такой картинки ломает вырезку (chroma_remove.cutout_green
                # берёт чужой цвет как ключ и съедает куски фигуры). Если среди
                # ОСНОВНЫХ попыток была картинка с лучшим/равным цветом рамки (пусть
                # и без встроенного текста — тот всё равно будет наложен кодом ниже) —
                # используем ЕЁ вместо испорченного фолбэка.
                print(f"{p} !! fallback-фон хуже основной попытки (fb_cov="
                      f"{fb_confirmed_no_text_cov:.2f} < best_cov={main_loop_best_cov:.2f}) "
                      f"— используем лучшую основную попытку вместо fallback-картинки "
                      f"с чужим фоном", flush=True)
                raw_img, best_cov = main_loop_best_img, main_loop_best_cov
            else:
                raw_img, best_cov = fb_confirmed_no_text_img, fb_confirmed_no_text_cov
            text_fallback = True
        else:
            # НИ ОДНА fallback-попытка (максимум _FALLBACK_NO_TEXT_MAX_ATTEMPTS) не
            # подтвердилась OCR-ом как "без текста" — модель упорно рисует
            # художественный текст несмотря на запрет (см. докстринг ветки выше).
            # text_fallback ВСЁ РАВНО True (fallback-путь был запрошен и пройден — это
            # факт хода пайплайна, отдельная задача от "что теперь делать с текстом"),
            # но НАЛОЖИТЬ КОДОВУЮ ТИПОГРАФИКУ ПОВЕРХ ЗАПРЕЩЕНО (это и есть сам баг,
            # который чиним) — лучше выпустить принт БЕЗ повторного текста (единственный
            # уже нарисованный художественный слой остаётся как есть), чем удвоить его.
            # single_text_no_overlay=True — отдельный флаг для apply_code_typography
            # ниже (отменяет наложение безусловно, ДАЖЕ раз text_fallback=True обычно
            # включал бы typography_v3/typography поверх).
            candidate_img = fb_best_img if fb_best_img is not None else main_loop_best_img
            candidate_cov = fb_best_cov if fb_best_img is not None else main_loop_best_cov
            if fb_best_img is not None and fb_best_cov < main_loop_best_cov:
                candidate_img, candidate_cov = main_loop_best_img, main_loop_best_cov
            print(f"{p} !! warning: {_FALLBACK_NO_TEXT_MAX_ATTEMPTS} фолбэк-попыток "
                  f"БЕЗ текста ВСЕ ПРИШЛИ С художественным текстом (модель "
                  f"игнорирует запрет) — принимаем как есть БЕЗ наложения кодовой "
                  f"типографики (двойной текст хуже, чем принт без слогана)",
                  flush=True)
            raw_img, best_cov = candidate_img, candidate_cov
            text_fallback = True
            single_text_no_overlay = True
    elif best_img_figure_ok is not None:
        # TEXT_RENDER=code (или design без type_spec) — OCR не участвует, но масштаб
        # фигуры И анатомия рук проверяются всегда: используем лучшую по coverage
        # СРЕДИ попыток, прошедших ОБА QC-гейта (масштаб + анатомия), если хоть одна
        # нашлась (см. figure_anatomy_ok в цикле выше).
        raw_img, best_cov = best_img_figure_ok, best_cov_figure_ok
    else:
        fallback_best = best_layout_img if magazine_print else best_img
        _, worst_figure_frac = _figure_fills_frame(fallback_best, chroma=design["chroma"])
        print(f"{p} !! warning: масштаб фигуры и/или анатомия рук не сошлись ни на "
              f"одной попытке (высота bbox={worst_figure_frac:.2f} < "
              f"{config.FIGURE_MIN_FRAC} и/или anatomy_warning, см. отдельный лог "
              f"vision-QC анатомии выше) — используем лучшую по coverage как есть",
              flush=True)
        raw_img = fallback_best
        if magazine_print:
            best_cov = best_layout_cov

    result["attempts"] = attempts_used
    result["images"] = images_received
    result["coverage"] = best_cov
    result["text_fallback"] = text_fallback
    result["single_text_no_overlay"] = single_text_no_overlay

    if magazine_print:
        final_layout_ok, final_layout_metrics = _magazine_print_layout_quality(
            raw_img,
            chroma=design["chroma"],
        )
        if not final_layout_ok:
            print(f"{p} !! HARD-reject style34 final: bottom="
                  f"{final_layout_metrics['bottom']:.2f}, "
                  f"left={final_layout_metrics['left']:.2f}, "
                  f"right={final_layout_metrics['right']:.2f}", flush=True)
            result["error"] = (
                "стиль 34: финальная композиция касается края и выглядит как "
                "прямоугольная обложка — нужна перегенерация"
            )
            return result

    # anatomy_warning: гейт реально был применим (фигуративный дизайн И ANATOMY_QC=on)
    # НА ХОТЬ ОДНОЙ попытке, но НИ ОДНА попытка не подтвердилась как анатомически чистая
    # — best-effort (не блокируем выпуск дизайна, та же логика, что border coverage <
    # 0.90 ниже), только честное предупреждение в result и в лог.
    result["anatomy_warning"] = anatomy_gate_applicable and not any_attempt_anatomy_ok
    if result["anatomy_warning"]:
        print(f"{p} !! warning: vision-QC анатомии не подтвердил чистую анатомию НИ "
              f"НА ОДНОЙ попытке ({attempts_used} попыт.) — используем лучшую попытку "
              f"как есть (best-effort, не блокируем выпуск дизайна)", flush=True)

    if best_cov < 0.90:
        print(f"{p} !! warning: лучшая попытка coverage={best_cov:.2f} < 0.90, "
              f"используем её (seed={best_seed})", flush=True)

    # HARD-reject: рамка совсем не хромакей (coverage ниже жёсткого порога) — это почти
    # всегда off-style кадр (nano-banana перерисовала эталон-портрет лицом на не-
    # хромакейном фоне вместо стиля, жалоба владельца на gym). Ни вырезать, ни выпускать
    # нельзя — честный провал слота (ok=False, вызывающий код перегенерит), а не выпуск
    # мусора как ok. Порог config.CHROMA_HARD_MIN_COVERAGE (дефолт 0.5); легитимные
    # принты идут ~0.9-1.0. 0/off — отключить отсечку (старое поведение).
    if config.CHROMA_HARD_MIN_COVERAGE > 0 and best_cov < config.CHROMA_HARD_MIN_COVERAGE:
        print(f"{p} !! HARD-reject: coverage={best_cov:.2f} < "
              f"{config.CHROMA_HARD_MIN_COVERAGE} — кадр без хромакей-фона "
              f"(вероятно off-style/эталон-портрет), провал дизайна", flush=True)
        result["error"] = (f"кадр без хромакей-фона (coverage {best_cov:.2f}) — "
                           f"вероятно портрет/off-style вместо стиля, нужна перегенерация")
        return result

    if green_only:
        # Режим green_only (заказ владельца, мега-партия D:\800): РОВНО ОДИН файл
        # <tag>.png на успешный принт — исходная генерация СОХРАНЯЕТСЯ КАК ЕСТЬ,
        # вообще БЕЗ обработки пикселей (правка владельца 2026-07-10 — см.
        # докстринг render_design): фон остаётся ТЕМ хромакеем, что реально
        # сгенерился (зелёным ИЛИ синим), recolor_bg здесь больше НЕ вызывается.
        # Никакой вырезки/апскейла/ongreen/отдельного _raw.png (см. докстринг).
        green_path = outdir / f"{tag}.png"
        try:
            raw_img.save(green_path)
        except Exception as e:  # noqa: BLE001 — сбой записи = провал дизайна
            print(f"{p} !! green_only: сохранение упало: {e}", flush=True)
            result["error"] = str(e)
            return result
        result["green"] = str(green_path)
        result["ok"] = True
        print(f"{p} ok (green_only) -> {green_path.name} "
              f"(chroma={design.get('chroma')}, text_fallback={text_fallback}, "
              f"паспорт: {design_json_path})", flush=True)
        return result

    raw_path = outdir / f"{tag}_raw.png"
    raw_img.save(raw_path)
    result["raw"] = str(raw_path)

    # code_typography: применять ли typography.py/typography_v3.py ПОВЕРХ вырезки.
    # TEXT_RENDER=code — всегда (старое поведение). TEXT_RENDER=image — только если
    # text_fallback сработал (встроенный текст не подтверждён, откатились на код) ИЛИ
    # у дизайна вообще не было текст-блока для встраивания (expected_phrases пуст, но
    # design может ВСЁ РАВНО просить typography_v3/typography — например старый дамп
    # design.json без type_spec, воспроизводимый через reprocess_typo.py). Если текст
    # встроен и OCR подтвердил (не text_fallback, expected_phrases непуст) — код НЕ
    # накладывает ничего поверх, диекат = вырезка как есть.
    #
    # single_text_no_overlay (пятнадцатый заход, регресс-фикс двойного текста):
    # ВСЕ fallback-попытки без текст-блока (_FALLBACK_NO_TEXT_MAX_ATTEMPTS) ВСЁ РАВНО
    # пришли с художественным текстом — единственный уже нарисованный текстовый слой
    # ЗАПРЕЩЕНО дублировать кодовой typography_v3/typography, даже если общая формула
    # ниже иначе сказала бы apply_code_typography=True (text_fallback здесь намеренно
    # False — см. ветку выбора raw_img выше). Проверяется ПЕРВОЙ, отменяет остальные
    # условия безусловно.
    apply_code_typography = (
        not single_text_no_overlay
        and ((not text_render_image) or text_fallback or not expected_phrases)
    )

    try:
        cut = chroma_remove.cutout_green(raw_img, tol=52.0).convert("RGBA")
        # protect_text_islands: при TEXT_RENDER=image и реально ожидаемом встроенном
        # тексте (expected_phrases непуст И текст НЕ ушёл в фолбэк) буквы у края
        # фигуры могут лежать отдельными альфа-островами — не срезать их наравне с
        # мусором водяных знаков (см. drop_small_islands/_TEXT_ISLAND_PROTECT_FRAC).
        # single_text_no_overlay: raw_img здесь несёт художественный текст, который
        # модель нарисовала вопреки запрету (принят как единственный текстовый слой,
        # код ничего не накладывает поверх) — буквы у края фигуры так же нуждаются в
        # защите от drop_small_islands, поэтому попадает под защиту наравне с "текст
        # встроен и подтверждён", несмотря на text_fallback=True.
        protect_islands = text_render_image and bool(expected_phrases) and (
            not text_fallback or single_text_no_overlay)
        cut = drop_small_islands(cut, protect_text_islands=protect_islands)
        if not no_juice:
            cut = juice(cut)

        slogan = design.get("slogan", "")
        slogan_color = design.get("slogan_color", "orange")
        kana = design.get("kana", "")

        if ring_medallion:
            # Медальон-гибрид: генерация нарисовала ПУСТОЕ декоративное кольцо (см.
            # _RING_MEDALLION_SUFFIX выше) — точный спеллинг кольцевого текста кладёт
            # typography_v3.ring_text КОДОМ поверх вырезки. Никакая другая типографика
            # (v2/v3/apply_style) поверх НЕ накладывается — ring_text САМ единственный
            # текстовый элемент этого режима.
            final = typography_v3.ring_text(cut, ring_phrase) if ring_phrase else cut
        elif not apply_code_typography:
            # TEXT_RENDER=image, текст встроен генерацией и подтверждён OCR — диекат
            # = вырезка как есть, кодовая типографика НЕ накладывается поверх.
            final = cut
        elif text_style == "auto":
            text_modes_v3 = design.get("text_modes_v3") or []
            if text_modes_v3:
                # Типографика v3 (docs/PRINT_STYLE_GUIDE.md): режимы решает Claude
                # (design["text_modes_v3"], может быть комбинацией), цвета текста —
                # ТОЛЬКО из палитры конкретной иллюстрации (typography_v3.compose_text_v3
                # сам вызывает palette.extract_palette), не typography._TITLE_COLORS.
                final = typography_v3.compose_text_v3(
                    cut, text_modes_v3, design, brand_label=config.BRAND_LABEL)
            else:
                # Типографика v2: режим решает Claude (design["text_mode"]) ПО КОМПОЗИЦИИ
                # конкретного дизайна — compose_text сам расширяет холст по необходимости
                # и кропит итог до контента (текст+фигура) с полями 4-6%, без мёртвой
                # пустой полосы, которую оставлял старый блок расширения 1.28.
                text_mode = design.get("text_mode", "none")
                final = typography.compose_text(cut, text_mode, slogan, slogan_color, kana)
        else:
            # Явный --text-style из typography.STYLES (v1, обратная совместимость):
            # приоритетнее text_mode. Старая калибровка v1 расчитана на портретный
            # кадр ComfyUI (текстовая зона ниже 0.80H) — на квадрате nano-banana
            # двухстрочный слоган обрезается краем, поэтому квадратный кадр расширяем
            # вниз прозрачной полосой, как раньше.
            if slogan and text_style != "none" and cut.height < int(cut.width * 1.15):
                extended = Image.new("RGBA", (cut.width, int(cut.width * 1.28)), (0, 0, 0, 0))
                extended.paste(cut, (0, 0))
                cut = extended
            final = typography.apply_style(cut, text_style, slogan, slogan_color, kana)
        diecut_path = outdir / f"{tag}_diecut.png"
        final.save(diecut_path)
        result["diecut"] = str(diecut_path)

        # Версия на ровном зелёном RGB(0,177,64) ПОВЕРХ вырезки (кодом, всегда одна и
        # та же — независимо от того, какой хромакей был при генерации). ongreen —
        # ПРЕВЬЮ, остаётся в исходном (не апскейленном) размере намеренно.
        ongreen = Image.new("RGBA", final.size, (0, 177, 64, 255))
        ongreen.alpha_composite(final)
        ongreen_path = outdir / f"{tag}_ongreen.png"
        ongreen.convert("RGB").save(ongreen_path)
        result["ongreen"] = str(ongreen_path)

        # Апскейл до печатных 300 DPI (upscale.py, portable realesrgan-ncnn-vulkan) —
        # <tag>_print.png = адаптивный апскейл (x4 realesrgan + Lanczos-досчёт до
        # config.PRINT_MIN_SIDE, см. upscale.upscale_to_print_min, пятнадцатый заход)
        # поверх diecut. config.UPSCALE=off отключает; отсутствие exe/модели на диске
        # ИЛИ таймаут/сбой subprocess -> upscale_to_print_min САМА откатывается на
        # чистый Lanczos с исходника (result["ok"]=True, result["print_fallback"]=True)
        # — печатный размер гарантирован даже без realesrgan, просто хуже качеством
        # (задача лида: "при таймауте/сбое — Lanczos-фолбэк ... + предупреждение").
        result["print_png"] = None
        result["print_fallback"] = False
        if config.UPSCALE:
            up_path = outdir / f"{tag}_print.png"
            up_res = upscale.upscale_to_print_min(
                diecut_path, up_path, min_side=config.PRINT_MIN_SIDE,
                scale=config.UPSCALE_SCALE, model=config.UPSCALE_MODEL,
                timeout=config.UPSCALE_TIMEOUT)
            result["print_fallback"] = bool(up_res.get("print_fallback"))
            if up_res["ok"]:
                result["print_png"] = str(up_path)
                if up_res["print_fallback"]:
                    print(f"{p} !! апскейл: realesrgan недоступен/упал, Lanczos-фолбэк "
                          f"до {config.PRINT_MIN_SIDE}px -> {up_path.name} "
                          f"({up_res['out_size']}, {up_res['elapsed_sec']}с, "
                          f"причина: {up_res['error']})", flush=True)
                else:
                    print(f"{p} апскейл x{config.UPSCALE_SCALE} -> {up_path.name} "
                          f"({up_res['out_size']}, {up_res['elapsed_sec']}с)", flush=True)
            else:
                print(f"{p} !! апскейл пропущен: {up_res['error']}", flush=True)

        print(f"{p} ok -> {raw_path.name} + diecut.png + ongreen.png"
              f"{' + print.png' if result['print_png'] else ''} "
              f"(slogan={slogan!r}, text_fallback={text_fallback})", flush=True)
        result["ok"] = True
        return result
    except Exception as e:  # noqa: BLE001
        print(f"{p} !! вырезка/типографика упала: {e} (raw сохранён)", flush=True)
        result["error"] = str(e)
        return result


def run_one(design: dict, idx: int, outdir: Path, timeout_retries: int,
            text_style: str, no_juice: bool) -> bool:
    """Обёртка над render_design для CLI-батча (нумерованный tag "01", "02", ...).
    Возвращает True при успехе (все 4 файла сохранены)."""
    tag = f"{idx:02d}"
    res = render_design(design, tag, outdir, timeout_retries, text_style, no_juice,
                        log_prefix=f"[{tag}]")
    return res["ok"]


def main() -> None:
    # Windows-консоль (cp1251/cp866) не кодирует эмодзи/кану в выводе — не падать,
    # а заменять некодируемые символы.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True,
                     help="файл с темами: КАЖДАЯ строка = свой персонаж/тема (по одному "
                          "дизайну на строку); строки, начинающиеся с #, — комментарии")
    ap.add_argument("--format", choices=["cutout", "diecut"], default="diecut",
                     help="cutout = просто персонаж на хромакее; diecut = персонаж в "
                          "обрамлении пламени/энергии, образующем силуэт, + слоган снизу")
    ap.add_argument("--text-style", choices=["auto"] + list(typography.STYLES),
                     default="auto",
                     help="типографика слогана: 'auto' (дефолт) — типографика v2 "
                          "(typography.compose_text), режим (none/under/punch/kana_side) "
                          "решает арт-директор ПО КОМПОЗИЦИИ каждого дизайна. Явный стиль "
                          "из v1 (none/anton/kana/comic/tag, typography.py) ПРИОРИТЕТНЕЕ "
                          "text_mode, если передан явно. Игнорируется по сути в cutout "
                          "(слоган всё равно не рисуется художественно, но накладывается "
                          "тем же способом)")
    ap.add_argument("--chroma", choices=["green", "blue"], default=None,
                     help="ручной override цвета хромакея для ВСЕХ дизайнов батча — "
                          "обходит выбор арт-директора и код-предохранитель")
    ap.add_argument("--retries", type=int, default=2,
                     help="сколько доп. попыток генерации при провале QC-гейта границ "
                          "кадра (итого максимум 1+retries попыток на дизайн)")
    ap.add_argument("--no-juice", action="store_true",
                     help="отключить пост-фильтр сочности цвета (Color x1.15, Contrast "
                          "x1.05) — по умолчанию включён")
    args = ap.parse_args()

    print(f"провайдер: {config.IMAGE_PROVIDER}", flush=True)

    outdir = HERE / "out_batch" / time.strftime("%Y%m%d_%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"формат: {args.format}\nвывод: {outdir}\n", flush=True)

    with open(args.file, encoding="utf-8") as f:
        themes = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    print(f"файл тем: {len(themes)} тем(ы), по 1 дизайну на каждую\n", flush=True)

    # Ротация банка стилей (docs/STYLE_BANK.json) — скользящее окно последних
    # style_id по ВСЕМУ батчу тем файла, та же логика, что в daily_prints.py (не
    # давать один стиль дважды подряд); последовательный цикл здесь, поэтому просто
    # snapshot()/record() без гонок потоков.
    recent_styles = art_director.RecentStyles()

    designs = []
    for t in themes:
        print(f"  прошу дизайн для «{t}»...", flush=True)
        try:
            recent = recent_styles.snapshot()
            d = art_director.make_ideas(t, 1, args.format, recent_styles=recent)[0]
            recent_styles.record(d.get("style_id", ""))
            designs.append(d)
        except Exception as e:  # noqa: BLE001
            print(f"  !! пропуск «{t}»: {e}", flush=True)

    ok = 0
    for i, d in enumerate(designs, 1):
        if args.chroma:
            d["chroma"] = args.chroma
        try:
            if run_one(d, i, outdir, args.retries, args.text_style, args.no_juice):
                ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[{i:02d}] ОШИБКА: {e}", flush=True)

    print(f"\nГотово: {ok}/{len(designs)} -> {outdir}")


if __name__ == "__main__":
    main()
