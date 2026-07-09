#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mega_batch_run.py — раннер плана на 800 принтов (mega_plan_800.json, см.
build_mega_plan.py) в D:\\800\\<category>\\<sub>\\.

Пайплайн ОДНОГО задания = тот же, что в batch_print.py/daily_prints.py
(art_director.make_ideas -> batch_print.render_design), с одним отличием:
style_pref из плана прокидывается в make_ideas С ПРИОРИТЕТОМ (см. art_director.py,
docstring make_ideas) — часть тем плана (taro_zodiac/часть professions/gym)
форсирует конкретный style_id из docs/STYLE_BANK.json, остальные (style_pref=null)
идут через обычную авторотацию банка (RecentStyles, как в daily_prints.py).

На запись:
  1. art_director.make_ideas(theme, 1, format, recent_styles=..., style_pref=...)
  2. batch_print.render_design(design, filename_base, outdir, ...) — пишет
     outdir/<filename_base>_raw.png (ВСЕГДА, см. batch_print.py) + _diecut.png +
     _ongreen.png + _print.png (адаптивный апскейл — на этой машине ПЕРВЫМ путём
     идёт Replicate при наличии REPLICATE_API_TOKEN, см. upscale.py шестнадцатый
     заход, локальный realesrgan-ncnn-vulkan — второй путь, Lanczos — фолбэк) +
     _design.json.
  3. raw НЕ хранится при успешной вырезке (задача лида, экономия места на D:\\800)
     — mega_batch_run САМ удаляет <filename_base>_raw.png ПОСЛЕ успешного
     render_design (result["ok"]==True). При провале вырезки render_design
     возвращает ok=False, но raw уже сохранён (батч-пайплайн так устроен
     безусловно) — ОСТАВЛЯЕМ его на диске для диагностики брака, как просил лид.

Журнал `<outroot>/_journal.jsonl` (одна строка на ЗАВЕРШЁННОЕ задание, JSONL,
append-only) — резюмируемость: повторный запуск того же плана пропускает
filename_base со статусом "done" в журнале (не платим дважды за уже готовые
принты), "failed"/"skipped_budget_cap" обрабатываются заново.

Потолок стоимости (задача лида, страховка $60, ожидаемая полная смета ~$35-40) —
СОВОКУПНО по ВСЕМ прогонам (включая прошлые, читает журнал), не только текущему:
как только накопленная est-стоимость (по факту attempts * COST_PER_IMAGE_USD)
достигает потолка, ВСЕ ещё НЕ НАЧАТЫЕ задания (Future.cancel() — работает только
для заданий, которые ThreadPoolExecutor ещё не взял в работу) отменяются, уже
выполняющиеся доигрывают до конца (нельзя прервать поток на середине без риска
битых файлов) — небольшой заброс за потолок возможен и ожидаем, это страховка,
не жёсткий hard-limit посреди одного вызова render_design.

Использование:
    python mega_batch_run.py                    # полный план (800, за вычетом done)
    python mega_batch_run.py --limit 6           # смоук-тест (маленький живой прогон)
    python mega_batch_run.py --workers 4 --budget-cap 60
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import art_director            # noqa: E402
import batch_print             # noqa: E402
import config                  # noqa: E402
import upscale                 # noqa: E402

DEFAULT_PLAN_PATH = HERE / "mega_plan_800.json"
DEFAULT_OUTROOT = Path("D:/800")
DEFAULT_BUDGET_CAP_USD = 60.0
PROGRESS_EVERY = 5


def _journal_path(outroot: Path) -> Path:
    return outroot / "_journal.jsonl"


def _summary_path(outroot: Path) -> Path:
    return outroot / "_SUMMARY.json"


def _load_plan(plan_path: Path) -> list:
    return json.loads(plan_path.read_text(encoding="utf-8"))


def _load_journal(outroot: Path) -> dict:
    """Читает <outroot>/_journal.jsonl (если есть от предыдущего прогона) ->
    dict {filename_base: ПОСЛЕДНЯЯ запись про это задание} (append-only журнал,
    записи могут повторяться при retry — побеждает самая свежая строка файла)."""
    path = _journal_path(outroot)
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
                status[rec["filename_base"]] = rec
            except Exception:  # noqa: BLE001 — битая строка журнала не должна ронять запуск
                continue
    return status


_JOURNAL_LOCK = threading.Lock()


