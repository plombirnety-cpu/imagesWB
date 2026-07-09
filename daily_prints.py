#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily_prints.py — дневной оркестратор print-factory-nb: тренды -> план ->
до PRINTS_PER_DAY принтов (дефолт 500), параллельно (WORKERS потоков), с журналом
прогресса (можно прервать и продолжить в тот же день без повторной оплаты готового)
и предохранителем дневного бюджета.

Пайплайн ОДНОГО задания = тот же, что в batch_print.py (arт-директор -> генерация ->
QC-гейт -> вырезка -> типографика -> raw/diecut/ongreen/design.json) — переиспользован
через batch_print.render_design, здесь ничего не продублировано.

Использование:
  python daily_prints.py                  # полный день (PRINTS_PER_DAY заданий)
  python daily_prints.py --dry-run        # весь контур БЕЗ платных вызовов картинок
  python daily_prints.py --limit 2        # обрезать план (мини-тест)
  python daily_prints.py --limit 2 --dry-run
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import art_director            # noqa: E402
import batch_print             # noqa: E402
import config                  # noqa: E402
import theme_scout              # noqa: E402


def _today_dir() -> Path:
    date_str = time.strftime("%Y-%m-%d")
    d = HERE / "out_batch" / f"daily_{date_str}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _queue_path(outdir: Path) -> Path:
    return outdir / "queue.jsonl"


def _load_queue_status(outdir: Path) -> dict:
    """Читает queue.jsonl (если есть от прежнего запуска в тот же день) -> dict
    {task_tag: последняя запись-статус}. Используется для докрутки без повторной
    оплаты уже done-заданий."""
    path = _queue_path(outdir)
    status = {}
    if not path.exists():
        return status
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
                status[rec["tag"]] = rec
            except Exception:  # noqa: BLE001
                continue
    return status


