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
import providers               # noqa: E402
import typography              # noqa: E402
import typography_v3           # noqa: E402


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
    только убирает ASCII-пунктуацию и whitespace, не буквы любого алфавита)."""
    import re as _re
    text = text.upper()
    text = _re.sub(r"[\r\n\t]+", " ", text)
    # Убираем пунктуацию/апострофы (ASCII), но НЕ буквы/цифры/CJK/катакану/пробелы.
    text = _re.sub(r"[^\w\s]", "", text, flags=_re.UNICODE)
    text = _re.sub(r"_", "", text)  # \w включает "_", слоганы его не содержат
    text = _re.sub(r"\s+", " ", text).strip()
    return text


def _count_nonoverlapping(haystack: str, needle: str) -> int:
    """Число НЕПЕРЕСЕКАЮЩИХСЯ вхождений needle в haystack (str.count делает ровно
    это — считает от найденной позиции + len(needle), не пересекающиеся вхождения не
    складываются в завышенное число для коротких/повторяющихся фраз)."""
    if not needle:
        return 0
    return haystack.count(needle)


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
    нужно)."""
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
    ok = not missing and not duplicated
    if not ok:
        reasons = []
        if missing:
            reasons.append(f"отсутствуют фразы {missing!r}")
        if duplicated:
            reasons.append(f"фразы повторены ДВАЖДЫ+ (дубль на картинке) {duplicated!r}")
        print(f"  OCR-контроль: транскрипт={transcript!r} — {'; '.join(reasons)}",
              flush=True)
    return ok


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


# ── Генерация одного дизайна (переиспользуемое ядро пайплайна) ──────────────────

