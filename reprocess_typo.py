#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reprocess_typo.py — БЕСПЛАТНАЯ проверка типографики v2/v3 на уже оплаченных raw.

Читает пары NN_raw.png + NN_design.json из --src (дамп прошлого батча,
batch_print.render_design уже сохраняет оба файла на каждый дизайн) и заново
прогоняет вырезку (chroma_remove.cutout_green + drop_small_islands + juice — ТОТ ЖЕ
пайплайн, что batch_print.render_design) и типографику (typography.compose_text v2,
либо typography_v3.compose_text_v3, если у дизайна есть v3-поля) БЕЗ повторного
обращения к Claude/nano-banana — ни одного платного вызова.

Выбор типографики на дизайн (по убыванию приоритета):
1. `--mode` (CLI) форсирует ОДИН v2-режим на ВСЕ дизайны (обратная совместимость).
2. Если в design.json есть непустой "text_modes_v3" — типографика v3
   (typography_v3.compose_text_v3) с полями design как есть.
3. Если в design.json есть "text_mode" (v2, новые дампы без v3) — используется он.
4. СТАРЫЕ дампы БЕЗ "text_mode"/"text_modes_v3" (до седьмого захода) — эвристика v3
   round-robin по каноничным комбинациям раздела 3.6 стайлгайда (см.
   _ROUND_ROBIN_V3_COMBOS), поля quote/name_jp/mood достраиваются эвристикой
   (_heuristic_v3_fields) из уже существующих slogan/kana полей старого дампа.

Использование:
  python reprocess_typo.py --src out_batch/20260708_153106 --dst out_batch/reprocessed
  python reprocess_typo.py --src out_batch/20260708_153106 --dst out_batch/reprocessed --mode punch
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from PIL import Image  # noqa: E402

import batch_print  # noqa: E402
import chroma_remove  # noqa: E402
import config  # noqa: E402
import typography  # noqa: E402
import typography_v3  # noqa: E402

# Режимы по кругу для СТАРЫХ дампов без "text_mode"/"text_modes_v3" (до седьмого
# захода) — показывает v2-режимы на живых картинках одного батча.
_ROUND_ROBIN_MODES = ("under", "punch", "kana_side")

# Канонические комбинации v3 (docs/PRINT_STYLE_GUIDE.md раздел 3.6) для round-robin
# эвристики на СТАРЫХ дампах без text_modes_v3 — наглядно показывает разные
# допустимые комбинации на одном батче, не один и тот же режим на всех.
_ROUND_ROBIN_V3_COMBOS = (
    ["quote_bottom", "kanji_on", "collection_footer"],
    ["editorial"],
    ["kanji_on", "collection_footer"],  # ghost-вариант выставляется через mood=pop_trash
    ["quote_bottom", "collection_footer"],
)


def _heuristic_v3_fields(design: dict) -> dict:
    """Достраивает v3-поля (quote/name_jp/mood) эвристикой из существующих
    slogan/kana старого дампа (design.json до седьмого захода, без v3-полей вовсе).
    Не трогает design на месте — возвращает НОВЫЙ dict (копия + добавленные поля)."""
    d = dict(design)
    if not str(d.get("quote") or "").strip():
        d["quote"] = str(d.get("slogan") or "").strip()
    if not str(d.get("name_jp") or "").strip():
        d["name_jp"] = str(d.get("kana") or "").strip()
    if not str(d.get("mood") or "").strip():
        d["mood"] = "duotone_quote"
    return d


def _find_pairs(src: Path) -> list:
    """Находит пары (raw_path, design_json_path) по общему префиксу NN_ в src —
    тот же формат имён, что пишет batch_print.render_design."""
    pairs = []
    for design_path in sorted(src.glob("*_design.json")):
        tag = design_path.name[: -len("_design.json")]
        raw_path = src / f"{tag}_raw.png"
        if raw_path.exists():
            pairs.append((tag, raw_path, design_path))
        else:
            print(f"  !! пропуск {tag}: нет {raw_path.name}", flush=True)
    return pairs


