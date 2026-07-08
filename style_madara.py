#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""style_madara.py — прогон 10 стилевых рецептов Мадары Учихи (docs/MADARA_RECIPES.json)
через боевой путь batch_print.render_design (референс персонажа подтягивается сам через
character_ref, QC-гейт границ хромакея, OCR-контроль спеллинга, вырезка фона) —
каждый рецепт задаёт РАДИКАЛЬНО разное размещение типографики относительно фигуры
(не только смену шрифта), проверка гипотезы дизайнера про "скучный" плейсмент.

Каждый рецепт docs/MADARA_RECIPES.json превращается в design-dict формата
art_director/batch_print (те же ключи, что в out_batch/daily_*/*_design.json):
prompt (сцена: moment + art_style + палитра словами, канон-приметы), chroma,
slogan/quote (= text_content.main), slogan_color, kana/name_jp (= text_content.vertical_jp,
для OCR), character_en/title_en (для character_ref-референса), signature_props (gunbai
war fan, где рецепт его упоминает), text_mode/text_modes_v3/mood/type_spec (typography-
поля пайплайна — заполнены так, чтобы _exact_spelling_phrase/_expected_text_phrases
взяли РОВНО текст рецепта, type_spec = typography-абзац рецепта почти дословно).

Результат — out_batch/madara_styles/<id>_raw.png + _diecut.png + _ongreen.png +
_design.json (через render_design) + summary.json (сводка по всем 10).

Использование:
  python style_madara.py                 # все 10 рецептов, 2 потока
  python style_madara.py --workers 1      # последовательно
  python style_madara.py --only 01_gothic_gold 09_ring_medallion_arc
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# cp1251-консоль Windows — та же защита, что в batch_print.py/daily_prints.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

import batch_print  # noqa: E402

RECIPES_PATH = HERE / "docs" / "MADARA_RECIPES.json"
OUTDIR = HERE / "out_batch" / "madara_styles"

CHARACTER_EN = "Madara Uchiha"
TITLE_EN = "Naruto"

# slogan/slogan_color санация в art_director._parse режет slogan регексом
# [^A-Za-z0-9 !?'\-] и обрезает до 34 симв — рецепт 06 ("DANCE, MADARA!" содержит
# запятую) и 04/08 (длиннее 34 симв) идут через это же правило здесь, чтобы файлы
# design.json оставались совместимы с остальным пайплайном (typography.py и т.п.,
# на случай текст-фолбэка ниже).
_SLOGAN_SANITIZE_RE = re.compile(r"[^A-Za-z0-9 !?'\-]")


def _sanitize_slogan(text: str) -> str:
    return _SLOGAN_SANITIZE_RE.sub("", text).strip()[:34]


def load_recipes() -> list:
    with open(RECIPES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _mood_for_recipe(art_style: str, typography: str) -> str:
    """mood — только влияет на typography_v3-фолбэк (если встроенный текст не сойдётся
    по OCR за все попытки, batch_print откатывается на кодовую типографику, которая
    красит текст по palette.extract_palette независимо от mood — mood здесь чисто
    информационный, для design.json). Грубая эвристика по ключевым словам стиля рецепта,
    не участвует в самой генерации (type_spec ниже уже содержит собственный точный
    typography-абзац рецепта, mood не переиспользуется в промпте)."""
    blob = f"{art_style} {typography}".lower()
    if any(k in blob for k in ("fashion", "editorial", "vogue", "minimal")):
        return "fashion_editorial"
    if any(k in blob for k in ("stencil", "grunge", "manga", "trash", "screentone")):
        return "pop_trash"
    return "duotone_quote"


def recipe_to_design(recipe: dict) -> dict:
    """docs/MADARA_RECIPES.json[i] -> design-dict формата art_director/batch_print
    (см. docstring модуля). Единственный источник текста type_spec/quote/slogan —
    typography+text_content самого рецепта, ничего не придумываем поверх."""
    text_content = recipe.get("text_content", {}) or {}
    main = str(text_content.get("main") or "").strip()
    secondary = str(text_content.get("secondary") or "").strip()
    vertical_jp = str(text_content.get("vertical_jp") or "").strip()

    moment = str(recipe.get("moment") or "").strip()
    art_style = str(recipe.get("art_style") or "").strip()
    palette = str(recipe.get("palette") or "").strip()
    typography_spec = str(recipe.get("typography") or "").strip()
    chroma = str(recipe.get("chroma") or "green").strip().lower()
    chroma = chroma if chroma in ("green", "blue") else "green"

    # ── КИНЕМАТОГРАФИЧНАЯ ПРОЗА сцены (без типографики — та идёт в type_spec, как в
    # обычном пайплайне art_director.build_prompt собирает их раздельно). Прямым
    # текстом называем пол (мужчина) и канон-приметы согласно требованиям
    # art_director._COMMON_RULES_BASE (2-3 приметы, точный термин оружия).
    prompt = (
        f"An adult man, Madara Uchiha from Naruto, is shown as follows: {moment} "
        f"Rendered in {art_style}. Palette: {palette}, rich saturated anime cel-shading "
        f"colors with bold clean ink outlines, no pastel softness. The full figure is "
        f"completely unclipped inside the frame, with generous even chroma-key margin "
        f"on all four sides — nothing touching the frame edge. There is only one "
        f"subject in the frame — no companions, no background figures, no secondary "
        f"transformations beside him."
    )

    # signature_props — только для рецептов, где gunbai war fan реально часть сцены
    # (дизайнер прописал термин в moment для 04/06) — точный канон-термин, не
    # обобщённое "fan"/"weapon" (правило art_director._signature_props_schema).
    signature_props = ""
    if "gunbai" in moment.lower():
        signature_props = (
            "his gunbai war fan: a broad rigid battle fan with a rounded fan-shaped "
            "head split by a curved dividing line, red and white/black paneled "
            "coloring, a chain-and-weight (kusari-fundo) hanging from the base, "
            "held or swung as a weapon, not a decorative prop"
        )

    # ── Типографика: exact-spelling фраза = main (приоритет как quote в
    # art_director._exact_spelling_phrase), вторичная фраза (если есть) добавляется
    # прямым текстом внутри type_spec САМОГО рецепта (typography_spec уже описывает,
    # где и как рисовать secondary — см. рецепты 04/06/08). name_jp = vertical_jp
    # (для OCR-проверки и кандзи-колонки).
    quote = _sanitize_slogan(main) if main else ""
    # main может быть иероглифом/катаканой (рецепт 02: "舞") — _sanitize_slogan режет
    # неё регексом ASCII-only, тогда quote потерял бы CJK-символ. Для non-ASCII main
    # используем его КАК ЕСТЬ (санация design.json полей в art_director._parse не
    # применяется здесь — этот скрипт строит design-dict напрямую, не через
    # art_director.make_ideas).
    if main and not quote:
        quote = main

    slogan = quote  # slogan-поле = та же фраза (typography-фолбэк читает design["slogan"])
    name_jp = vertical_jp

    exact_spelling_notes = [f"Spell it EXACTLY, letter by letter, exactly once: \"{main}\"."] if main else []
    if secondary:
        exact_spelling_notes.append(
            f"The secondary line reads EXACTLY, letter by letter, exactly once: \"{secondary}\"."
        )
    if vertical_jp:
        exact_spelling_notes.append(
            f"The vertical Japanese column reads EXACTLY, character by character, top to "
            f"bottom, exactly once: {vertical_jp}."
        )

    type_spec = (
        f"{typography_spec} " + " ".join(exact_spelling_notes) +
        " Leave clear, visible spacing between words within the same line — words "
        "must never touch or merge together. Each text element listed above appears "
        "EXACTLY ONCE on the whole composition — no duplicate smaller echo copies "
        "anywhere else. No other text anywhere beyond what is specified above."
    ).strip()

    mood = _mood_for_recipe(art_style, typography_spec)

    # text_mode — ТОЛЬКО влияет на кодовый typography-фолбэк (TEXT_RENDER=image
    # применяет его лишь если встроенный текст не сошёлся по OCR за все попытки,
    # см. batch_print.render_design/apply_code_typography). "punch" — есть main-фраза
    # (обычный слоган впритык к фигуре снизу); "kana_side" — main пуст, но есть
    # катакана/кандзи по краю фигуры (рецепт 05 — вся типографика держится на
    # vertical_jp, без этого фолбэк рисовал бы вообще без текста); "none" — рецепту
    # правда нечего показать кодом (просто не сошлось бы ничего осмысленного).
    kana = vertical_jp if re.fullmatch(r"[゠-ヿー・ ]+", vertical_jp or "") else ""
    if main:
        text_mode = "punch"
    elif kana:
        text_mode = "kana_side"
    else:
        text_mode = "none"

    return {
        "prompt": prompt,
        "chroma": chroma,
        "slogan": slogan,
        "slogan_color": "orange",
        "kana": kana,
        "character_en": CHARACTER_EN,
        "title_en": TITLE_EN,
        "signature_props": signature_props,
        "text_mode": text_mode,
        "text_modes_v3": [],
        "quote": quote,
        "name_jp": name_jp,
        "mood": mood,
        "type_spec": type_spec,
        # Поля скрипта (не часть стандартной схемы art_director, но не мешают
        # render_design — тот читает только известные ключи design.get(...)):
        "_recipe_id": recipe.get("id", ""),
        "_recipe_name": recipe.get("name", ""),
    }


def run_recipe(recipe: dict, outdir: Path) -> dict:
    tag = recipe.get("id", "unknown")
    design = recipe_to_design(recipe)
    t0 = time.time()
    try:
        res = batch_print.render_design(design, tag, outdir, timeout_retries=2,
                                        text_style="auto", no_juice=False,
                                        log_prefix=f"[{tag}]")
    except Exception as e:  # noqa: BLE001 — один упавший рецепт не блокирует остальные
        print(f"[{tag}] !! render_design упал целиком: {e}", flush=True)
        res = {"ok": False, "attempts": 0, "coverage": 0.0, "error": str(e),
               "raw": None, "diecut": None, "ongreen": None, "text_fallback": False}
    res["id"] = tag
    res["name"] = recipe.get("name", "")
    res["elapsed_sec"] = round(time.time() - t0, 1)
    return res


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2,
                     help="сколько рецептов генерировать параллельно (дефолт 2 — не "
                          "ловить лимиты API)")
    ap.add_argument("--only", nargs="*", default=None,
                     help="прогнать только эти id рецептов (по умолчанию — все 10)")
    args = ap.parse_args()

    recipes = load_recipes()
    if args.only:
        wanted = set(args.only)
        recipes = [r for r in recipes if r.get("id") in wanted]
        missing = wanted - {r.get("id") for r in recipes}
        if missing:
            print(f"!! не найдены id рецептов: {sorted(missing)}", flush=True)

    OUTDIR.mkdir(parents=True, exist_ok=True)
    print(f"рецептов к прогону: {len(recipes)} -> {OUTDIR}\n", flush=True)

    results = []
    if args.workers <= 1:
        for r in recipes:
            results.append(run_recipe(r, OUTDIR))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(run_recipe, r, OUTDIR): r for r in recipes}
            for fut in as_completed(futs):
                results.append(fut.result())

    # Стабильный порядок сводки — по id рецепта (как в исходном JSON), не по порядку
    # завершения потоков.
    order = {r.get("id"): i for i, r in enumerate(recipes)}
    results.sort(key=lambda r: order.get(r["id"], 999))

    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n{'id':<28} {'статус':<6} {'попыт.':<7} {'coverage':<9} {'OCR-фолбэк':<11} ошибка")
    for r in results:
        status = "ok" if r["ok"] else "FAIL"
        fb = "да" if r.get("text_fallback") else "нет"
        err = r.get("error") or ""
        print(f"{r['id']:<28} {status:<6} {r.get('attempts', 0):<7} "
              f"{r.get('coverage', 0.0):<9.2f} {fb:<11} {err}")

    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(results),
        "ok": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }
    summary_path = OUTDIR / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nГотово: {ok_count}/{len(results)} -> {OUTDIR}")
    print(f"сводка: {summary_path}")


if __name__ == "__main__":
    main()
