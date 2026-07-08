# -*- coding: utf-8 -*-
"""character_ref.py — каноничный референс-портрет персонажа для генерации ПО
РЕФЕРЕНСУ (не только по текстовому промпту).

Зачем: nano-banana по одному текстовому описанию рисует персонажа «по мотивам»
(лицо/канон-приметы приблизительные — например у Кенпачи Зараки нет фирменной
повязки на глазу), при этом РЕАЛЬНЫЕ объекты (машины и т.п.) она рисует отлично
без референса. Лечение — подмешать во входной запрос картинку-референс: gemini-
2.5-flash-image (nano-banana) поддерживает image+text на входе и умеет перерисовать
персонажа в новой позе, сохранив опознаваемость лица/причёски/костюма.

get_reference(character_en, title_en="") -> PIL.Image | None:
    1. Источник 1 — Jikan (MyAnimeList): GET /v4/characters?q=<character_en>&limit=10,
       выбор лучшего совпадения (точность имени + максимум favorites; при неоднозначности
       топ-1 vs топ-2 — доп. запрос /v4/characters/{id}/anime, предпочесть кандидата,
       у которого встречается title_en).
    2. Источник 2 (fallback) — AniList GraphQL: Character(search: title_en) { image { large } }.
    3. Кэш на диске: data/char_refs/<slug>.jpg — повторный запрос читает файл, сеть не
       трогает.
    4. Любой сбой -> None с предупреждением (вызывающий код продолжает генерацию без
       референса, как раньше — graceful degradation, ничего не падает).
"""
from __future__ import annotations

import io
import re
import time
import unicodedata
from pathlib import Path

import requests
from PIL import Image

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "data" / "char_refs"

_REQUEST_TIMEOUT = 15
_JIKAN_BASE = "https://api.jikan.moe/v4"
_ANILIST_URL = "https://graphql.anilist.co"

_ANILIST_CHARACTER_QUERY = """
query ($search: String) {
  Character(search: $search) {
    name { full }
    image { large }
  }
}
"""


def _slug(character_en: str) -> str:
    """Имя персонажа -> безопасное имя файла (латиница/цифры/дефис, нижний регистр)."""
    norm = unicodedata.normalize("NFKD", character_en)
    ascii_only = norm.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug or "character"


def _cache_path(character_en: str) -> Path:
    return CACHE_DIR / f"{_slug(character_en)}.jpg"


def _load_cached(character_en: str) -> Image.Image | None:
    path = _cache_path(character_en)
    if not path.exists():
        return None
    try:
        img = Image.open(path)
        img.load()
        return img.convert("RGB")
    except Exception as e:  # noqa: BLE001 — битый файл кэша не должен ронять генерацию
        print(f"  !! character_ref: кэш {path.name} повреждён ({e}) — перезапрашиваю", flush=True)
        return None


def _save_cache(character_en: str, img: Image.Image) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(_cache_path(character_en), format="JPEG", quality=92)
    except Exception as e:  # noqa: BLE001 — кэш не критичен, просто следующий раз сходим в сеть
        print(f"  !! character_ref: не удалось сохранить кэш для {character_en!r}: {e}",
              flush=True)


def _download_image(url: str) -> Image.Image | None:
    """Скачивает картинку по URL -> PIL.Image (RGB). None при любом сбое сети."""
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        img.load()
        return img.convert("RGB")
    except Exception as e:  # noqa: BLE001
        print(f"  !! character_ref: не удалось скачать референс {url!r}: {e}", flush=True)
        return None


# ── Источник 1: Jikan ────────────────────────────────────────────────────────

