#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""theme_scout.py — сбор трендов из соседнего trend-watch/ и разворачивание их в
дневной план заданий на принты (для daily_prints.py).

Шаги:
1. Запускает media_watch.py и anime_watch.py в trend-watch/ (subprocess, таймаут,
   try/except — упавший парсер не валит цикл, работаем с тем, что уже лежит в CSV).
2. Читает trend-watch/data/media_latest.csv и anime_latest.csv, берёт топ-N по score.
3. Claude-«тематизатор»: леммы + example_text -> задания на принты до дневного объёма.
   Аниме-тренд (тайтл/персонаж) -> ростер главных персонажей x форматы x 1-2 вариации.
   Общая тема -> несколько принтопригодных концептов. Политика/война/катастрофы/
   трагедии/смерти реальных людей/криминал — ОТСЕКАЮТСЯ МОЛЧА системным промптом.
4. Если тем не хватает на дневной объём — добор из evergreen_themes.txt.

Выход: themes_daily_YYYY-MM-DD.txt (по одной теме на строку, для batch_print-совместимого
формата) + themes_daily_YYYY-MM-DD.json (полный план: тема, формат, источник, score).
"""
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import anthropic

import config

HERE = Path(__file__).resolve().parent

TREND_WATCH_DIR = Path(config.TREND_WATCH_DIR)
if not TREND_WATCH_DIR.is_absolute():
    TREND_WATCH_DIR = (HERE / TREND_WATCH_DIR).resolve()

EVERGREEN_FILE = HERE / "evergreen_themes.txt"

# Сколько строк топ-N брать из каждой CSV-ленты трендов перед передачей тематизатору.
_TOP_N_MEDIA = 8
_TOP_N_ANIME = 6

_PARSER_TIMEOUT = 300  # секунд на каждый парсер trend-watch


def _run_parser(script: str) -> bool:
    """Запускает парсер trend-watch subprocess-ом. Возвращает True при успехе.
    НЕ валит вызывающий цикл — падение/таймаут парсера логируется и цикл продолжается
    с тем, что уже лежит в data/*.csv (может быть от предыдущего запуска)."""
    script_path = TREND_WATCH_DIR / script
    if not script_path.exists():
        print(f"  !! {script} не найден в {TREND_WATCH_DIR} — пропуск", flush=True)
        return False
    try:
        print(f"  запускаю {script}...", flush=True)
        r = subprocess.run([sys.executable, script], cwd=str(TREND_WATCH_DIR),
                           timeout=_PARSER_TIMEOUT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(f"  !! {script} завершился с кодом {r.returncode}: "
                  f"{r.stderr[-500:] if r.stderr else '(нет stderr)'}", flush=True)
            return False
        print(f"  {script} ok", flush=True)
        return True
    except subprocess.TimeoutExpired:
        print(f"  !! {script} превысил таймаут {_PARSER_TIMEOUT}с — пропуск", flush=True)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  !! {script} упал: {e}", flush=True)
        return False


def collect_trends() -> None:
    """Запускает оба парсера trend-watch. Не бросает исключения наружу."""
    print("сбор трендов из trend-watch...", flush=True)
    _run_parser("media_watch.py")
    _run_parser("anime_watch.py")


def _read_csv_top(path: Path, top_n: int) -> list:
    """Читает CSV трендов (score,lemma,mentions,delta,is_new,sources_count,sources,
    example_text,example_url), сортирует по score убыв., берёт top_n строк."""
    if not path.exists():
        print(f"  !! {path.name} не найден — эта лента пуста", flush=True)
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                row["score"] = float(row.get("score", 0) or 0)
            except ValueError:
                row["score"] = 0.0
            rows.append(row)
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:top_n]


def read_trend_rows() -> tuple:
    """Возвращает (media_rows, anime_rows) — топ-N по score из обеих лент."""
    media = _read_csv_top(TREND_WATCH_DIR / "data" / "media_latest.csv", _TOP_N_MEDIA)
    anime = _read_csv_top(TREND_WATCH_DIR / "data" / "anime_latest.csv", _TOP_N_ANIME)
    return media, anime


# ── Claude-«тематизатор и расширитель» ──────────────────────────────────────────

SYSTEM_SCOUT = (
    "Ты продюсер принтов для футболок. Тебе дают список трендовых лемм (ключевых слов) "
    "из СМИ/соцсетей и аниме-комьюнити с примером текста-упоминания — твоя задача "
    "развернуть их в конкретные задания на дизайн принта. "
    "ЛОГИКА РАЗВОРАЧИВАНИЯ: "
    "(1) Если лемма — название аниме-тайтла или персонажа (сериал, манга, игра) — "
    "разверни в РОСТЕР главных персонажей этого тайтла (сколько реально известно, "
    "не выдумывай персонажей), для каждого персонажа укажи 1-2 вариации образа "
    "(разная поза/арка/форма — если применимо) и формат (cutout или diecut). ВАЖНО: "
    "даже если у франшизы сотни персонажей (напр. One Piece) — бери ТОЛЬКО 15-20 "
    "самых главных/узнаваемых с их вариациями на ОДИН тайтл, НЕ пытайся перечислить "
    "весь ростер целиком, это раздувает ответ до неотправляемого размера. "
    "(2) Если лемма — общая тема/предмет/событие (НЕ персонаж) — разверни в НЕСКОЛЬКО "
    "принтопригодных КОНКРЕТНЫХ визуальных концептов по этой теме (не абстрактных). "
    "СТРОГИЙ ФИЛЬТР ПРИНТОПРИГОДНОСТИ (ОБЯЗАТЕЛЬНО, без исключений) — ОТСЕКАЙ МОЛЧА "
    "(просто не включай в результат, без пояснений) любую лемму или пример текста про: "
    "политику и политиков, войну/боевые действия/армию, катастрофы и аварии, трагедии, "
    "смерти и травмы реальных людей, криминал и преступления, теракты, стихийные "
    "бедствия. Такие темы НИКОГДА не превращай в принт, даже завуалированно. Если ПОСЛЕ "
    "фильтра лемма осталась нейтральной (аниме, кино, игры, техника, машины, животные, "
    "бытовые явления, мемы, спорт, еда, природа) — работай с ней. "
    "Для каждого задания верни JSON-объект: "
    "{\"theme\":\"<готовая тема-описание для арт-директора принтов, на русском, "
    "конкретная — имя персонажа+тайтл, или конкретный предмет/сцена>\","
    "\"format\":\"<cutout ИЛИ diecut>\"}. "
    "Отвечай СТРОГО JSON-массивом объектов, БЕЗ markdown и пояснений. Если ПОСЛЕ фильтра "
    "не осталось ни одной годной лемм — верни пустой массив []."
)


# Claude пытается выдать ВЕСЬ дневной объём (может быть 500) ОДНИМ JSON-ответом, если
# попросить "нужно ~500 заданий" напрямую — ответ обрезается по max_tokens на середине
# JSON-массива (проверено: 9193 символа, незакрытый массив, json.loads падает). Явный
# потолок ЗА ОДИН вызов — тематизатор просят развернуть тренды в РАЗУМНОЕ число заданий
# (не пытаться закрыть весь дневной план одним тайтлом), а добор до полного объёма идёт
# через evergreen-пул (или несколько вызовов, если понадобится — см. build_daily_plan).
_MAX_TASKS_PER_CALL = 60


def _ask_claude_scout(media_rows: list, anime_rows: list, target_n: int) -> str:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    call_n = min(target_n, _MAX_TASKS_PER_CALL)
    lines = ["ОБЩИЕ ТРЕНДЫ (СМИ/соцсети):"]
    for r in media_rows:
        lines.append(f"- {r['lemma']} (score={r['score']}): {r.get('example_text', '')[:200]}")
    lines.append("\nАНИМЕ-ТРЕНДЫ:")
    for r in anime_rows:
        lines.append(f"- {r['lemma']} (score={r['score']}): {r.get('example_text', '')[:200]}")
    lines.append(f"\nРазверни эти тренды примерно в {call_n} заданий на принты суммарно "
                 f"(ОРИЕНТИР, не жёсткий потолок — можно чуть меньше или больше). НЕ "
                 f"пытайся закрыть весь дневной объём одним тайтлом/темой — бери "
                 f"РАЗУМНОЕ число заданий с каждого тренда (ростер персонажей — не "
                 f"более 15-20 на один тайтл, даже если у франшизы сотни персонажей), "
                 f"остальной объём дня доберётся из отдельного вечнозелёного пула.")
    user = "\n".join(lines)
    resp = client.messages.create(
        model=config.MODEL,
        max_tokens=8000,
        system=SYSTEM_SCOUT,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _parse_scout(text: str) -> list:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for x in data:
        if isinstance(x, dict) and str(x.get("theme", "")).strip():
            fmt = str(x.get("format") or "").strip().lower()
            out.append({"theme": str(x["theme"]).strip(),
                        "format": fmt if fmt in ("cutout", "diecut") else "diecut"})
    return out


def expand_trends_to_tasks(media_rows: list, anime_rows: list, target_n: int) -> list:
    """Тематизатор Claude: тренды -> список заданий {theme, format, source}.

    НЕ откатывается тихо при сбое парсинга JSON (как art_director): 1 ретрай, затем
    явная ошибка — вызывающий код (build_daily_plan) переходит на чистый evergreen-добор.
    """
    if not media_rows and not anime_rows:
        return []
    text = _ask_claude_scout(media_rows, anime_rows, target_n)
    tasks = _parse_scout(text)
    if not tasks:
        text = _ask_claude_scout(media_rows, anime_rows, target_n)  # 1 ретрай
        tasks = _parse_scout(text)
    if not tasks:
        print("  !! тематизатор не смог собрать валидный JSON дважды подряд — "
              "переходим на чистый evergreen-добор", flush=True)
        return []
    for t in tasks:
        t["source"] = "trend"
    return tasks


# ── Evergreen-добор ──────────────────────────────────────────────────────────────

def _read_evergreen() -> list:
    if not EVERGREEN_FILE.exists():
        return []
    lines = []
    with open(EVERGREEN_FILE, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                lines.append(ln)
    return lines


def build_daily_plan(target_n: int) -> list:
    """Полный план дня: тренды (расширенные тематизатором) + evergreen-добор до
    target_n. Возвращает список {theme, format, source}."""
    media_rows, anime_rows = read_trend_rows()
    tasks = expand_trends_to_tasks(media_rows, anime_rows, target_n)
    print(f"  из трендов получено {len(tasks)} заданий", flush=True)

    if len(tasks) < target_n:
        evergreen = _read_evergreen()
        need = target_n - len(tasks)
        # Не повторяем темы, которые уже взяты из трендов (по точному совпадению строки).
        used_themes = {t["theme"].strip().lower() for t in tasks}
        added = 0
        idx = 0
        while added < need and idx < len(evergreen) * 3:  # запас на случай мало строк
            theme = evergreen[idx % len(evergreen)] if evergreen else None
            idx += 1
            if not theme:
                break
            key = theme.strip().lower()
            # Разрешаем повтор evergreen-темы несколько раз только если строк реально
            # не хватает на нужный объём (используем round-robin по списку).
            if key in used_themes and idx <= len(evergreen):
                continue
            used_themes.add(key)
            tasks.append({"theme": theme, "format": "diecut", "source": "evergreen"})
            added += 1
        print(f"  добрано {added} заданий из evergreen-пула", flush=True)

    return tasks[:target_n]


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=config.PRINTS_PER_DAY,
                     help="сколько заданий собрать на день (дефолт из .env PRINTS_PER_DAY)")
    ap.add_argument("--skip-collect", action="store_true",
                     help="не запускать media_watch.py/anime_watch.py заново — работать "
                          "с уже лежащими CSV в trend-watch/data/")
    args = ap.parse_args()

    if not args.skip_collect:
        collect_trends()
    else:
        print("пропускаю запуск парсеров (--skip-collect) — читаю существующие CSV",
              flush=True)

    print(f"\nсобираю дневной план (цель: {args.target} заданий)...", flush=True)
    plan = build_daily_plan(args.target)

    date_str = time.strftime("%Y-%m-%d")
    txt_path = HERE / f"themes_daily_{date_str}.txt"
    json_path = HERE / f"themes_daily_{date_str}.json"

    with open(txt_path, "w", encoding="utf-8") as f:
        for t in plan:
            f.write(t["theme"] + "\n")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    n_trend = sum(1 for t in plan if t["source"] == "trend")
    n_ever = sum(1 for t in plan if t["source"] == "evergreen")
    print(f"\nГотово: {len(plan)} заданий ({n_trend} из трендов, {n_ever} из evergreen)")
    print(f"-> {txt_path}")
    print(f"-> {json_path}")


if __name__ == "__main__":
    main()