def render_design(design: dict, tag: str, outdir: Path, timeout_retries: int = 2,
                   text_style: str = "auto", no_juice: bool = False,
                   log_prefix: str = "") -> dict:
    """Полный цикл ОДНОГО дизайна: генерация (с QC-гейтом границ) -> вырезка ->
    типографика -> 4 файла (tag_raw.png/tag_diecut.png/tag_ongreen.png/tag_design.json)
    в outdir. Переиспользуется и CLI-батчем (run_one ниже), и daily_prints.py.

    design: dict из art_director.make_ideas (prompt/chroma/slogan/slogan_color/kana/
    character_en/title_en/signature_props/text_mode). Если character_en непусто —
    перед генерацией достаётся каноничный референс-портрет персонажа
    (character_ref.get_reference) и рисование идёт ПО РЕФЕРЕНСУ (см.
    providers.generate_image(reference=...)), иначе как раньше.
    tag: базовое имя файлов без расширения (напр. "01" или "0137_kenpachi").

    text_style: "auto" (дефолт) — типографика v2 (typography.compose_text), режим
    берётся из design["text_mode"] (Claude решает ПО КОМПОЗИЦИИ конкретного дизайна:
    none/under/punch/kana_side). Если передан ЯВНЫЙ стиль из typography.STYLES
    (none/anton/kana/comic/tag, напр. через CLI --text-style) — он ПРИОРИТЕТНЕЕ
    text_mode (обратная совместимость v1).

    Возвращает {"ok": bool, "attempts": int, "coverage": float, "error": str|None,
    "raw": path|None, "diecut": path|None, "ongreen": path|None, "design_json": path,
    "text_fallback": bool} — attempts нужен вызывающему коду для точного учёта
    стоимости (QC-ретраи И OCR-ретраи спеллинга считаются в один общий счётчик).
    text_fallback=True — TEXT_RENDER=image не сошёлся по OCR-контролю спеллинга за
    все попытки, финальная картинка сгенерирована БЕЗ встроенного текста и текст
    наложен кодовой типографикой (см. раздел «Текст в генерации» в README)."""
    p = log_prefix or f"[{tag}]"
    design_json_path = outdir / f"{tag}_design.json"
    design_json_path.write_text(
        json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {"ok": False, "attempts": 0, "coverage": 0.0, "error": None,
              "raw": None, "diecut": None, "ongreen": None,
              "design_json": str(design_json_path), "text_fallback": False}

    prompt = art_director.build_prompt(design)
    seed = random.randint(0, 2**31 - 1)

    # Рисование ПО РЕФЕРЕНСУ: если арт-директор распознал конкретного вымышленного
    # персонажа (character_en непусто), достаём каноничный портрет (Jikan/AniList,
    # см. character_ref.py) и подмешиваем его в запрос — иначе nano-banana рисует
    # персонажа «по мотивам» (лицо/канон-приметы приблизительные). Любой сбой поиска
    # референса -> None, генерация идёт по чистому тексту, как раньше (не падаем).
    reference = None
    character_en = str(design.get("character_en") or "").strip()
    if character_en:
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

    # TEXT_RENDER=image (десятый заход): встроенная типографика ожидает конкретные
    # фразы на картинке — OCR-контроль спеллинга (_verify_text) проверяет каждую
    # попытку в ТОМ ЖЕ цикле, что QC-гейт границ. Если design не просит текст
    # (type_spec пуст) — expected_phrases пуст, OCR всегда True (нечего проверять).
    text_render_image = config.TEXT_RENDER == "image"
    expected_phrases = _expected_text_phrases(design) if text_render_image else []

    # QC-гейт границ кадра + OCR-контроль спеллинга: до timeout_retries доп. попыток
    # генерации (итого максимум 1+timeout_retries попыток), берём попытку с
    # максимальным coverage рамки хромакеем СРЕДИ попыток, прошедших OCR (если
    # expected_phrases непуст) — попытка с провальным OCR не выбирается лучшей, пока
    # есть хоть одна прошедшая; если НИ ОДНА не прошла OCR — берём лучшую по coverage
    # как есть (сработает text-fallback ниже).
    best_img, best_cov, best_seed = None, -1.0, seed
    best_img_ocr_ok, best_cov_ocr_ok = None, -1.0
    attempts_used = 0
    for attempt in range(1 + timeout_retries):
        try_seed = seed + 101 * attempt
        attempts_used += 1
        try:
            print(f"{p} генерация... seed={try_seed} chroma={design['chroma']} "
                  f"slogan={design.get('slogan')!r}", flush=True)
            attempt_img = providers.generate_image(prompt, seed=try_seed, reference=reference)
        except Exception as e:  # noqa: BLE001
            print(f"{p} !! попытка {attempt + 1} упала: {e}", flush=True)
            result["error"] = str(e)
            continue
        cov = _border_chroma_coverage(attempt_img, chroma=design["chroma"])
        print(f"{p} border coverage={cov:.2f}", flush=True)
        if cov > best_cov:
            best_img, best_cov, best_seed = attempt_img, cov, try_seed

        ocr_ok = True
        if expected_phrases:
            # OCR-вызов — отдельный факт, НЕ платный image-вызов; НЕ увеличивает
            # attempts_used (тот считает именно image-генерации для сметы
            # daily_prints/COST_PER_IMAGE_USD, OCR копеечный текстовый вызов
            # gemini-2.5-flash) — сам факт вызова залогирован print()'ом ниже.
            ocr_ok = _verify_text(attempt_img, expected_phrases)
            print(f"{p} OCR-контроль спеллинга: {'OK' if ocr_ok else 'провал'} "
                  f"(факт вызова залогирован)", flush=True)
        if ocr_ok and cov > best_cov_ocr_ok:
            best_img_ocr_ok, best_cov_ocr_ok = attempt_img, cov

        if cov >= 0.90 and ocr_ok:
            break
        if attempt < timeout_retries:
            reason = []
            if cov < 0.90:
                reason.append(f"coverage {cov:.2f} < 0.90")
            if not ocr_ok:
                reason.append("OCR не сошёлся")
            print(f"{p} {' и '.join(reason)} -> retry", flush=True)

    result["attempts"] = attempts_used
    result["coverage"] = best_cov

    if best_img is None:
        print(f"{p} !! ни одна попытка не дала картинку — пропуск", flush=True)
        result["error"] = result["error"] or "нет изображения ни на одной попытке"
        return result

    # Выбор финального изображения + решение про text-fallback.
    text_fallback = False
    if expected_phrases and best_img_ocr_ok is not None:
        # Хотя бы одна попытка прошла OCR — используем ЛУЧШУЮ ИЗ НИХ (не просто лучшую
        # по coverage, чтобы не выпустить принт с неверным спеллингом ради чуть более
        # ровной рамки).
        raw_img, best_cov = best_img_ocr_ok, best_cov_ocr_ok
    elif expected_phrases:
        # НИ ОДНА попытка не прошла OCR за все timeout_retries+1 генераций — честный
        # откат: одна ДОПОЛНИТЕЛЬНАЯ генерация БЕЗ текст-блока (design с пустым
        # type_spec/quote -> art_director.build_prompt добавит обычный запрет букв),
        # текст наносится кодовой типографикой (typography_v3/typography) ниже, как
        # раньше при TEXT_RENDER=code.
        print(f"{p} !! текст в генерации не сошёлся за {attempts_used} попыток "
              f"(OCR) — откат на кодовый путь (доп. генерация БЕЗ текст-блока)",
              flush=True)
        fallback_design = dict(design)
        fallback_design["type_spec"] = ""
        fallback_prompt = art_director.build_prompt(fallback_design)
        fb_seed = seed + 101 * (1 + timeout_retries)
        try:
            attempts_used += 1
            fb_img = providers.generate_image(fallback_prompt, seed=fb_seed,
                                              reference=reference)
            fb_cov = _border_chroma_coverage(fb_img, chroma=design["chroma"])
            print(f"{p} фолбэк-генерация (без текста) border coverage={fb_cov:.2f}",
                  flush=True)
            raw_img, best_cov = fb_img, fb_cov
            text_fallback = True
        except Exception as e:  # noqa: BLE001
            print(f"{p} !! фолбэк-генерация тоже упала: {e} — используем лучшую "
                  f"попытку по coverage как есть (спеллинг НЕ подтверждён)", flush=True)
            raw_img = best_img
    else:
        raw_img = best_img

    result["attempts"] = attempts_used
    result["coverage"] = best_cov
    result["text_fallback"] = text_fallback
    if best_cov < 0.90:
        print(f"{p} !! warning: лучшая попытка coverage={best_cov:.2f} < 0.90, "
              f"используем её (seed={best_seed})", flush=True)

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
    apply_code_typography = (not text_render_image) or text_fallback or not expected_phrases

    try:
        cut = chroma_remove.cutout_green(raw_img, tol=52.0).convert("RGBA")
        # protect_text_islands: при TEXT_RENDER=image и реально ожидаемом встроенном
        # тексте (expected_phrases непуст И текст НЕ ушёл в фолбэк) буквы у края
        # фигуры могут лежать отдельными альфа-островами — не срезать их наравне с
        # мусором водяных знаков (см. drop_small_islands/_TEXT_ISLAND_PROTECT_FRAC).
        protect_islands = text_render_image and bool(expected_phrases) and not text_fallback
        cut = drop_small_islands(cut, protect_text_islands=protect_islands)
        if not no_juice:
            cut = juice(cut)

        slogan = design.get("slogan", "")
        slogan_color = design.get("slogan_color", "orange")
        kana = design.get("kana", "")

        if not apply_code_typography:
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
        # та же — независимо от того, какой хромакей был при генерации).
        ongreen = Image.new("RGBA", final.size, (0, 177, 64, 255))
        ongreen.alpha_composite(final)
        ongreen_path = outdir / f"{tag}_ongreen.png"
        ongreen.convert("RGB").save(ongreen_path)
        result["ongreen"] = str(ongreen_path)

        print(f"{p} ok -> {raw_path.name} + diecut.png + ongreen.png "
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

    designs = []
    for t in themes:
        print(f"  прошу дизайн для «{t}»...", flush=True)
        try:
            d = art_director.make_ideas(t, 1, args.format)[0]
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