def reprocess_one(tag: str, raw_path: Path, design_path: Path, dst: Path,
                   mode_override: str, idx: int, no_juice: bool = False) -> dict:
    """Один дизайн: raw.png + design.json -> вырезка (тот же пайплайн, что
    render_design) -> типографика (v2 ИЛИ v3, см. модуль-docstring про приоритет) ->
    tag_diecut.png + tag_ongreen.png в dst. НИКАКИХ платных вызовов (Claude/
    nano-banana) — raw уже оплачен и лежит на диске."""
    design = json.loads(design_path.read_text(encoding="utf-8"))
    raw_img = Image.open(raw_path).convert("RGB")

    # Тот же пайплайн вырезки, что batch_print.render_design (дословно, чтобы
    # результат был сравним 1:1 с боевым прогоном, не выдумываем свою логику).
    cut = chroma_remove.cutout_green(raw_img, tol=52.0).convert("RGBA")
    cut = batch_print.drop_small_islands(cut)
    if not no_juice:
        cut = batch_print.juice(cut)

    slogan = design.get("slogan", "")
    slogan_color = design.get("slogan_color", "orange")
    kana = design.get("kana", "")

    if mode_override:
        # CLI --mode форсирует v2-режим на ВСЕ дизайны (обратная совместимость,
        # приоритетнее и над text_mode, и над text_modes_v3 дампа).
        text_mode = mode_override
        final = typography.compose_text(cut, text_mode, slogan, slogan_color, kana)
        label = f"text_mode={text_mode!r}"
    elif design.get("text_modes_v3"):
        # Дамп уже содержит v3-поля (седьмой+ заход, реальный design от art_director) —
        # используем typography_v3 как есть, без эвристик.
        modes_v3 = design["text_modes_v3"]
        final = typography_v3.compose_text_v3(cut, modes_v3, design,
                                              brand_label=config.BRAND_LABEL)
        label = f"text_modes_v3={modes_v3!r} mood={design.get('mood')!r}"
    elif "text_mode" in design:
        # Дамп v2 (седьмой заход, ДО typography v3) — использует v2 путь как есть,
        # text_modes_v3 в нём просто отсутствовал на момент генерации.
        text_mode = design["text_mode"]
        final = typography.compose_text(cut, text_mode, slogan, slogan_color, kana)
        label = f"text_mode={text_mode!r}"
    else:
        # СТАРЫЙ дамп БЕЗ text_mode/text_modes_v3 (до седьмого захода) — эвристика v3:
        # round-robin по каноничным комбинациям раздела 3.6 стайлгайда + достройка
        # quote/name_jp/mood из существующих slogan/kana (_heuristic_v3_fields).
        combo = _ROUND_ROBIN_V3_COMBOS[(idx - 1) % len(_ROUND_ROBIN_V3_COMBOS)]
        design_v3 = _heuristic_v3_fields(design)
        if combo == ["kanji_on", "collection_footer"]:
            design_v3["mood"] = "pop_trash"  # ghost-вариант kanji_on, раздел 3.2а
        final = typography_v3.compose_text_v3(cut, combo, design_v3,
                                              brand_label=config.BRAND_LABEL)
        label = f"heuristic_v3={combo!r} mood={design_v3.get('mood')!r}"

    diecut_path = dst / f"{tag}_diecut.png"
    final.save(diecut_path)

    ongreen = Image.new("RGBA", final.size, (0, 177, 64, 255))
    ongreen.alpha_composite(final)
    ongreen_path = dst / f"{tag}_ongreen.png"
    ongreen.convert("RGB").save(ongreen_path)

    print(f"[{tag}] {label} slogan={slogan!r} -> "
          f"{diecut_path.name} + {ongreen_path.name}", flush=True)
    return {"tag": tag, "label": label, "diecut": str(diecut_path),
            "ongreen": str(ongreen_path)}


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True,
                     help="папка с NN_raw.png + NN_design.json (дамп прошлого батча)")
    ap.add_argument("--dst", required=True,
                     help="куда сохранить NN_diecut.png + NN_ongreen.png")
    ap.add_argument("--mode", choices=list(typography.TEXT_MODES), default=None,
                     help="форсировать ОДИН режим типографики на ВСЕ дизайны (по "
                          "умолчанию — text_mode из design.json, либо round-robin "
                          "under/punch/kana_side для старых дампов без этого поля)")
    ap.add_argument("--no-juice", action="store_true",
                     help="отключить пост-фильтр сочности цвета (как в batch_print.py)")
    args = ap.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)
    dst.mkdir(parents=True, exist_ok=True)

    pairs = _find_pairs(src)
    if not pairs:
        print(f"!! в {src} не найдено ни одной пары NN_raw.png + NN_design.json",
              flush=True)
        return

    print(f"найдено {len(pairs)} дизайн(ов) в {src} -> {dst}\n", flush=True)
    for idx, (tag, raw_path, design_path) in enumerate(pairs, 1):
        try:
            reprocess_one(tag, raw_path, design_path, dst, args.mode, idx, args.no_juice)
        except Exception as e:  # noqa: BLE001
            print(f"[{tag}] !! ошибка: {e}", flush=True)

    print(f"\nГотово -> {dst}")


if __name__ == "__main__":
    main()