def _append_queue(outdir: Path, record: dict) -> None:
    """Дописывает одну строку в queue.jsonl (append — журнал накопительный, каждый
    прогон дописывает новые события поверх старых)."""
    with open(_queue_path(outdir), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _task_tag(idx: int, theme: str) -> str:
    """Стабильный tag для файлов задания: NNNN + короткий транслит-слаг темы (для
    читаемости имён файлов в out_batch/daily_*/)."""
    slug = "".join(ch if ch.isalnum() else "_" for ch in theme.lower())[:24].strip("_")
    return f"{idx:04d}_{slug}" if slug else f"{idx:04d}"


def build_cost_estimate(n_tasks: int) -> tuple:
    """Плановая смета (USD) на n_tasks заданий по цене текущего провайдера. Возвращает
    (стоимость_за_шт, итого). Не биллинговая точность — оценка для предохранителя."""
    per_item = config.COST_PER_IMAGE_USD.get(config.IMAGE_PROVIDER, 0.14)
    return per_item, per_item * n_tasks


def apply_budget_cap(plan: list, max_cost_usd: float) -> list:
    """Обрезает план так, чтобы плановая смета не превышала max_cost_usd."""
    per_item, total = build_cost_estimate(len(plan))
    if total <= max_cost_usd or per_item <= 0:
        return plan
    allowed_n = int(max_cost_usd / per_item)
    print(f"!! смета {total:.2f}$ превышает потолок MAX_DAILY_COST_USD={max_cost_usd:.2f}$ "
          f"— план обрезан с {len(plan)} до {allowed_n} заданий", flush=True)
    return plan[:allowed_n]


def _process_one(task: dict, idx: int, outdir: Path, dry_run: bool,
                  recent_styles: "art_director.RecentStyles" = None) -> dict:
    """Обрабатывает одно задание плана: арт-директор -> render_design (или заглушка
    в dry-run). Возвращает запись для журнала queue.jsonl.

    recent_styles (art_director.RecentStyles, опционально) — скользящее окно
    последних style_id для ротации банка стилей (docs/STYLE_BANK.json): снимок
    ПЕРЕД make_ideas, запись выбранного style_id ПОСЛЕ (см. RecentStyles docstring
    про потокобезопасность при WORKERS>1). None — ротация отключена (старое
    поведение, style_id не передаётся вовсе — art_director сам решит по banку
    без учёта истории)."""
    theme, fmt = task["theme"], task.get("format", "diecut")
    tag = _task_tag(idx, theme)
    record = {"tag": tag, "theme": theme, "format": fmt,
              "source": task.get("source", "unknown"), "status": "failed",
              "attempts": 0, "error": None, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}

    if dry_run:
        record["status"] = "dry_run_skipped"
        print(f"[{tag}] (dry-run) пропуск платной генерации: {theme!r} format={fmt}",
              flush=True)
        return record

    try:
        recent = recent_styles.snapshot() if recent_styles else None
        design = art_director.make_ideas(theme, 1, fmt, recent_styles=recent)[0]
        if recent_styles:
            recent_styles.record(design.get("style_id", ""))
    except Exception as e:  # noqa: BLE001
        record["error"] = f"арт-директор: {e}"
        print(f"[{tag}] !! арт-директор упал: {e}", flush=True)
        return record

    record["style_id"] = design.get("style_id", "")

    # text_style="auto" — типографика v2 (typography.compose_text), режим (none/under/
    # punch/kana_side) решает арт-директор ПО КОМПОЗИЦИИ design["text_mode"] каждого
    # конкретного задания (см. batch_print.render_design/typography.py, седьмой заход).
    res = batch_print.render_design(design, tag, outdir, timeout_retries=2,
                                    text_style="auto", no_juice=False,
                                    log_prefix=f"[{tag}]")
    record["attempts"] = res["attempts"]
    record["coverage"] = res["coverage"]
    record["raw"] = res["raw"]
    record["diecut"] = res["diecut"]
    record["ongreen"] = res["ongreen"]
    record["design_json"] = res["design_json"]
    record["status"] = "done" if res["ok"] else "failed"
    record["error"] = res["error"]
    return record


def run_daily(target_n: int, workers: int, max_cost_usd: float, dry_run: bool,
              limit: int = None, skip_collect: bool = False) -> None:
    outdir = _today_dir()
    print(f"вывод дня: {outdir}", flush=True)

    print("\n=== 1. сбор трендов + план дня ===", flush=True)
    if not skip_collect and not dry_run:
        theme_scout.collect_trends()
    elif not skip_collect and dry_run:
        # dry-run по-прежнему честно пробует собрать тренды (это бесплатный шаг) —
        # но не валится, если trend-watch недоступен/парсеры упали.
        theme_scout.collect_trends()
    plan = theme_scout.build_daily_plan(target_n)
    if limit is not None:
        plan = plan[:limit]
        print(f"--limit {limit}: план обрезан до {len(plan)} заданий", flush=True)

    n_trend = sum(1 for t in plan if t["source"].startswith("trend"))
    n_pop = sum(1 for t in plan if t["source"].startswith("trend:pop:"))
    n_ever = sum(1 for t in plan if t["source"] == "evergreen")
    print(f"план дня: {len(plan)} заданий ({n_trend} из трендов [{n_pop} pop], "
          f"{n_ever} из evergreen)", flush=True)

    plan_path = outdir / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"план сохранён -> {plan_path}", flush=True)

    print("\n=== 2. смета ===", flush=True)
    per_item, total = build_cost_estimate(len(plan))
    print(f"провайдер={config.IMAGE_PROVIDER} цена/шт=${per_item:.4f} "
          f"итого план=${total:.2f} потолок=${max_cost_usd:.2f}", flush=True)
    if not dry_run:
        plan = apply_budget_cap(plan, max_cost_usd)

    print("\n=== 3. журнал прогресса (докрутка недоделанного) ===", flush=True)
    prior = _load_queue_status(outdir)
    done_tags = {tag for tag, rec in prior.items() if rec.get("status") == "done"}
    if done_tags:
        print(f"уже done в журнале за сегодня: {len(done_tags)} заданий — "
              f"НЕ перегенерируем (не платим дважды)", flush=True)

    todo = []
    for i, task in enumerate(plan, 1):
        tag = _task_tag(i, task["theme"])
        if tag in done_tags:
            continue
        todo.append((i, task))
    print(f"к обработке: {len(todo)} из {len(plan)}", flush=True)

    # Ротация банка стилей (docs/STYLE_BANK.json, docs/PRINT_STYLE_GUIDE.md) — общее
    # скользящее окно последних STYLE_ROTATION_WINDOW style_id на ВЕСЬ дневной прогон
    # (не давать один стиль два раза подряд в батче), потокобезопасно для WORKERS>1.
    recent_styles = art_director.RecentStyles()

    if dry_run:
        print("\n=== 4. dry-run: полный цикл БЕЗ платных вызовов картинок ===",
              flush=True)
        for i, task in todo:
            rec = _process_one(task, i, outdir, dry_run=True)
            _append_queue(outdir, rec)
        print(f"\ndry-run завершён: {len(todo)} заданий прошли контур (план+смета+"
              f"журнал), генерация картинок пропущена -> {outdir}")
        return

    print(f"\n=== 4. генерация ({workers} потоков) ===", flush=True)
    ok, failed = 0, 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(_process_one, task, i, outdir, False, recent_styles): (i, task)
               for i, task in todo}
        for fut in as_completed(futs):
            i, task = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001
                rec = {"tag": _task_tag(i, task["theme"]), "theme": task["theme"],
                      "format": task.get("format", "diecut"),
                      "source": task.get("source", "unknown"), "status": "failed",
                      "attempts": 0, "error": f"необработанное исключение: {e}",
                      "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            _append_queue(outdir, rec)
            if rec["status"] == "done":
                ok += 1
            else:
                failed += 1

    print(f"\nГотово: {ok} done, {failed} failed (из {len(todo)} новых; "
          f"{len(done_tags)} уже были done ранее) -> {outdir}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                     help="полный цикл (парсеры + тематизатор + план + смета + журнал) "
                          "БЕЗ платных вызовов генерации картинок")
    ap.add_argument("--limit", type=int, default=None,
                     help="обрезать план дня до N заданий (для теста)")
    ap.add_argument("--skip-collect", action="store_true",
                     help="не запускать media_watch.py/anime_watch.py заново")
    ap.add_argument("--target", type=int, default=config.PRINTS_PER_DAY,
                     help="сколько заданий на день (дефолт .env PRINTS_PER_DAY)")
    ap.add_argument("--workers", type=int, default=config.WORKERS,
                     help="параллельные генерации (дефолт .env WORKERS)")
    ap.add_argument("--max-cost", type=float, default=config.MAX_DAILY_COST_USD,
                     help="потолок дневного бюджета USD (дефолт .env MAX_DAILY_COST_USD)")
    args = ap.parse_args()

    print(f"провайдер: {config.IMAGE_PROVIDER}", flush=True)
    run_daily(args.target, args.workers, args.max_cost, args.dry_run, args.limit,
              args.skip_collect)


if __name__ == "__main__":
    main()
