#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""franchise_scout.py — «вскрытие франшизы»: по названию тайтла (аниме/сериал/
дорама/фильм) собирает ИЗМЕРЕННУЮ популярность персонажей и моментов и выдаёт
ранжированное досье для прицельных принтов.

Зачем: Claude по своим знаниям не в курсе, кто из героев СВЕЖЕГО сериала реально
выстрелил у фанатов — классический пример: в Re:Zero формально главная героиня
Эмилия, но у Рем сильно больше favourites на AniList и фанаты покупают именно её.
Этот модуль подсовывает тематизатору/арт-директору ЧИСЛА вместо догадок.

Источники сигналов (каждый — отдельная функция, graceful degradation — падение
ОДНОГО источника печатает предупреждение и возвращает [] / {}, не роняя досье):
    _anilist_characters(title)  — AniList GraphQL: favourites на персонажа + роль.
    _jikan_characters(title)    — Jikan (MyAnimeList): favorites на персонажа.
    _tmdb_credits(title)        — TMDB: порядок каста + popularity актёров (кино/сериалы).
    _youtube_edits(title)       — YouTube: топ-эдиты по теме + просмотры (СТРОГО 1
                                   search-вызов на франшизу — квота 100 юнитов).
    _gtrends_related(title)     — Google Trends (trendspy): rising related queries.

Синтез: build_dossier(title, kind="auto") сводит все сигналы в ОДИН вызов Claude,
который взвешивает ЧИСЛА из входа (не свои знания) и возвращает строго JSON-досье
(персонажи по убыванию score + моменты). Парсинг — тот же приём, что в
theme_scout._parse_scout (ремонт обрезанного JSON + 1 ретрай + дамп сбоя в файл).

Кэш: data/franchise_cache/<slug>_<YYYY-MM-DD>.json — повторный запрос того же
тайтла в тот же день читает кэш и не дёргает сеть (экономит квоту YouTube).

Кэш СЫРЫХ СИГНАЛОВ: data/franchise_cache/signals_<slug>_<YYYY-MM-DD>.json — пишется
СРАЗУ после сбора сигналов от всех 5 источников, ДО вызова синтезатора Claude.
Отдельно от кэша готового досье (который по дизайну не пишется при провале
синтеза — не кэшируем неудачу). Если синтез Claude временно недоступен (rate-limit,
5xx, исчерпанный баланс на момент запуска) и затем восстановлен, повторный вызов
build_dossier для того же тайтла в тот же день переиспользует уже собранные сигналы
из этого кэша вместо повторного сбора (главная экономия — 100 юнитов YouTube
search.list за франшизу) и сразу пробует синтез заново.

CLI (ручной инструмент владельца):
    python franchise_scout.py "Re:Zero" [--kind anime|tv|movie]
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

import config
import llm_provider

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "data" / "franchise_cache"

_REQUEST_TIMEOUT = 15

# ── AniList ──────────────────────────────────────────────────────────────────

_ANILIST_URL = "https://graphql.anilist.co"
_ANILIST_QUERY = """
query ($search: String, $perPage: Int) {
  Media(search: $search, type: ANIME) {
    title { romaji english }
    characters(sort: FAVOURITES_DESC, perPage: $perPage) {
      edges {
        role
        node { name { full } favourites }
      }
    }
  }
}
"""