def _append_journal(outroot: Path, record: dict) -> None:
    """Append одной строки в журнал — потокобезопасно (ThreadPoolExecutor,
    WORKERS>1 пишут параллельно, см. daily_prints.py._append_queue, тот же
    паттерн + явный Lock, т.к. daily_prints полагается на GIL+короткую операцию,
    здесь добавлен явный лок для ясности при большем объёме записи 800 строк)."""
    with _JOURNAL_LOCK:
        with open(_journal_path(outroot), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _out_dir(outroot: Path, category: str) -> Path:
    """category — слэш-разделённый путь ОТНОСИТЕЛЬНО outroot ("anime/one_piece",
    см. build_mega_plan.py) -> outroot/anime/one_piece, создаётся при первом
    обращении (ThreadPoolExecutor может создавать одну и ту же папку параллельно
    из нескольких заданий одной категории — mkdir(exist_ok=True) идемпотентен)."""
    d = outroot.joinpath(*category.split("/"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _empty_journal_record(rec: dict, status: str, error: str = None) -> dict:
    """Журнальная запись-заглушка для веток, где обработка НЕ дошла до
    batch_print.render_design (отмена бюджетом / необработанное исключение) —
    единый формат со штатной веткой _process_one, чтобы _SUMMARY.json/резюме
    читали журнал одинаково независимо от того, где задание остановилось."""
    return {
        "seq": rec["seq"], "filename_base": rec["filename_base"],
        "category": rec["category"], "theme": rec["theme"],
        "format": rec.get("format"), "style_pref": rec.get("style_pref"),
        "status": status, "attempts": 0, "error": error, "est_cost_usd": 0.0,
        "print_fallback": False, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _process_one(rec: dict, outroot: Path, recent_styles: "art_director.RecentStyles") -> dict:
    """Обрабатывает ОДНО задание плана: арт-директор (style_pref-приоритет) ->
    render_design -> удаление raw при успехе. Отказ ОДНОГО задания (исключение
    любого рода) НЕ должен ронять весь прогон — вызывающий код (run_mega_batch)
    оборачивает fut.result() в try/except на случай необработанного исключения
    здесь тоже, но эта функция сама уже ловит ожидаемые точки отказа
    (арт-директор/render_design) и всегда возвращает журнальную запись, не
    бросает наружу."""
    seq = rec["seq"]
    filename_base = rec["filename_base"]
    theme = rec["theme"]
    fmt = rec.get("format", "diecut")
    style_pref = rec.get("style_pref")
    category = rec["category"]
    outdir = _out_dir(outroot, category)
    tag_p = f"[{seq:04d}/{filename_base}]"

    journal_rec = _empty_journal_record(rec, "failed")

    try:
        recent = recent_styles.snapshot() if recent_styles else None
        design = art_director.make_ideas(theme, 1, fmt, recent_styles=recent,
                                          style_pref=style_pref)[0]
        if recent_styles:
            recent_styles.record(design.get("style_id", ""))
    except Exception as e:  # noqa: BLE001 — арт-директор упал, задание пропускается
        journal_rec["error"] = f"арт-директор: {e}"
        print(f"{tag_p} !! арт-директор упал: {e}", flush=True)
        return journal_rec

    try:
        res = batch_print.render_design(design, filename_base, outdir,
                                        timeout_retries=2, text_style="auto",
                                        no_juice=False, log_prefix=tag_p)
    except Exception as e:  # noqa: BLE001 — render_design не должен ронять весь прогон
        journal_rec["error"] = f"render_design упал: {e}"
        print(f"{tag_p} !! render_design упал: {e}", flush=True)
        return journal_rec

    per_item_cost = config.COST_PER_IMAGE_USD.get(config.IMAGE_PROVIDER, 0.14)
    journal_rec["attempts"] = res["attempts"]
    journal_rec["error"] = res["error"]
    journal_rec["est_cost_usd"] = round(res["attempts"] * per_item_cost, 4)
    journal_rec["print_fallback"] = bool(res.get("print_fallback"))
    journal_rec["style_id"] = design.get("style_id", "")

    if res["ok"]:
        journal_rec["status"] = "done"
        # raw НЕ хранить при успешной вырезке (задача лида) — только диагностика
        # при провале (см. докстринг модуля). batch_print.render_design ВСЕГДА
        # сохраняет <tag>_raw.png безусловно (см. batch_print.py) — удаляем его
        # здесь ПОСЛЕ успеха, не трогаем модуль (общий, много других вызывающих).
        raw_path = res.get("raw")
        if raw_path:
            try:
                Path(raw_path).unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001 — сбой удаления не должен портить запись
                print(f"{tag_p} !! не смог удалить raw ({raw_path}): {e}", flush=True)
    else:
        journal_rec["status"] = "failed"

    return journal_rec


def run_mega_batch(plan_path: Path, outroot: Path, workers: int,
                    budget_cap_usd: float, limit: int = None) -> dict:
    outroot.mkdir(parents=True, exist_ok=True)
    plan = _load_plan(plan_path)
    if limit is not None:
        plan = plan[:limit]
        print(f"--limit {limit}: план обрезан до {len(plan)} заданий", flush=True)

    prior = _load_journal(outroot)
    done_bases = {b for b, r in prior.items() if r.get("status") == "done"}
    if done_bases:
        print(f"уже done в журнале за прошлые прогоны: {len(done_bases)} заданий "
              f"— НЕ перегенерируем (не платим дважды)", flush=True)

    todo = [rec for rec in plan if rec["filename_base"] not in done_bases]
    print(f"к обработке: {len(todo)} из {len(plan)}", flush=True)

    recent_styles = art_director.RecentStyles()

    # Потолок бюджета — СОВОКУПНО по всем прогонам (см. докстринг модуля):
    # затравка баланса из ЛЮБЫХ прошлых записей журнала (done ИЛИ failed — сам
    # неудачный вызов генерации тоже сжёг платные попытки), не только done.
    prior_cost = sum(r.get("est_cost_usd", 0.0) for r in prior.values())
    total_cost = {"usd": prior_cost}
    if prior_cost > 0:
        print(f"накопленная смета с прошлых прогонов: ${prior_cost:.2f}", flush=True)
    cost_lock = threading.Lock()
    stopped_for_budget = {"flag": False}
    completed = {"n": 0, "ok": 0, "failed": 0}
    count_lock = threading.Lock()
    by_category = {}

    t0 = time.time()
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {pool.submit(_process_one, rec, outroot, recent_styles): rec
                   for rec in todo}
            for fut in as_completed(futs):
                rec = futs[fut]
                try:
                    journal_rec = fut.result()
                except CancelledError:
                    journal_rec = _empty_journal_record(
                        rec, "skipped_budget_cap",
                        "отменено потолком бюджета до старта генерации (страховка)")
                except Exception as e:  # noqa: BLE001 — отказ одного принта не валит прогон
                    journal_rec = _empty_journal_record(
                        rec, "failed", f"необработанное исключение: {e}")
                _append_journal(outroot, journal_rec)

                with count_lock:
                    completed["n"] += 1
                    if journal_rec["status"] == "done":
                        completed["ok"] += 1
                    elif journal_rec["status"] != "skipped_budget_cap":
                        completed["failed"] += 1
                    cat = journal_rec["category"]
                    slot = by_category.setdefault(
                        cat, {"done": 0, "failed": 0, "skipped_budget_cap": 0})
                    slot[journal_rec["status"]] = slot.get(journal_rec["status"], 0) + 1
                    n_now = completed["n"]
                if n_now % PROGRESS_EVERY == 0 or n_now == len(todo):
                    elapsed = time.time() - t0
                    print(f"прогресс: {n_now}/{len(todo)} готово "
                          f"(ok={completed['ok']} failed={completed['failed']}), "
                          f"{elapsed / 60:.1f} мин прошло, смета "
                          f"${total_cost['usd']:.2f}", flush=True)

                with cost_lock:
                    total_cost["usd"] += journal_rec.get("est_cost_usd", 0.0)
                    if (total_cost["usd"] >= budget_cap_usd
                            and not stopped_for_budget["flag"]):
                        stopped_for_budget["flag"] = True
                        n_cancelled = sum(1 for f2 in futs if f2.cancel())
                        print(f"!! ПОТОЛОК БЮДЖЕТА ${budget_cap_usd:.2f} достигнут "
                              f"(накоплено ${total_cost['usd']:.2f}) — отменено "
                              f"{n_cancelled} ещё не начатых заданий (страховка, "
                              f"уже идущие догенерируются)", flush=True)

    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "plan_path": str(plan_path),
        "outroot": str(outroot),
        "total_plan": len(plan),
        "processed_this_run": completed["n"],
        "ok_this_run": completed["ok"],
        "failed_this_run": completed["failed"],
        "already_done_before_run": len(done_bases),
        "est_cost_usd_total": round(total_cost["usd"], 4),
        "budget_cap_usd": budget_cap_usd,
        "stopped_early_due_to_budget": stopped_for_budget["flag"],
        "by_category": by_category,
        "elapsed_min_this_run": round((time.time() - t0) / 60, 1),
    }
    _summary_path(outroot).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nГотово: ok={completed['ok']} failed={completed['failed']} "
          f"(из {len(todo)} новых в этом прогоне; {len(done_bases)} уже были done "
          f"ранее) -> {outroot}", flush=True)
    print(f"итог -> {_summary_path(outroot)}", flush=True)
    return summary


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default=str(DEFAULT_PLAN_PATH),
                    help="путь к mega_plan_800.json (дефолт рядом со скриптом)")
    ap.add_argument("--outroot", default=str(DEFAULT_OUTROOT),
                    help="куда сохранять принты (дефолт D:\\800)")
    ap.add_argument("--workers", type=int, default=config.WORKERS,
                    help="параллельные генерации (дефолт .env WORKERS)")
    ap.add_argument("--budget-cap", type=float, default=DEFAULT_BUDGET_CAP_USD,
                    help="потолок совокупной сметы USD, страховка (дефолт 60)")
    ap.add_argument("--limit", type=int, default=None,
                    help="обрезать план до N заданий (смоук-тест)")
    args = ap.parse_args()

    replicate_note = ("Replicate ПЕРВЫМ путём" if upscale.replicate_available()
                      else "Replicate НЕ настроен — локальный realesrgan/Lanczos")
    print(f"провайдер: {config.IMAGE_PROVIDER}, апскейл: {replicate_note}", flush=True)
    run_mega_batch(Path(args.plan), Path(args.outroot), args.workers,
                   args.budget_cap, args.limit)


if __name__ == "__main__":
    main()
