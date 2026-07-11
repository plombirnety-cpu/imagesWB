#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_pipeline.py — авто-цепочка ночного/утреннего прогона (заказ владельца 10.07).

Ждёт сброса дневной квоты Gemini (RPD), затем прогоняет ПО ПОРЯДКУ:
  1) ТРЕНДЫ (trends_plan.json -> D:\\trends) — оригинальное исполнение мемов;
  2) ПАК (mega_plan_800.json -> D:\\800, докатка) — уже переупорядочен:
     Магическая битва -> не-аниме нейтральные -> остальное аниме.

Квоту НЕ угадываем по времени: периодически пробуем 1 тренд-принт; как только он
проходит (не 429-quota) — квота вернулась, гоним всё. Докатка по журналам не платит
за готовое повторно. Скрипт переживает конец сессии (запускать detached).

Запуск (detached, PowerShell):
  Start-Process python -ArgumentList '-u','run_pipeline.py' -WindowStyle Hidden `
    -RedirectStandardOutput 'D:\\_pipeline.log' -RedirectStandardError 'D:\\_pipeline.err'
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(errors="replace")
sys.stderr.reconfigure(errors="replace")

PF = Path(__file__).resolve().parent
PY = sys.executable
TRENDS_PLAN = PF / "trends_plan.json"
TRENDS_OUT = Path(r"D:\trends")
TRENDS_JOURNAL = TRENDS_OUT / "_journal.jsonl"
PACK_OUT = Path(r"D:\800")

WAIT_MINUTES = 30          # пауза между пробами квоты
MAX_WAIT_HOURS = 24        # предохранитель: не ждать дольше суток
QUOTA_MARK = "exceeded your current quota"


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def run(args: list[str]) -> int:
    log("RUN: " + " ".join(args))
    p = subprocess.run([PY, "-u", *args], cwd=str(PF))
    log(f"  -> exit {p.returncode}")
    return p.returncode


def last_journal_status(journal: Path) -> tuple[str, str]:
    """(status, error) последней записи журнала, ('', '') если пусто/нет."""
    if not journal.exists():
        return "", ""
    last = ""
    for line in journal.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            last = line
    if not last:
        return "", ""
    try:
        rec = json.loads(last)
        return rec.get("status", ""), rec.get("error", "") or ""
    except Exception:
        return "", ""


def quota_available() -> bool:
    """Проба: 1 тренд-принт через мега-раннер. True ТОЛЬКО если принт реально
    сгенерился (status=done) — тогда квота точно вернулась и можно гнать всё.
    Любая ошибка (429-quota, 500-temp, прочее) -> False, повторить пробу позже
    (не запускаем полный прогон на временном сбое)."""
    run(["mega_batch_run.py", "--plan", str(TRENDS_PLAN), "--outroot", str(TRENDS_OUT),
         "--workers", "1", "--limit", "1"])
    status, error = last_journal_status(TRENDS_JOURNAL)
    if status == "done":
        log("  проба: done — квота вернулась, гоним цепочку")
        return True
    if QUOTA_MARK in error:
        log("  проба: quota-429 — ждём сброса")
    else:
        log(f"  проба: не done ({(error or status)[:60]}) — повтор позже")
    return False


def main() -> None:
    log("=== АВТО-ЦЕПОЧКА: ожидание квоты Gemini -> тренды -> пак ===")
    log(f"порядок пака (переупорядочен): Магическая битва -> не-аниме -> остальное аниме")

    # ── ФАЗА 0: дождаться сброса квоты ──
    deadline = time.time() + MAX_WAIT_HOURS * 3600
    while True:
        if quota_available():
            break
        if time.time() > deadline:
            log(f"!! квота не вернулась за {MAX_WAIT_HOURS}ч — стоп, нужен ручной разбор")
            return
        log(f"  сон {WAIT_MINUTES} мин до следующей пробы...")
        time.sleep(WAIT_MINUTES * 60)

    # ── ФАЗА 1: тренды (докатка добьёт остаток плана) ──
    log("=== ФАЗА 1: ТРЕНДЫ (оригинальное исполнение) ===")
    run(["mega_batch_run.py", "--plan", str(TRENDS_PLAN), "--outroot", str(TRENDS_OUT),
         "--workers", "2"])
    log("тренды завершены (см. D:\\trends\\_SUMMARY.json)")

    # ── ФАЗА 2: пак 800 (докатка, переупорядоченный план) ──
    log("=== ФАЗА 2: ПАК 800 (докатка: магичка -> не-аниме -> остальное) ===")
    run(["mega_batch_run.py", "--workers", "4"])
    log("пак завершён (см. D:\\800\\_SUMMARY.json)")

    log("=== АВТО-ЦЕПОЧКА ЗАВЕРШЕНА ===")


if __name__ == "__main__":
    main()