def _anilist_characters(title: str) -> list:
    """AniList: поиск Media(search=title, type=ANIME) -> персонажи, отсортированные
    по favourites (сколько пользователей добавили персонажа в избранное), с ролью
    MAIN/SUPPORTING. Уважает лимит ~30 req/min (429 + Retry-After).

    Возвращает список {"name": str, "favourites": int, "role": str}. Пусто при
    любой сетевой ошибке/отсутствии тайтла — не роняет build_dossier."""
    try:
        resp = requests.post(
            _ANILIST_URL,
            json={"query": _ANILIST_QUERY, "variables": {"search": title, "perPage": 15}},
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5") or "5")
            print(f"  [anilist] 429 Too Many Requests — жду {retry_after}с и повторяю один раз.",
                  flush=True)
            time.sleep(retry_after)
            resp = requests.post(
                _ANILIST_URL,
                json={"query": _ANILIST_QUERY, "variables": {"search": title, "perPage": 15}},
                timeout=_REQUEST_TIMEOUT,
            )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 — источник не должен ронять досье
        print(f"  !! anilist_characters({title!r}) не удалось загрузить: {e}", flush=True)
        return []

    if payload.get("errors"):
        print(f"  !! anilist_characters({title!r}) GraphQL вернул ошибку: "
              f"{payload['errors']} — пропуск", flush=True)
        return []

    media = (payload.get("data") or {}).get("Media")
    if not media:
        print(f"  !! anilist_characters({title!r}) тайтл не найден на AniList — пропуск",
              flush=True)
        return []

    out = []
    edges = ((media.get("characters") or {}).get("edges")) or []
    for edge in edges:
        node = edge.get("node") or {}
        name = ((node.get("name") or {}).get("full") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "favourites": int(node.get("favourites") or 0),
            "role": str(edge.get("role") or "").strip(),
        })
    return out


# ── Jikan (MyAnimeList) ──────────────────────────────────────────────────────

_JIKAN_BASE = "https://api.jikan.moe/v4"
_JIKAN_BETWEEN_CALLS_SLEEP = 1.0  # запас над официальным лимитом 3 req/sec


