# -*- coding: utf-8 -*-
"""Тесты character_ref.py — офлайн-логика выбора кандидата, кэша и graceful
degradation. ВСЯ сеть (requests.get/requests.post) мокается через monkeypatch,
реальные вызовы к Jikan/AniList НЕ делаются.

Также покрыт providers.py::_generate_gemini — сборка тела запроса Gemini с
reference (inline_data ПЕРЕД text), и art_director.py::_parse — новые поля
character_en/title_en (с default "" и без падения на старом JSON).

Запуск:
    cd print-factory-nb && python -m pytest tests/test_character_ref.py -v
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image  # noqa: E402

import art_director  # noqa: E402
import character_ref as cref  # noqa: E402
import providers  # noqa: E402


def _tiny_jpeg_bytes() -> bytes:
    img = Image.new("RGB", (4, 4), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, json_data=None, content=b"", status_code=200, headers=None):
        self._json = json_data
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.text = str(json_data)[:300] if json_data is not None else ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ------------------------------------------------------------- выбор лучшего кандидата (Jikan)
def test_pick_best_jikan_candidate_prefers_higher_favorites_among_namesakes():
    """Два тёзки (одинаковое имя, разные favorites) — выбираем с БОЛЬШИМ favorites,
    без неоднозначности по тайтлу (favorites отличаются больше чем в 2 раза, доп.
    запрос за тайтлами НЕ нужен)."""
    candidates = [
        {"mal_id": 1, "name": "Kenpachi Zaraki", "favorites": 500,
         "image_url": "http://x/1.jpg"},
        {"mal_id": 2, "name": "Kenpachi Zaraki", "favorites": 12000,
         "image_url": "http://x/2.jpg"},
    ]
    best = cref._pick_best_jikan_candidate(candidates, "Kenpachi Zaraki", "")
    assert best["mal_id"] == 2


def test_pick_best_jikan_candidate_disambiguates_close_favorites_by_title(monkeypatch):
    """Топ-1 и топ-2 по favorites близки (<2x разницы) и оба совпадают по имени —
    неоднозначность должна решаться доп. запросом за тайтлами: побеждает кандидат,
    у которого title_en реально встречается в его списке аниме."""
    candidates = [
        {"mal_id": 10, "name": "Rem", "favorites": 9000, "image_url": "http://x/10.jpg"},
        {"mal_id": 20, "name": "Rem", "favorites": 8000, "image_url": "http://x/20.jpg"},
    ]

    def fake_titles(mal_id):
        return {10: ["Some Other Anime"], 20: ["Re:Zero kara Hajimeru Isekai Seikatsu"]}[mal_id]

    monkeypatch.setattr(cref, "_jikan_character_titles", fake_titles)
    best = cref._pick_best_jikan_candidate(candidates, "Rem", "Re:Zero")
    assert best["mal_id"] == 20


def test_pick_best_jikan_candidate_filters_out_no_image_candidates():
    """Кандидаты без картинки не должны выбираться — если у лучшего по favorites
    нет image_url, берём следующего, у кого она есть."""
    candidates = [
        {"mal_id": 1, "name": "X", "favorites": 99999, "image_url": ""},
        {"mal_id": 2, "name": "X", "favorites": 10, "image_url": "http://x/2.jpg"},
    ]
    best = cref._pick_best_jikan_candidate(candidates, "X", "")
    assert best["mal_id"] == 2


def test_pick_best_jikan_candidate_no_candidates_returns_none():
    assert cref._pick_best_jikan_candidate([], "Nobody", "") is None


# ------------------------------------------------------------- кэш на диске
def test_get_reference_caches_to_disk_second_call_no_network(monkeypatch, tmp_path):
    """Второй вызов get_reference для того же персонажа должен читать файл кэша,
    а НЕ дёргать сеть — считаем количество сетевых вызовов через счётчик."""
    monkeypatch.setattr(cref, "CACHE_DIR", tmp_path / "char_refs")
    calls = {"n": 0}

    def fake_jikan_search(character_en):
        calls["n"] += 1
        return [{"mal_id": 1, "name": character_en, "favorites": 100,
                  "image_url": "http://x/img.jpg"}]

    def fake_download(url):
        return Image.new("RGB", (16, 16), (10, 20, 30))

    monkeypatch.setattr(cref, "_jikan_search_characters", fake_jikan_search)
    monkeypatch.setattr(cref, "_download_image", fake_download)

    img1 = cref.get_reference("Kenpachi Zaraki")
    assert img1 is not None
    assert calls["n"] == 1

    img2 = cref.get_reference("Kenpachi Zaraki")
    assert img2 is not None
    assert calls["n"] == 1, "второй вызов не должен был дёргать сеть — ожидался кэш"


def test_cache_path_is_slugified_and_reused():
    """Кэш-путь строится из безопасного slug (латиница/цифры/дефис) — разные
    варианты написания одного и того же имени дают предсказуемый путь."""
    p1 = cref._cache_path("Kenpachi Zaraki")
    assert p1.name == "kenpachi-zaraki.jpg"
    assert p1.parent == cref.CACHE_DIR


def test_cache_path_includes_franchise_to_avoid_namesake_collisions():
    """Одинаковое имя в разных тайтлах не должно переиспользовать чужой портрет."""
    gachi = cref._cache_path("Enjin", "Gachiakuta")
    other = cref._cache_path("Enjin", "Some Other Anime")

    assert gachi != other
    assert gachi.name == "enjin-gachiakuta.jpg"


def test_anilist_reference_prefers_candidate_from_requested_title(monkeypatch):
    payload = {
        "data": {"Page": {"characters": [
            {
                "name": {"full": "Enjin"},
                "favourites": 9000,
                "image": {"large": "https://example.test/wrong.jpg"},
                "media": {"nodes": [
                    {"title": {"romaji": "Some Other Anime", "english": None}},
                ]},
            },
            {
                "name": {"full": "Enjin"},
                "favourites": 2300,
                "image": {"large": "https://example.test/gachiakuta.jpg"},
                "media": {"nodes": [
                    {"title": {"romaji": "Gachiakuta", "english": "Gachiakuta"}},
                ]},
            },
        ]}},
    }
    monkeypatch.setattr(
        cref.requests, "post", lambda *args, **kwargs: _FakeResponse(json_data=payload)
    )
    monkeypatch.setattr(cref, "_download_image", lambda url: url)

    result = cref._get_reference_anilist("Enjin", "Gachiakuta")

    assert result == "https://example.test/gachiakuta.jpg"


# ------------------------------------------------------------- graceful degradation
def test_get_reference_network_failure_returns_none(monkeypatch, tmp_path):
    """Оба источника (Jikan и AniList) недоступны — get_reference не бросает
    исключение, а возвращает None (генерация продолжится без референса). Источники
    сами ловят свои сетевые ошибки и отдают None/[] — здесь проверяем контракт
    get_reference поверх этого (реальный сбой сети внутри источников проверяется
    отдельными тестами ниже: test_jikan_search_characters_returns_empty_on_network_error,
    test_anilist_reference_returns_none_on_network_error)."""
    monkeypatch.setattr(cref, "CACHE_DIR", tmp_path / "char_refs")
    monkeypatch.setattr(cref, "_get_reference_jikan", lambda c, t: None)
    monkeypatch.setattr(cref, "_get_reference_anilist", lambda c, t=None: None)

    result = cref.get_reference("Несуществующий Персонаж")
    assert result is None


def test_jikan_search_characters_returns_empty_on_network_error(monkeypatch):
    """Сама сетевая функция graceful: requests.get кидает исключение -> []."""
    def raise_get(*args, **kwargs):
        raise ConnectionError("нет сети")

    monkeypatch.setattr(cref.requests, "get", raise_get)
    assert cref._jikan_search_characters("Goku") == []


def test_anilist_reference_returns_none_on_network_error(monkeypatch):
    def raise_post(*args, **kwargs):
        raise ConnectionError("нет сети")

    monkeypatch.setattr(cref.requests, "post", raise_post)
    assert cref._get_reference_anilist("Goku") is None


def test_get_reference_empty_character_returns_none_without_network(monkeypatch):
    """Пустая строка character_en -> None сразу, без единого сетевого вызова."""
    calls = {"n": 0}
    monkeypatch.setattr(cref, "_get_reference_jikan",
                        lambda c, t: calls.__setitem__("n", calls["n"] + 1))
    assert cref.get_reference("") is None
    assert calls["n"] == 0


# ------------------------------------------------------------- providers.py: reference -> inline_data
def test_generate_gemini_request_body_has_inline_data_before_text(monkeypatch):
    """Gemini-запрос С референсом: contents.parts[0] = inline_data (JPEG base64
    референса), parts[1] = text промпта — референс СТРОГО перед текстом."""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        img = Image.new("RGB", (8, 8), (0, 255, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        import base64
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return _FakeResponse(json_data={
            "candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": b64}}
            ]}}]
        })

    monkeypatch.setattr(providers, "config", providers.config)
    monkeypatch.setattr(providers.config, "GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(providers.requests, "post", fake_post)

    reference = Image.new("RGB", (32, 32), (10, 20, 30))
    out = providers._generate_gemini("draw a hero", seed=1, reference=reference)

    assert out is not None
    parts = captured["json"]["contents"][0]["parts"]
    assert len(parts) == 2
    assert "inline_data" in parts[0]
    assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"
    assert parts[1] == {"text": "draw a hero"}


def test_generate_gemini_request_body_without_reference_has_only_text(monkeypatch):
    """Без референса (reference=None) — parts содержит ТОЛЬКО текст, как раньше
    (обратная совместимость поведения до этой задачи)."""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        img = Image.new("RGB", (8, 8), (0, 255, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        import base64
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return _FakeResponse(json_data={
            "candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": b64}}
            ]}}]
        })

    monkeypatch.setattr(providers.config, "GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(providers.requests, "post", fake_post)

    providers._generate_gemini("draw a car", seed=1, reference=None)
    parts = captured["json"]["contents"][0]["parts"]
    assert parts == [{"text": "draw a car"}]


def test_generate_image_pollinations_ignores_reference_with_warning(monkeypatch, capsys):
    """pollinations + reference задан -> референс игнорируется (предупреждение в
    консоль), генерация всё равно проходит по тексту, как раньше — не падает."""
    monkeypatch.setattr(providers.config, "IMAGE_PROVIDER", "pollinations")
    fake_img = Image.new("RGB", (8, 8), (1, 2, 3))
    monkeypatch.setattr(providers, "_generate_pollinations",
                        lambda prompt, seed, model=None: fake_img)

    reference = Image.new("RGB", (10, 10), (5, 5, 5))
    out = providers.generate_image("some prompt", seed=1, reference=reference)

    assert out is fake_img
    captured = capsys.readouterr()
    assert "проигнорирован" in captured.out


# ------------------------------------------------------------- art_director._parse: новые поля
def test_parse_accepts_character_en_and_title_en():
    text = ('[{"prompt": "a young man with spiky hair", "chroma": "green", '
            '"slogan": "COME ON", "slogan_color": "orange", "kana": "",'
            '"character_en": "Kenpachi Zaraki", "title_en": "Bleach"}]')
    out = art_director._parse(text)
    assert len(out) == 1
    assert out[0]["character_en"] == "Kenpachi Zaraki"
    assert out[0]["title_en"] == "Bleach"


def test_parse_defaults_character_en_and_title_en_to_empty_string_when_missing():
    """Обратная совместимость: старый JSON БЕЗ character_en/title_en (напр. если
    Claude на редком прогоне их не прислал) — парсер не падает, поля = ""."""
    text = ('[{"prompt": "a red sports car", "chroma": "blue", '
            '"slogan": "DRIVE FAST", "slogan_color": "red", "kana": ""}]')
    out = art_director._parse(text)
    assert len(out) == 1
    assert out[0]["character_en"] == ""
    assert out[0]["title_en"] == ""


def test_parse_sanitizes_non_latin_characters_in_character_en():
    """character_en/title_en санируются (убираются символы вне латиницы/цифр/
    базовой пунктуации) — код-предохранитель, как у slogan/kana."""
    text = ('[{"prompt": "a hero", "chroma": "green", "slogan": "GO", '
            '"slogan_color": "red", "kana": "",'
            '"character_en": "Kenpachi\\u0301 Zaraki\\u3042", "title_en": "Bleach\\u3042"}]')
    out = art_director._parse(text)
    assert out[0]["character_en"] == "Kenpachi Zaraki"
    assert out[0]["title_en"] == "Bleach"
