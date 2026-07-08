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

def _border_chroma_coverage(img_rgb: Image.Image, tol: float = 52.0) -> float:
    """Доля пикселей рамки кадра (ширина 1% от min(W,H), сэмплинг каждые 4px),
    близких к цвету хромакея — QC-гейт против «персонаж/эффекты упираются в край
    кадра, хромакей-фона не осталось». tol=52 — то же калиброванное значение, что
    и в cutout_green (НЕ менять — подобрано под nanobanana)."""
    rgb = np.array(img_rgb.convert("RGB"))
    h, w = rgb.shape[:2]
    key = chroma_remove._border_key(rgb).astype(np.uint8)
    keyy = cv2.cvtColor(key.reshape(1, 1, 3), cv2.COLOR_RGB2YCrCb)[0, 0].astype(np.float32)
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


def drop_small_islands(rgba: Image.Image, min_frac: float = 0.002) -> Image.Image:
    """Убрать мусорные острова после хромакей-вырезки (водяные знаки, крапинки по
    углам) — остаётся главный силуэт и всё крупнее min_frac от него. Перенесено
    дословно из comfyui-print-server/batch_print.py."""
    a = np.array(rgba.getchannel("A"))
    n, labels, stats, _ = cv2.connectedComponentsWithStats((a > 0).astype(np.uint8), 8)
    if n <= 2:
        return rgba
    areas = stats[1:, cv2.CC_STAT_AREA]
    kill = [i + 1 for i, ar in enumerate(areas) if ar < areas.max() * min_frac]
    if not kill:
        return rgba
    a[np.isin(labels, kill)] = 0
    out = rgba.copy()
    out.putalpha(Image.fromarray(a))
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
    "raw": path|None, "diecut": path|None, "ongreen": path|None, "design_json": path}
    — attempts нужен вызывающему коду для точного учёта стоимости (QC-ретраи считаются)."""
    p = log_prefix or f"[{tag}]"
    design_json_path = outdir / f"{tag}_design.json"
    design_json_path.write_text(
        json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {"ok": False, "attempts": 0, "coverage": 0.0, "error": None,
              "raw": None, "diecut": None, "ongreen": None,
              "design_json": str(design_json_path)}

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

    # QC-гейт границ кадра: до timeout_retries доп. попыток генерации (итого максимум
    # 1+timeout_retries попыток), берём попытку с максимальным coverage рамки хромакеем.
    best_img, best_cov, best_seed = None, -1.0, seed
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
        cov = _border_chroma_coverage(attempt_img)
        print(f"{p} border coverage={cov:.2f}", flush=True)
        if cov > best_cov:
            best_img, best_cov, best_seed = attempt_img, cov, try_seed
        if cov >= 0.90:
            break
        if attempt < timeout_retries:
            print(f"{p} coverage {cov:.2f} < 0.90 -> retry", flush=True)

    result["attempts"] = attempts_used
    result["coverage"] = best_cov

    if best_img is None:
        print(f"{p} !! ни одна попытка не дала картинку — пропуск", flush=True)
        result["error"] = result["error"] or "нет изображения ни на одной попытке"
        return result
    if best_cov < 0.90:
        print(f"{p} !! warning: лучшая попытка coverage={best_cov:.2f} < 0.90, "
              f"используем её (seed={best_seed})", flush=True)

    raw_img = best_img
    raw_path = outdir / f"{tag}_raw.png"
    raw_img.save(raw_path)
    result["raw"] = str(raw_path)

    try:
        cut = chroma_remove.cutout_green(raw_img, tol=52.0).convert("RGBA")
        cut = drop_small_islands(cut)
        if not no_juice:
            cut = juice(cut)

        slogan = design.get("slogan", "")
        slogan_color = design.get("slogan_color", "orange")
        kana = design.get("kana", "")

        if text_style == "auto":
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
              f"(slogan={slogan!r})", flush=True)
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