def _jikan_characters(title: str) -> list:
    """Jikan: GET /anime?q=title&limit=1 -> mal_id -> GET /anime/{id}/characters.
    Пауза 1с между двумя вызовами (тот же запас, что у trend-watch/providers/jikan.py).

    Возвращает список {"name": str, "favorites": int, "role": str}. Пусто при
    любой ошибке/отсутствии тайтла."""
    try:
        resp = requests.get(f"{_JIKAN_BASE}/anime", params={"q": title, "limit": 1},
                            timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 429:
            print(f"  !! jikan_characters({title!r}) 429 Too Many Requests — пропуск",
                  flush=True)
            return []
        resp.raise_for_status()
        search_data = (resp.json().get("data") or [])
    except Exception as e:  # noqa: BLE001
        print(f"  !! jikan_characters({title!r}) поиск тайтла не удался: {e}", flush=True)
        return []

    if not search_data:
        print(f"  !! jikan_characters({title!r}) тайтл не найден на Jikan — пропуск",
              flush=True)
        return []

    mal_id = search_data[0].get("mal_id")
    if not mal_id:
        return []

    time.sleep(_JIKAN_BETWEEN_CALLS_SLEEP)

    try:
        resp = requests.get(f"{_JIKAN_BASE}/anime/{mal_id}/characters",
                            timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 429:
            print(f"  !! jikan_characters({title!r}) 429 на /characters — пропуск",
                  flush=True)
            return []
        resp.raise_for_status()
        chars_data = (resp.json().get("data") or [])
    except Exception as e:  # noqa: BLE001
        print(f"  !! jikan_characters({title!r}) /characters не удался: {e}", flush=True)
        return []

    out = []
    for item in chars_data:
        node = item.get("character") or {}
        name = (node.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "favorites": int(item.get("favorites") or 0),
            "role": str(item.get("role") or "").strip(),
        })
    return out


# ── TMDB (кино/сериалы/дорамы) ───────────────────────────────────────────────

_TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/multi"


def _tmdb_credits(title: str) -> list:
    """TMDB: поиск search/multi (ru-RU) -> первый результат (tv или movie) ->
    /tv/{id}/credits или /movie/{id}/credits: порядок каста (order) + popularity
    актёров. Для неаниме-тайтлов (сериал/дорама/фильм).

    Возвращает список {"name": str, "character": str, "order": int,
    "popularity": float}. Пусто при отсутствии ключа/тайтла/ошибке сети."""
    api_key = (config.TMDB_API_KEY or "").strip()
    if not api_key:
        print("  !! tmdb_credits: нет TMDB_API_KEY в .env — пропуск", flush=True)
        return []

    try:
        resp = requests.get(_TMDB_SEARCH_URL,
                            params={"api_key": api_key, "language": "ru-RU", "query": title},
                            timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        results = (resp.json().get("results") or [])
    except Exception as e:  # noqa: BLE001
        print(f"  !! tmdb_credits({title!r}) поиск не удался: {e}", flush=True)
        return []

    # Первый результат нужного типа (tv или movie) — search/multi возвращает и person.
    first = next((r for r in results if r.get("media_type") in ("tv", "movie")), None)
    if not first:
        print(f"  !! tmdb_credits({title!r}) тайтл не найден на TMDB — пропуск", flush=True)
        return []

    media_type = first["media_type"]
    media_id = first.get("id")
    if not media_id:
        return []

    credits_url = f"https://api.themoviedb.org/3/{media_type}/{media_id}/credits"
    try:
        resp = requests.get(credits_url, params={"api_key": api_key, "language": "ru-RU"},
                            timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        cast = (resp.json().get("cast") or [])
    except Exception as e:  # noqa: BLE001
        print(f"  !! tmdb_credits({title!r}) /credits не удался: {e}", flush=True)
        return []

    out = []
    for member in cast[:20]:
        actor = (member.get("name") or "").strip()
        if not actor:
            continue
        out.append({
            "name": actor,
            "character": (member.get("character") or "").strip(),
            "order": int(member.get("order") if member.get("order") is not None else 999),
            "popularity": float(member.get("popularity") or 0.0),
        })
    return out


# ── YouTube (эдиты — сигнал моментов) ────────────────────────────────────────

_YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

# ВНИМАНИЕ: search.list стоит 100 юнитов квоты (из бесплатных 10000/сутки) — СТРОГО
# не более ОДНОГО search-вызова на франшизу за весь build_dossier. videos.list (доп.
# запрос по id за viewCount) стоит всего 1 юнит — итого ОДИН build_dossier с реальным
# YouTube-сигналом жрёт ~101 юнит квоты (100 search + 1 videos).
_YOUTUBE_SEARCH_COST_UNITS = 100


def _youtube_edits(title: str) -> list:
    """YouTube: search.list(q="<title> edit", type=video, maxResults=12) — ОДИН
    вызов на франшизу (100 юнитов), затем videos.list по id (1 юнит) за viewCount.

    Возвращает список {"title": str, "views": int, "url": str}. Пусто без ключа/
    при ошибке/пустой выдаче."""
    api_key = (config.YOUTUBE_API_KEY or "").strip()
    if not api_key:
        print("  !! youtube_edits: нет YOUTUBE_API_KEY в .env — пропуск", flush=True)
        return []

    try:
        resp = requests.get(_YOUTUBE_SEARCH_URL, params={
            "part": "snippet", "q": f"{title} edit", "type": "video",
            "maxResults": 12, "key": api_key,
        }, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        items = (resp.json().get("items") or [])
    except Exception as e:  # noqa: BLE001
        print(f"  !! youtube_edits({title!r}) search.list не удался: {e}", flush=True)
        return []

    video_ids = [it["id"]["videoId"] for it in items
                 if isinstance(it.get("id"), dict) and it["id"].get("videoId")]
    if not video_ids:
        print(f"  !! youtube_edits({title!r}) поиск не дал видео — пропуск", flush=True)
        return []

    snippets = {it["id"]["videoId"]: (it.get("snippet") or {}).get("title", "")
                for it in items if isinstance(it.get("id"), dict) and it["id"].get("videoId")}

    try:
        resp = requests.get(_YOUTUBE_VIDEOS_URL, params={
            "part": "statistics", "id": ",".join(video_ids), "key": api_key,
        }, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        video_items = (resp.json().get("items") or [])
    except Exception as e:  # noqa: BLE001
        print(f"  !! youtube_edits({title!r}) videos.list не удался: {e}", flush=True)
        return []

    out = []
    for item in video_items:
        vid = item.get("id", "")
        stats = item.get("statistics") or {}
        out.append({
            "title": snippets.get(vid, ""),
            "views": int(stats.get("viewCount") or 0),
            "url": f"https://www.youtube.com/watch?v={vid}" if vid else "",
        })
    out.sort(key=lambda r: r["views"], reverse=True)
    return out


# ── Google Trends related (rising) ───────────────────────────────────────────

def _gtrends_related(title: str) -> list:
    """trendspy related_queries (rising) — какие имена/слова дописывают к тайтлу
    в поиске (сигнал: если фанаты гуглят "<тайтл> <имя персонажа>", персонаж
    резонирует). Библиотека неофициальная/капризная — любая ошибка/анти-бот
    защита превращается в [], не роняя досье."""
    try:
        from trendspy import Trends
    except ImportError:
        print("  !! gtrends_related: нет библиотеки trendspy — пропуск", flush=True)
        return []

    try:
        tr = Trends()
        related = tr.related_queries(title)
    except Exception as e:  # noqa: BLE001 — неофициальный протокол, сбои ожидаемы
        print(f"  !! gtrends_related({title!r}) не удалось получить: {e}", flush=True)
        return []

    # related_queries может вернуть dict({"top":..., "rising":...}), обычный список,
    # либо pandas.DataFrame (замечено на реальном прогоне) — DataFrame НЕЛЬЗЯ
    # проверять через `or`/`if rising:` (ValueError: truth value of a DataFrame is
    # ambiguous), поэтому явные isinstance-ветки без бинарной логики на объекте.
    rising = related
    if isinstance(related, dict):
        rising = related.get("rising")
        if rising is None:
            rising = related.get("top")

    out = []
    try:
        # pandas.DataFrame -> список словарей построчно (если библиотека не установлена
        # или объект другого типа — просто пропускаем эту ветку, .to_dict есть только у DF).
        if hasattr(rising, "to_dict") and not isinstance(rising, (list, dict)):
            rising = rising.to_dict("records")
        if rising is None:
            rising = []
        for row in list(rising)[:15]:
            if isinstance(row, dict):
                query = str(row.get("query") or row.get("keyword") or "").strip()
            else:
                query = str(getattr(row, "query", "") or getattr(row, "keyword", "")).strip()
            if query:
                out.append(query)
    except Exception as e:  # noqa: BLE001 — формат ответа trendspy нестабилен между версиями
        print(f"  !! gtrends_related({title!r}) не удалось разобрать ответ: {e}", flush=True)
        return []
    return out


# ── Синтез Claude ─────────────────────────────────────────────────────────────

SYSTEM_FRANCHISE = (
    "Ты аналитик фан-метрик для продюсера принтов на футболки. Тебе дают СЫРЫЕ "
    "ИЗМЕРЕННЫЕ данные о популярности персонажей/актёров/моментов франшизы из "
    "нескольких источников (AniList favourites, MyAnimeList favorites, порядок "
    "каста и popularity TMDB, просмотры YouTube-эдитов, растущие поисковые "
    "запросы). ТВОЯ ЗАДАЧА — взвесить и ранжировать персонажей ПО ЭТИМ ЧИСЛАМ, "
    "а НЕ по собственным знаниям о том, кто \"должен\" быть популярен. Если "
    "формально главный герой имеет МЕНЬШЕ favourites/просмотров, чем "
    "второстепенный — второстепенный ранжируется ВЫШЕ, это и есть смысл задачи "
    "(пример: в Re:Zero номинальная героиня Эмилия уступает по favourites Рем). "
    "Если сигналов по персонажу мало или нет вообще — НЕ придумывай его "
    "популярность, просто не включай в результат или дай низкий score. Если "
    "данных мало по всей франшизе — верни МЕНЬШЕ персонажей, это честно, лучше "
    "короткое достоверное досье, чем длинное выдуманное. "
    "Для каждого персонажа print_moment — КОНКРЕТНАЯ сцена/арка/боевая форма/поза, "
    "подходящая для принта (не общая фраза), можно оставить пустой строкой, если "
    "по входным данным не видно конкретного момента. "
    "Отвечай СТРОГО одним JSON-объектом (без markdown, без пояснений) формата: "
    "{\"title\": \"...\", "
    "\"characters\": [{\"name_ru\": \"...\", \"name_en\": \"...\", "
    "\"score\": 0-100, \"why\": \"кратко: конкретные числа-сигналы, которые "
    "привели к этому score (favourites/favorites/просмотры/каст-order/поиск)\", "
    "\"print_moment\": \"конкретная сцена/арка/форма для принта или пустая строка\"}], "
    "\"moments\": [{\"name\": \"...\", \"evidence\": \"из названия эдита + просмотры, "
    "или из растущего поискового запроса\"}]}. "
    "characters ОБЯЗАТЕЛЬНО отсортированы по score по убыванию."
)

# gemini-pro-latest — «думающая» модель: часть бюджета уходит на рассуждение,
# а досье — многосоставный JSON (персонажи + моменты). 4000 обрезало массив
# посреди (диагностировано 2026-07-21: персонаж выходил с оборванным именем).
_MAX_TOKENS_DOSSIER = int(os.getenv("DOSSIER_MAX_TOKENS", "12000"))


def _build_synthesis_input(title: str, anilist_chars: list, jikan_chars: list,
                            tmdb_cast: list, youtube_edits: list,
                            gtrends_rising: list) -> str:
    lines = [f"ТАЙТЛ: {title}"]

    if anilist_chars:
        lines.append("\nAniList — персонажи по favourites (избранное фанатов), роль:")
        for c in anilist_chars:
            lines.append(f"- {c['name']}: favourites={c['favourites']} role={c['role']}")
    if jikan_chars:
        lines.append("\nMyAnimeList (Jikan) — персонажи по favorites, роль:")
        for c in jikan_chars:
            lines.append(f"- {c['name']}: favorites={c['favorites']} role={c['role']}")
    if tmdb_cast:
        lines.append("\nTMDB — каст (порядок = billing order, чем меньше — тем главнее "
                     "по титрам) + popularity актёра:")
        for c in tmdb_cast:
            lines.append(f"- {c['name']} играет {c['character']}: order={c['order']} "
                         f"popularity={c['popularity']}")
    if youtube_edits:
        lines.append("\nYouTube — топ-эдиты по теме (название видео + просмотры):")
        for e in youtube_edits:
            lines.append(f"- \"{e['title']}\": views={e['views']}")
    if gtrends_rising:
        lines.append("\nGoogle Trends — растущие запросы, которые дописывают к тайтлу "
                     "в поиске:")
        for q in gtrends_rising:
            lines.append(f"- {q}")

    if len(lines) == 1:
        lines.append("\n(ни один источник не дал данных — верни честно короткое или "
                     "пустое досье, не выдумывай числа)")

    lines.append("\nСобери досье строго по инструкции — характеры отсортированы по "
                 "score по убыванию, ранжирование ОПИРАЕТСЯ на числа выше.")
    return "\n".join(lines)


def _ask_claude_dossier(user_text: str) -> str:
    """Имя _ask_claude_dossier — историческое (обратная совместимость вызовов/
    тестов), реально зовёт llm_provider.generate_text — провайдер переключаем
    через config.ART_DIRECTOR_PROVIDER (gemini по умолчанию, см. llm_provider.py),
    не только Claude."""
    try:
        return llm_provider.generate_text(
            SYSTEM_FRANCHISE, user_text, max_tokens=_MAX_TOKENS_DOSSIER)
    except RuntimeError as e:
        # llm_provider.generate_text поднимает RuntimeError на ЛЮБОЙ сбой провайдера
        # (сеть/баланс/rate-limit/5xx, независимо от gemini/openai/anthropic) — тот же
        # сигнал, что и невалидный JSON: _parse_dossier("") -> None -> вызывающий
        # _ask_and_parse_dossier_with_retry честно ретраит один раз, при двойном сбое
        # бросает ЯВНУЮ RuntimeError (не тихий откат) — см. build_dossier/_collect_dossiers,
        # которые уже переживают падение одного тайтла.
        print(f"  !! franchise_scout: вызов арт-директора (LLM) не удался: {e}", flush=True)
        return ""


def _repair_characters_array(text: str) -> list:
    """Ремонт ИМЕННО массива characters, когда объект верхнего уровня обрезан
    посреди этого массива (типичный обрыв по max_tokens) — то же ремонтное
    правило, что theme_scout._parse_scout использует для своего верхнеуровневого
    массива: от '[' после "characters" до последнего полного объекта '}' + ']'.
    Возвращает [] (не None), если найти массив вообще не удалось — вызывающий
    код это уже трактует как часть общего сбоя парсинга."""
    key_pos = text.find('"characters"')
    if key_pos == -1:
        return []
    arr_start = text.find("[", key_pos)
    if arr_start == -1:
        return []
    cut = text.rfind("}", arr_start)
    if cut == -1 or cut <= arr_start:
        return []
    candidate = text[arr_start:cut + 1] + "]"
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _parse_dossier(text: str) -> dict:
    """Парсит JSON-объект досье; ремонт обрезанного ответа (та же ИДЕЯ, что
    theme_scout._parse_scout, — принять частичный результат, а не выбросить всё):
    сперва пробуем весь объект как есть (от первой '{' до последней '}'), при
    неудаче вытаскиваем ХОТЯ БЫ массив characters отдельно (см.
    _repair_characters_array) и собираем title/moments best-effort. Возвращает
    None при полном сбое парсинга (НЕ путать с валидным пустым досье)."""
    candidates = []
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        candidates.append(m.group(0))
    start = text.find("{")
    cut = text.rfind("}")
    if start != -1 and cut > start:
        candidates.append(text[start:cut + 1])

    data = None
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                data = parsed
                break
        except Exception:
            continue

    if data is None:
        # Объект целиком не восстановить (обрыв пришёлся на середину characters) —
        # попробуем спасти хотя бы массив персонажей, это и есть основная ценность
        # досье (лучше 5 персонажей из 6, чем выбросить весь ответ).
        repaired_characters = _repair_characters_array(text)
        if not repaired_characters:
            return None
        title_match = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        data = {
            "title": title_match.group(1) if title_match else "",
            "characters": repaired_characters,
            "moments": [],  # moments обычно идёт ПОСЛЕ characters в JSON — при обрыве
                            # внутри characters он в принципе не мог быть в ответе
        }

    characters = []
    for x in (data.get("characters") or []):
        if not (isinstance(x, dict) and str(x.get("name_ru", "") or x.get("name_en", "")).strip()):
            continue
        try:
            score = float(x.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        characters.append({
            "name_ru": str(x.get("name_ru") or "").strip(),
            "name_en": str(x.get("name_en") or "").strip(),
            "score": max(0.0, min(100.0, score)),
            "why": str(x.get("why") or "").strip(),
            "print_moment": str(x.get("print_moment") or "").strip(),
        })
    characters.sort(key=lambda c: c["score"], reverse=True)

    moments = []
    for x in (data.get("moments") or []):
        if isinstance(x, dict) and str(x.get("name", "")).strip():
            moments.append({
                "name": str(x["name"]).strip(),
                "evidence": str(x.get("evidence") or "").strip(),
            })

    return {
        "title": str(data.get("title") or "").strip(),
        "characters": characters,
        "moments": moments,
    }


def _dump_dossier_failure(text: str, attempt: int) -> None:
    """Сырой ответ Claude при сбое парсинга — в файл (та же схема, что
    theme_scout._dump_scout_failure), иначе сбой недиагностируем."""
    try:
        dump_dir = HERE / "out_batch" / "scout_failures"
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (dump_dir / f"franchise_fail_{stamp}_try{attempt}.txt").write_text(
            text or "<пустой ответ>", encoding="utf-8")
    except Exception:
        pass


def _ask_and_parse_dossier_with_retry(user_text: str) -> dict:
    """Один вызов синтезатора + парсинг, с 1 ретраем при сбое JSON. При двойном
    сбое — ЯВНАЯ ошибка (не тихий откат на пустое досье), сырой ответ в файл."""
    text = _ask_claude_dossier(user_text)
    dossier = _parse_dossier(text)
    if dossier is None:
        _dump_dossier_failure(text, attempt=1)
        text = _ask_claude_dossier(user_text)
        dossier = _parse_dossier(text)
        if dossier is None:
            _dump_dossier_failure(text, attempt=2)
            raise RuntimeError(
                "franchise_scout: синтезатор Claude не смог собрать валидный JSON "
                "дважды подряд — сырые ответы сохранены в out_batch/scout_failures/")
    return dossier


# ── Кэш ────────────────────────────────────────────────────────────────────

def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.strip().lower()).strip("_")
    return slug or "untitled"


def _cache_path(title: str) -> Path:
    date_str = datetime.now().strftime("%Y-%m-%d")
    return CACHE_DIR / f"{_slugify(title)}_{date_str}.json"


def _read_cache(title: str) -> dict:
    """Читает кэш досье за СЕГОДНЯ, если он есть. None, если кэша нет или он битый
    (битый кэш не роняет build_dossier — просто пересобираем заново)."""
    path = _cache_path(title)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  !! кэш {path.name} повреждён ({e}) — пересобираю досье", flush=True)
        return None


def _write_cache(title: str, dossier: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(title).write_text(json.dumps(dossier, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  !! не удалось записать кэш досье: {e}", flush=True)


def _signals_cache_path(title: str) -> Path:
    date_str = datetime.now().strftime("%Y-%m-%d")
    return CACHE_DIR / f"signals_{_slugify(title)}_{date_str}.json"


def _read_signals_cache(title: str) -> dict:
    """Читает кэш СЫРЫХ сигналов (собранных ДО синтеза) за сегодня, если он есть.
    None, если кэша нет или он битый (битый кэш не роняет build_dossier — просто
    собираем сигналы заново из сети)."""
    path = _signals_cache_path(title)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  !! кэш сигналов {path.name} повреждён ({e}) — собираю заново", flush=True)
        return None


def _write_signals_cache(title: str, signals: dict) -> None:
    """Пишется СРАЗУ после сбора сигналов, ДО вызова синтезатора Claude — так при
    временном сбое синтеза (rate-limit/5xx/баланс) уже собранные сигналы (в т.ч.
    дорогая YouTube-квота) не теряются: следующий вызов build_dossier в тот же
    день их переиспользует вместо повторного сетевого сбора."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _signals_cache_path(title).write_text(
            json.dumps(signals, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  !! не удалось записать кэш сигналов: {e}", flush=True)


# ── Главная функция ───────────────────────────────────────────────────────

def build_dossier(title: str, kind: str = "auto") -> dict:
    """Полное досье франшизы: собирает сигналы всех источников (каждый —
    graceful degradation) -> один синтез-вызов Claude -> {"title", "characters",
    "moments"}. Кэшируется на день (см. _read_cache/_write_cache) — повторный
    вызов для того же title в тот же день НЕ дёргает сеть.

    kind: "auto" (пробовать и anilist/jikan, и tmdb — дешевле, чем гадать заранее),
    "anime" (только anilist/jikan), "tv"/"movie" (только tmdb). YouTube и Google
    Trends сигналы собираются всегда (не зависят от kind).

    ВНИМАНИЕ ПО КВОТЕ: если сеть реально дёргается (кэша нет), youtube_edits тратит
    ~101 юнит квоты YouTube (100 за search.list + 1 за videos.list) — это СТРОГО
    один раз на вызов build_dossier, не за персонажа. Если синтез Claude временно
    недоступен, уже собранные сигналы кэшируются ОТДЕЛЬНО (см. _write_signals_cache)
    ДО вызова синтезатора — повторный вызов build_dossier в тот же день эту квоту
    заново не тратит, даже если готовое досье в тот раз не собралось."""
    cached = _read_cache(title)
    if cached is not None:
        print(f"  франшиза {title!r}: досье взято из кэша ({_cache_path(title).name}), "
              f"сеть не дёргалась", flush=True)
        return cached

    cached_signals = _read_signals_cache(title)
    if cached_signals is not None:
        print(f"  франшиза {title!r}: сигналы взяты из кэша "
              f"({_signals_cache_path(title).name}) — сеть не дёргалась, "
              f"пробую синтез заново", flush=True)
        anilist_chars = cached_signals.get("anilist_chars", [])
        jikan_chars = cached_signals.get("jikan_chars", [])
        tmdb_cast = cached_signals.get("tmdb_cast", [])
        youtube_edits = cached_signals.get("youtube_edits", [])
        gtrends_rising = cached_signals.get("gtrends_rising", [])
    else:
        print(f"  франшиза {title!r}: собираю сигналы (kind={kind})...", flush=True)

        anilist_chars, jikan_chars, tmdb_cast = [], [], []
        if kind in ("auto", "anime"):
            anilist_chars = _anilist_characters(title)
            jikan_chars = _jikan_characters(title)
        if kind in ("auto", "tv", "movie"):
            tmdb_cast = _tmdb_credits(title)

        youtube_edits = _youtube_edits(title)
        gtrends_rising = _gtrends_related(title)

        print(f"  сигналы: anilist={len(anilist_chars)} jikan={len(jikan_chars)} "
              f"tmdb={len(tmdb_cast)} youtube={len(youtube_edits)} "
              f"gtrends={len(gtrends_rising)}", flush=True)

        _write_signals_cache(title, {
            "anilist_chars": anilist_chars,
            "jikan_chars": jikan_chars,
            "tmdb_cast": tmdb_cast,
            "youtube_edits": youtube_edits,
            "gtrends_rising": gtrends_rising,
        })

    user_text = _build_synthesis_input(title, anilist_chars, jikan_chars, tmdb_cast,
                                        youtube_edits, gtrends_rising)
    dossier = _ask_and_parse_dossier_with_retry(user_text)
    if not dossier.get("title"):
        dossier["title"] = title

    _write_cache(title, dossier)
    return dossier


# ── CLI ────────────────────────────────────────────────────────────────────

def _print_dossier(dossier: dict) -> None:
    print(f"\n=== ДОСЬЕ ФРАНШИЗЫ: {dossier.get('title', '?')} ===")
    characters = dossier.get("characters") or []
    if not characters:
        print("  (персонажей не найдено — сигналов недостаточно)")
    for i, c in enumerate(characters, 1):
        name = c.get("name_ru") or c.get("name_en") or "?"
        name_en = f" ({c['name_en']})" if c.get("name_en") and c.get("name_ru") else ""
        print(f"  {i:>2}. [{c.get('score', 0):>5.1f}] {name}{name_en}")
        if c.get("why"):
            print(f"       почему: {c['why']}")
        if c.get("print_moment"):
            print(f"       для принта: {c['print_moment']}")

    moments = dossier.get("moments") or []
    if moments:
        print("\n  МОМЕНТЫ:")
        for m in moments:
            evidence = f" — {m['evidence']}" if m.get("evidence") else ""
            print(f"    - {m.get('name', '?')}{evidence}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    import argparse
    ap = argparse.ArgumentParser(
        description="Вскрытие франшизы: измеренная популярность персонажей/моментов")
    ap.add_argument("title", help="название тайтла (аниме/сериал/дорама/фильм)")
    ap.add_argument("--kind", choices=("auto", "anime", "tv", "movie"), default="auto",
                     help="тип тайтла (дефолт auto — пробует все применимые источники)")
    args = ap.parse_args()

    try:
        dossier = build_dossier(args.title, args.kind)
    except Exception as e:  # noqa: BLE001 — CLI: явная ошибка, не тихий сбой
        print(f"\n!! не удалось собрать досье: {e}", flush=True)
        sys.exit(1)

    _print_dossier(dossier)


if __name__ == "__main__":
    main()