def _jikan_search_characters(character_en: str) -> list:
    """GET /v4/characters?q=<character_en>&limit=10 -> список кандидатов
    {"mal_id", "name", "favorites", "image_url"}. Пусто при любой ошибке сети."""
    try:
        resp = requests.get(f"{_JIKAN_BASE}/characters",
                             params={"q": character_en, "limit": 10},
                             timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 429:
            print("  !! character_ref: Jikan 429 Too Many Requests — пропуск источника",
                  flush=True)
            return []
        resp.raise_for_status()
        rows = resp.json().get("data") or []
    except Exception as e:  # noqa: BLE001
        print(f"  !! character_ref: Jikan-поиск {character_en!r} не удался: {e}", flush=True)
        return []

    out = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        image_url = ((row.get("images") or {}).get("jpg") or {}).get("image_url") or ""
        out.append({
            "mal_id": row.get("mal_id"),
            "name": name,
            "favorites": int(row.get("favorites") or 0),
            "image_url": image_url,
        })
    return out


def _jikan_character_titles(mal_id: int) -> list:
    """GET /v4/characters/{id}/anime -> список названий тайтлов персонажа (romaji/en),
    используется ТОЛЬКО для разрешения неоднозначности топ-1 vs топ-2 по имени.
    Пусто при любой ошибке — тогда неоднозначность решается по favorites."""
    try:
        resp = requests.get(f"{_JIKAN_BASE}/characters/{mal_id}/anime",
                             timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json().get("data") or []
    except Exception as e:  # noqa: BLE001
        print(f"  !! character_ref: Jikan /characters/{mal_id}/anime не удался: {e}",
              flush=True)
        return []
    titles = []
    for row in rows:
        anime = row.get("anime") or {}
        t = str(anime.get("title") or "").strip()
        if t:
            titles.append(t)
    return titles


def _name_matches(candidate_name: str, character_en: str) -> bool:
    """Точность совпадения имени: точное совпадение (регистронезависимо) ИЛИ запрошенное
    имя целиком встречается как отдельные слова в имени кандидата (Jikan часто хранит имя
    в формате "Zaraki, Kenpachi")."""
    cand = candidate_name.strip().lower()
    want = character_en.strip().lower()
    if cand == want:
        return True
    cand_words = set(re.split(r"[,\s]+", cand))
    want_words = set(re.split(r"[,\s]+", want))
    return bool(want_words) and want_words.issubset(cand_words)


def _pick_best_jikan_candidate(candidates: list, character_en: str, title_en: str) -> dict | None:
    """Выбор лучшего совпадения: сперва только кандидаты с точным/полным совпадением
    имени (если такие есть), из них — максимум favorites. При неоднозначности между
    топ-1 и топ-2 по favorites (оба содержат искомое имя) — доп. запрос за тайтлами,
    предпочесть кандидата, у которого встречается title_en."""
    with_image = [c for c in candidates if c.get("image_url")]
    if not with_image:
        return None

    name_matched = [c for c in with_image if _name_matches(c["name"], character_en)]
    pool = name_matched or with_image
    pool_sorted = sorted(pool, key=lambda c: c["favorites"], reverse=True)

    top = pool_sorted[0]
    if len(pool_sorted) >= 2 and title_en:
        second = pool_sorted[1]
        # Неоднозначность: оба реально похожи по имени и по favorites (в пределах 2x) —
        # стоит свериться по тайтлу франшизы, а не слепо брать топ по favorites.
        close_favorites = second["favorites"] > 0 and top["favorites"] / max(second["favorites"], 1) < 2.0
        if close_favorites:
            for cand in (top, second):
                if not cand.get("mal_id"):
                    continue
                titles = _jikan_character_titles(cand["mal_id"])
                if any(title_en.strip().lower() in t.lower() for t in titles):
                    return cand
    return top


def _get_reference_jikan(character_en: str, title_en: str) -> Image.Image | None:
    candidates = _jikan_search_characters(character_en)
    if not candidates:
        return None
    best = _pick_best_jikan_candidate(candidates, character_en, title_en)
    if not best:
        return None
    return _download_image(best["image_url"])


# ── Источник 2 (fallback): AniList ───────────────────────────────────────────

def _get_reference_anilist(character_en: str) -> Image.Image | None:
    try:
        resp = requests.post(
            _ANILIST_URL,
            json={"query": _ANILIST_CHARACTER_QUERY, "variables": {"search": character_en}},
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5") or "5")
            print(f"  !! character_ref: AniList 429 — жду {retry_after}с и повторяю один раз",
                  flush=True)
            time.sleep(retry_after)
            resp = requests.post(
                _ANILIST_URL,
                json={"query": _ANILIST_CHARACTER_QUERY, "variables": {"search": character_en}},
                timeout=_REQUEST_TIMEOUT,
            )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        print(f"  !! character_ref: AniList-запрос {character_en!r} не удался: {e}", flush=True)
        return None

    if payload.get("errors"):
        print(f"  !! character_ref: AniList вернул ошибку для {character_en!r}: "
              f"{payload['errors']}", flush=True)
        return None

    node = (payload.get("data") or {}).get("Character") or {}
    image_url = (node.get("image") or {}).get("large") or ""
    if not image_url:
        print(f"  !! character_ref: AniList не нашёл персонажа {character_en!r}", flush=True)
        return None
    return _download_image(image_url)


# ── Точка входа ───────────────────────────────────────────────────────────────

def get_reference(character_en: str, title_en: str = "") -> Image.Image | None:
    """Каноничный референс-портрет персонажа (PIL.Image, RGB) для подмешивания в
    generate_image(reference=...). None при любом сбое — вызывающий код продолжает
    генерацию по чистому тексту, как раньше (graceful degradation)."""
    character_en = (character_en or "").strip()
    if not character_en:
        return None

    cached = _load_cached(character_en)
    if cached is not None:
        return cached

    img = _get_reference_jikan(character_en, title_en)
    if img is None:
        print(f"  character_ref: Jikan без результата для {character_en!r} — пробую AniList",
              flush=True)
        img = _get_reference_anilist(character_en)

    if img is None:
        print(f"  !! character_ref: референс для {character_en!r} не найден ни в одном "
              f"источнике — генерация пойдёт без референса", flush=True)
        return None

    _save_cache(character_en, img)
    return img
