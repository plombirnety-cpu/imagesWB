#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reprocess_typo.py — БЕСПЛАТНАЯ проверка типографики v2 на уже оплаченных raw.

Читает пары NN_raw.png + NN_design.json из --src (дамп прошлого батча,
batch_print.render_design уже сохраняет оба файла на каждый дизайн) и заново
прогоняет вырезку (chroma_remove.cutout_green + drop_small_islands + juice — ТОТ ЖЕ
пайплайн, что batch_print.render_design) и НОВУЮ типографику (typography.compose_text)
БЕЗ повторного обращения к Claude/nano-banana — ни одного платного вызова.

Режим типографики для показа ВСЕХ трёх режимов v2 на живых картинках: если в
design.json уже есть "text_mode" (новые дампы) — используется он как есть. Если
"text_mode" отсутствует (старые дампы, седьмой заход ещё не существовал) — режимы
назначаются ПО КРУГУ (round-robin: 1-й дизайн -> under, 2-й -> punch, 3-й ->
kana_side, 4-й снова under, ...), чтобы наглядно сравнить все три режима на одном
батче, как просил владелец.

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
import typography  # noqa: E402

# Режимы по кругу для старых дампов без "text_mode" — показывает все три режима v2
# на живых картинках одного батча, а не один и тот же режим на всех.
_ROUND_ROBIN_MODES = ("under", "punch", "kana_side")


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
    render_design) -> compose_text с НОВОЙ типографикой -> tag_diecut.png +
    tag_ongreen.png в dst. НИКАКИХ платных вызовов (Claude/nano-banana) — raw уже
    оплачен и лежит на диске."""
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
        text_mode = mode_override
    elif "text_mode" in design:
        text_mode = design["text_mode"]
    else:
        # Старый дамп без text_mode (до седьмого захода) — назначаем по кругу, чтобы
        # показать ВСЕ три режима v2 на одном батче.
        text_mode = _ROUND_ROBIN_MODES[(idx - 1) % len(_ROUND_ROBIN_MODES)]

    final = typography.compose_text(cut, text_mode, slogan, slogan_color, kana)

    diecut_path = dst / f"{tag}_diecut.png"
    final.save(diecut_path)

    ongreen = Image.new("RGBA", final.size, (0, 177, 64, 255))
    ongreen.alpha_composite(final)
    ongreen_path = dst / f"{tag}_ongreen.png"
    ongreen.convert("RGB").save(ongreen_path)

    print(f"[{tag}] text_mode={text_mode!r} slogan={slogan!r} -> "
          f"{diecut_path.name} + {ongreen_path.name}", flush=True)
    return {"tag": tag, "text_mode": text_mode, "diecut": str(diecut_path),
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
