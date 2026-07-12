# -*- coding: utf-8 -*-
"""consume_trends_queue.py — подхват очереди «Радара трендов» в план генерации.

least-invasive мост: платный core (mega_batch_run.py / batch_print.py) НЕ трогаем.
Радар (trend-radar/make_prints.py) только ДОПИСЫВАЕТ строки в trends_queue.jsonl
(одна JSON-запись на дизайн). Этот конвертер:

  1. читает trends_queue.jsonl (строки, ещё не обработанные);
  2. присваивает seq по порядку (продолжая нумерацию существующего trends_plan.json);
  3. срезает служебный ключ `_radar` (метаданные радара, конвейеру не нужны);
  4. валидирует filename_base на уникальность против уже стоящих в плане И, если
     задан --outroot, против журнала _journal.jsonl (что уже сгенерировано) —
     дубли пропускает, не роняя прогон;
  5. дописывает уникальные записи в trends_plan.json (массив), который
     mega_batch_run._load_plan уже читает как есть;
  6. переносит обработанные строки очереди в trends_queue.done.jsonl и очищает
     очередь (маркер «обработано» = перенос в .done, очередь снова пуста для
     следующих нажатий «Сделать принты»).

Запуск НЕ автоматический (квота Gemini): по умолчанию конвертер только готовит
план и печатает команду. С флагом --run он сам вызовет mega_batch_run по
обновлённому trends_plan.json.

    python consume_trends_queue.py                 # только конвертировать очередь → план
    python consume_trends_queue.py --run           # + сразу запустить mega_batch_run
    python consume_trends_queue.py --limit 6        # обрезать план и прогон (смоук)

АЛЬТЕРНАТИВА (если предпочесть не плодить конвертер): добавить в mega_batch_run
флаг `--plan-jsonl trends_queue.jsonl`, который читает JSONL напрямую и присваивает
seq на лету. Здесь НЕ реализовано намеренно — цель least-invasive держать платный
mega_batch_run.py неизменным; конвертер — путь по умолчанию.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE_PATH = HERE / "trends_queue.jsonl"
DONE_PATH = HERE / "trends_queue.done.jsonl"
PLAN_PATH = HERE / "trends_plan.json"
DEFAULT_OUTROOT = Path("D:/800")

# Поля записи плана, которые понимает mega_batch_run (остальное, включая _radar,
# отбрасываем). Обязательны для конвейера: theme, filename_base, category
# (mega_batch_run читает их через rec[...]); style_pref/meme_ref/format —
# опциональные (через rec.get, format по умолчанию 'diecut'). seq присваиваем здесь.
_PLAN_KEYS = ("category", "theme", "style_pref", "meme_ref", "filename_base", "format")
_REQUIRED = ("theme", "filename_base", "category")


def _read_queue_lines(queue_path: Path) -> list[dict]:
    """Читает необработанные записи очереди. Пустые строки пропускает; строку с
    `_consumed: true` (альтернативный маркер) пропускает как уже обработанную;
    битую строку пропускает с предупреждением (не роняет прогон)."""
    out: list[dict] = []
    if not queue_path.exists():
        return out
    with open(queue_path, encoding="utf-8") as f:
        for i, ln in enumerate(f, 1):
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception as e:  # noqa: BLE001 — битая строка не должна ронять конвертацию
                print(f"  !! строка {i} очереди битая ({e}) — пропущена", flush=True)
                continue
            if isinstance(rec, dict) and rec.get("_consumed") is True:
                continue
            out.append(rec)
    return out


def _load_plan(plan_path: Path) -> list:
    """Существующий trends_plan.json (массив). Нет файла/пусто -> []. То же чтение,
    что mega_batch_run._load_plan, плюс мягкая деградация на отсутствии файла."""
    if not plan_path.exists():
        return []
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"trends_plan.json нечитаем ({e}) — остановлено, чтобы не затереть план")
    return data if isinstance(data, list) else []


def _journal_filename_bases(outroot: Path) -> set[str]:
    """filename_base из журнала <outroot>/_journal.jsonl (что уже генерировалось в
    прошлых прогонах). Нет файла -> пусто. Для дедупликации против журнала."""
    path = outroot / "_journal.jsonl"
    seen: set[str] = set()
    if not path.exists():
        return seen
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            fb = rec.get("filename_base")
            if fb:
                seen.add(str(fb))
    return seen


def _clean_record(rec: dict, seq: int) -> dict:
    """Чистая запись плана: только понятные конвейеру поля + присвоенный seq.
    _radar и прочий мусор срезаны. Опциональные поля с пустым/None значением не
    включаем (mega_batch_run.get даст дефолт: format -> 'diecut', meme_ref -> нет)."""
    out: dict = {"seq": seq}
    for k in _PLAN_KEYS:
        if k not in rec:
            continue
        v = rec[k]
        if k in ("style_pref", "meme_ref", "format") and (v is None or v == ""):
            continue  # опциональное пустое — не засоряем план
        out[k] = v
    return out


def _next_seq(plan: list) -> int:
    """Следующий seq — max существующего + 1 (план мог быть заполнен вручную/
    прошлым прогоном). Пустой план -> 1."""
    mx = 0
    for rec in plan:
        if isinstance(rec, dict):
            try:
                mx = max(mx, int(rec.get("seq", 0)))
            except (TypeError, ValueError):
                continue
    return mx + 1


def consume(queue_path: Path = QUEUE_PATH, plan_path: Path = PLAN_PATH,
            done_path: Path = DONE_PATH, outroot: Path = DEFAULT_OUTROOT) -> dict:
    """Ядро конвертации (без запуска генерации). Возвращает сводку
    {appended, skipped, plan_size, appended_bases, skipped_bases, plan_path}."""
    queued = _read_queue_lines(queue_path)
    if not queued:
        return {"appended": 0, "skipped": 0, "plan_size": len(_load_plan(plan_path)),
                "appended_bases": [], "skipped_bases": [], "plan_path": str(plan_path),
                "note": "очередь пуста — нечего конвертировать"}

    plan = _load_plan(plan_path)
    seen: set[str] = {str(r.get("filename_base")) for r in plan
                      if isinstance(r, dict) and r.get("filename_base")}
    seen |= _journal_filename_bases(outroot)

    seq = _next_seq(plan)
    appended_bases: list[str] = []
    skipped_bases: list[str] = []
    for rec in queued:
        if not isinstance(rec, dict):
            continue
        fb = str(rec.get("filename_base", "")).strip()
        missing = [k for k in _REQUIRED if not str(rec.get(k, "")).strip()]
        if missing:
            skipped_bases.append(fb or "<без filename_base>")
            print(f"  !! пропуск {fb or '?'}: нет обязательных полей {missing}", flush=True)
            continue
        if fb in seen:
            skipped_bases.append(fb)
            print(f"  · пропуск {fb}: уже есть в плане/журнале (дубль)", flush=True)
            continue
        plan.append(_clean_record(rec, seq))
        seen.add(fb)
        appended_bases.append(fb)
        seq += 1

    if appended_bases:
        # атомарно: пишем во временный файл рядом и заменяем (не рвём план при сбое)
        tmp = plan_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(plan_path)

    # Обработанные строки очереди -> в .done (аудит), очередь очищаем. Маркер
    # «обработано» = строка перенесена в done и больше не стоит в очереди.
    if queued:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(done_path, "a", encoding="utf-8") as f:
            for rec in queued:
                rec = dict(rec) if isinstance(rec, dict) else {"_raw": rec}
                rec["_consumed_at"] = stamp
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        queue_path.write_text("", encoding="utf-8")

    return {"appended": len(appended_bases), "skipped": len(skipped_bases),
            "plan_size": len(plan), "appended_bases": appended_bases,
            "skipped_bases": skipped_bases, "plan_path": str(plan_path)}


def _run_mega_batch(plan_path: Path, outroot: Path, limit: int | None) -> int:
    """Запуск платного пайплайна отдельным процессом (тем же интерпретатором).
    Возвращает код возврата mega_batch_run.py."""
    cmd = [sys.executable, str(HERE / "mega_batch_run.py"),
           "--plan", str(plan_path), "--outroot", str(outroot)]
    if limit is not None:
        cmd += ["--limit", str(limit)]
    print("запуск:", " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def main() -> None:
    ap = argparse.ArgumentParser(description="Подхват trends_queue.jsonl → trends_plan.json")
    ap.add_argument("--queue", default=str(QUEUE_PATH), help="путь к trends_queue.jsonl")
    ap.add_argument("--plan", default=str(PLAN_PATH), help="путь к trends_plan.json")
    ap.add_argument("--done", default=str(DONE_PATH), help="куда переносить обработанные строки")
    ap.add_argument("--outroot", default=str(DEFAULT_OUTROOT),
                    help="корень генерации (для дедупликации против журнала и --run)")
    ap.add_argument("--run", action="store_true",
                    help="сразу запустить mega_batch_run по обновлённому плану "
                         "(по умолчанию только конвертировать; генерацию запускает владелец)")
    ap.add_argument("--limit", type=int, default=None,
                    help="обрезать прогон mega_batch_run до N заданий (только с --run)")
    args = ap.parse_args()

    outroot = Path(args.outroot)
    summary = consume(Path(args.queue), Path(args.plan), Path(args.done), outroot)
    print(f"конвертация: +{summary['appended']} в план, пропущено {summary['skipped']}, "
          f"план теперь {summary['plan_size']} заданий.", flush=True)
    if summary["appended_bases"]:
        print("  добавлены:", ", ".join(summary["appended_bases"]), flush=True)
    if summary["skipped_bases"]:
        print("  пропущены (дубли/неполные):", ", ".join(summary["skipped_bases"]), flush=True)

    if not summary["appended"]:
        print("нечего запускать (в план ничего не добавлено).", flush=True)
        return

    if args.run:
        rc = _run_mega_batch(Path(args.plan), outroot, args.limit)
        sys.exit(rc)
    else:
        print("готово. Запуск генерации (владелец, когда есть квота Gemini):", flush=True)
        print(f"  python mega_batch_run.py --plan {args.plan} --outroot {args.outroot}",
              flush=True)


if __name__ == "__main__":
    main()
