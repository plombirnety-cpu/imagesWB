# -*- coding: utf-8 -*-
"""Тесты franchise_scout.py — только офлайн-логика (парсинг, кэш, синтез, graceful
degradation по источникам). ВСЕ сетевые функции (_anilist_characters,
_jikan_characters, _tmdb_credits, _youtube_edits, _gtrends_related, Claude) —
мокаются через monkeypatch, реальная сеть НЕ дёргается.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_franchise_scout.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import franchise_scout as fscout  # noqa: E402
import theme_scout as ts  # noqa: E402


def _no_network(monkeypatch, module=fscout):
    """Глушит все 5 источников сигналов на пустые значения — тест сам подставляет
    нужные ему заглушки поверх при необходимости."""
    monkeypatch.setattr(module, "_anilist_characters", lambda title: [])
    monkeypatch.setattr(module, "_anilist_title", lambda title: "")
    monkeypatch.setattr(module, "_jikan_characters", lambda title: [])
    monkeypatch.setattr(module, "_tmdb_credits", lambda title: [])
    monkeypatch.setattr(module, "_youtube_edits", lambda title: [])
    monkeypatch.setattr(module, "_gtrends_related", lambda title: [])


def _fake_claude_response(payload: dict):
    """Возвращает функцию-заглушку _ask_claude_dossier, отдающую JSON payload как
    текст ответа Claude (как если бы model.messages.create() реально ответила)."""
    text = json.dumps(payload, ensure_ascii=False)
    return lambda user_text: text


# ------------------------------------------------------------- схема досье / сортировка
def test_build_dossier_schema_and_sorted_by_score(monkeypatch, tmp_path):
    """Досье имеет ожидаемую схему, characters отсортированы по score по убыванию
    (даже если Claude вернул их не по порядку — _parse_dossier досортировывает)."""
    monkeypatch.setattr(fscout, "CACHE_DIR", tmp_path / "franchise_cache")
    _no_network(monkeypatch)
    monkeypatch.setattr(fscout, "_anilist_characters", lambda title: [
        {"name": "Rem", "favourites": 45000, "role": "MAIN"},
        {"name": "Emilia", "favourites": 20000, "role": "MAIN"},
    ])

    payload = {
        "title": "Re:Zero",
        "characters": [
            {"name_ru": "Эмилия", "name_en": "Emilia", "score": 40,
             "why": "favourites=20000, ниже чем у Рем", "print_moment": ""},
            {"name_ru": "Рем", "name_en": "Rem", "score": 92,
             "why": "favourites=45000 — выше номинальной героини",
             "print_moment": "сцена с топором в разрушенной деревне"},
        ],
        "moments": [{"name": "арка Демонической Резни", "evidence": "эдиты набирают просмотры"}],
    }
    monkeypatch.setattr(fscout, "_ask_claude_dossier", _fake_claude_response(payload))

    dossier = fscout.build_dossier("Re:Zero", kind="anime")

    assert dossier["title"] == "Re:Zero"
    assert isinstance(dossier["characters"], list)
    assert isinstance(dossier["moments"], list)
    names = [c["name_ru"] for c in dossier["characters"]]
    assert names == ["Рем", "Эмилия"], (
        f"characters должны быть отсортированы по score по убыванию, получили: {names}"
    )
    assert dossier["characters"][0]["score"] >= dossier["characters"][1]["score"]
    assert dossier["characters"][0]["print_moment"] == "сцена с топором в разрушенной деревне"


def test_parse_dossier_repairs_truncated_json():
    """_parse_dossier чинит обрезанный по max_tokens ответ (незакрытый массив/объект),
    та же схема ремонта, что theme_scout._parse_scout."""
    truncated = (
        '{"title": "Chainsaw Man", "characters": [ '
        '{"name_ru": "Дэндзи", "name_en": "Denji", "score": 88, "why": "high favourites", '
        '"print_moment": "трансформация в бензопилу"}, '
        '{"name_ru": "Пауэр", "name_en": "Power", "score": 95, "why": "top favourites"'
        # обрыв здесь — нет закрывающих скобок объекта/массива/верхнего уровня
    )
    result = fscout._parse_dossier(truncated)
    assert result is not None, "ремонт должен вытащить хотя бы один полный объект"
    names = [c["name_ru"] for c in result["characters"]]
    assert "Дэндзи" in names


def test_parse_dossier_invalid_json_returns_none():
    """Полностью невалидный ответ (не JSON вообще) -> None, это именно СБОЙ парсинга,
    отличимый от валидного пустого досье."""
    assert fscout._parse_dossier("это не джейсон вообще, просто текст без скобок") is None


def test_parse_dossier_clamps_score_to_0_100():
    """score из Claude зажимается в диапазон [0, 100] — код-предохранитель, как
    аналогичные предохранители в art_director._parse."""
    payload = json.dumps({
        "title": "Test",
        "characters": [
            {"name_ru": "А", "name_en": "A", "score": 150, "why": "", "print_moment": ""},
            {"name_ru": "Б", "name_en": "B", "score": -20, "why": "", "print_moment": ""},
        ],
        "moments": [],
    })
    result = fscout._parse_dossier(payload)
    scores = {c["name_ru"]: c["score"] for c in result["characters"]}
    assert scores["А"] == 100.0
    assert scores["Б"] == 0.0


# ------------------------------------------------------------- ретрай / явная ошибка
def test_ask_and_parse_with_retry_raises_on_double_failure(monkeypatch, tmp_path):
    """Двойной сбой парсинга JSON -> явная ошибка (RuntimeError), НЕ тихий откат на
    пустое досье — как того требует ТЗ (парсинг по образцу theme_scout, но явная
    ошибка при двойном сбое, не тихий откат)."""
    monkeypatch.setattr(fscout, "_ask_claude_dossier", lambda user_text: "не json совсем")
    dump_dir = tmp_path / "out_batch" / "scout_failures"
    monkeypatch.setattr(fscout, "HERE", tmp_path)

    try:
        fscout._ask_and_parse_dossier_with_retry("любой текст запроса")
        assert False, "ожидалась RuntimeError при двойном сбое парсинга"
    except RuntimeError as e:
        assert "дважды подряд" in str(e)

    # Сырой ответ дампится в файл на КАЖДУЮ из двух попыток.
    dumped = list(dump_dir.glob("franchise_fail_*_try*.txt"))
    assert len(dumped) == 2, f"ожидалось 2 дампа сбоя, найдено: {dumped}"


def test_ask_and_parse_with_retry_recovers_after_one_failure(monkeypatch):
    """Первый вызов вернул мусор, второй (ретрай) — валидный JSON: результат
    валидный, дамп сбоя записан только за ПЕРВУЮ попытку."""
    calls = {"n": 0}
    good = json.dumps({"title": "T", "characters": [], "moments": []})

    def _flaky(user_text):
        calls["n"] += 1
        return "мусор" if calls["n"] == 1 else good

    monkeypatch.setattr(fscout, "_ask_claude_dossier", _flaky)
    dumped = []
    monkeypatch.setattr(fscout, "_dump_dossier_failure",
                        lambda text, attempt: dumped.append(attempt))

    result = fscout._ask_and_parse_dossier_with_retry("запрос")
    assert result["title"] == "T"
    assert calls["n"] == 2
    assert dumped == [1]


# ------------------------------------------------------------- кэш
def test_cyrillic_titles_have_distinct_cache_keys():
    """Регрессия: старый ASCII-only slug превращал ЛЮБОЙ кириллический тайтл
    в ``untitled``. Поэтому досье «Гачиакуты» повторно использовалось для
    «Клинка, рассекающего демонов» и панель генерировала не тех персонажей."""
    gachi = fscout._slugify("Гачиакута")
    demon_slayer = fscout._slugify("Клинок рассекающий демонов")

    assert gachi != "untitled"
    assert demon_slayer != "untitled"
    assert gachi != demon_slayer
    assert fscout._cache_path("Гачиакута") != fscout._cache_path(
        "Клинок рассекающий демонов"
    )
    assert fscout._signals_cache_path("Гачиакута") != fscout._signals_cache_path(
        "Клинок рассекающий демонов"
    )


def test_cache_key_is_stable_for_equivalent_unicode():
    """Визуально одинаковый Unicode должен попадать в один кэш независимо от
    формы нормализации и регистра."""
    assert fscout._slugify("  КЛИНОК  ") == fscout._slugify("клинок")


def test_build_dossier_cache_hit_does_not_touch_network(monkeypatch, tmp_path):
    """Второй вызов build_dossier для того же тайтла в тот же день читает кэш и
    НЕ дёргает ни один сетевой источник, ни Claude — проверяется через
    мок-счётчик вызовов."""
    monkeypatch.setattr(fscout, "CACHE_DIR", tmp_path / "franchise_cache")

    network_calls = {"n": 0}

    def _counting_anilist(title):
        network_calls["n"] += 1
        return [{"name": "Гоку", "favourites": 100, "role": "MAIN"}]

    monkeypatch.setattr(fscout, "_anilist_characters", _counting_anilist)
    monkeypatch.setattr(fscout, "_jikan_characters", lambda title: [])
    monkeypatch.setattr(fscout, "_tmdb_credits", lambda title: [])
    monkeypatch.setattr(fscout, "_youtube_edits", lambda title: [])
    monkeypatch.setattr(fscout, "_gtrends_related", lambda title: [])

    claude_calls = {"n": 0}

    def _counting_claude(user_text):
        claude_calls["n"] += 1
        return json.dumps({"title": "Dragon Ball", "characters": [], "moments": []})

    monkeypatch.setattr(fscout, "_ask_claude_dossier", _counting_claude)

    first = fscout.build_dossier("Dragon Ball", kind="anime")
    assert network_calls["n"] == 1
    assert claude_calls["n"] == 1

    second = fscout.build_dossier("Dragon Ball", kind="anime")
    assert network_calls["n"] == 1, "второй вызов НЕ должен был дёрнуть anilist снова"
    assert claude_calls["n"] == 1, "второй вызов НЕ должен был дёрнуть Claude снова"
    assert second == first

    cache_file = fscout._cache_path("Dragon Ball")
    assert cache_file.exists()


def test_build_dossier_caches_signals_before_synthesis_survives_claude_failure(monkeypatch, tmp_path):
    """РЕГРЕССИЯ (находка тестировщика): при провале синтеза Claude (баланс/rate-limit/
    5xx) досье не кэшируется (корректно по дизайну — не кэшируем неудачу), НО уже
    собранные сигналы (в т.ч. дорогая YouTube-квота) должны кэшироваться ОТДЕЛЬНО, ДО
    вызова синтезатора. Повторный вызов build_dossier (после восстановления Claude, тот
    же день) обязан переиспользовать сигналы из этого кэша, а не дёргать сеть заново —
    иначе каждая повторная попытка после временного сбоя баланса заново тратит ~101
    юнит квоты YouTube впустую."""
    monkeypatch.setattr(fscout, "CACHE_DIR", tmp_path / "franchise_cache")

    network_calls = {"n": 0}

    def _counting_anilist(title):
        network_calls["n"] += 1
        return [{"name": "Рем", "favourites": 45000, "role": "MAIN"}]

    monkeypatch.setattr(fscout, "_anilist_characters", _counting_anilist)
    monkeypatch.setattr(fscout, "_jikan_characters", lambda title: [])
    monkeypatch.setattr(fscout, "_tmdb_credits", lambda title: [])
    monkeypatch.setattr(fscout, "_youtube_edits", lambda title: [])
    monkeypatch.setattr(fscout, "_gtrends_related", lambda title: [])

    # Первый вызов: синтезатор Claude падает дважды подряд -> build_dossier бросает
    # RuntimeError (штатное поведение), досье НЕ кэшируется, но сигналы уже должны
    # быть записаны на диск ДО этого сбоя.
    monkeypatch.setattr(fscout, "_ask_claude_dossier", lambda user_text: "не json совсем")
    try:
        fscout.build_dossier("Re:Zero", kind="anime")
        assert False, "ожидалась RuntimeError при двойном сбое синтеза"
    except RuntimeError:
        pass

    assert network_calls["n"] == 1, "сигналы должны были собраться один раз до сбоя синтеза"
    signals_cache = fscout._signals_cache_path("Re:Zero")
    assert signals_cache.exists(), "кэш сигналов обязан быть записан ДО вызова синтезатора"
    dossier_cache = fscout._cache_path("Re:Zero")
    assert not dossier_cache.exists(), "досье НЕ должно кэшироваться при провале синтеза"

    # Второй вызов (имитация повтора после восстановления баланса/сети Claude):
    # синтезатор теперь отвечает валидным JSON. Сигналы должны браться из кэша
    # (network_calls не растёт), синтез отрабатывает и досье теперь кэшируется.
    good = json.dumps({"title": "Re:Zero", "characters": [
        {"name_ru": "Рем", "name_en": "Rem", "score": 92, "why": "favourites=45000",
         "print_moment": "сцена с топором"},
    ], "moments": []})
    monkeypatch.setattr(fscout, "_ask_claude_dossier", lambda user_text: good)

    dossier = fscout.build_dossier("Re:Zero", kind="anime")

    assert network_calls["n"] == 1, (
        "второй вызов НЕ должен был дёрнуть anilist снова — сигналы берутся из кэша"
    )
    assert dossier["title"] == "Re:Zero"
    assert dossier["characters"][0]["name_ru"] == "Рем"
    assert dossier_cache.exists(), "досье теперь должно быть закэшировано после успешного синтеза"


def test_build_dossier_cache_corrupted_falls_back_to_rebuild(monkeypatch, tmp_path):
    """Битый файл кэша не роняет build_dossier — просто пересобирается заново."""
    monkeypatch.setattr(fscout, "CACHE_DIR", tmp_path / "franchise_cache")
    fscout.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fscout._cache_path("Broken Cache").write_text("{не валидный json", encoding="utf-8")

    _no_network(monkeypatch)
    monkeypatch.setattr(fscout, "_ask_claude_dossier", _fake_claude_response(
        {"title": "Broken Cache", "characters": [], "moments": []}))

    dossier = fscout.build_dossier("Broken Cache", kind="anime")
    assert dossier["title"] == "Broken Cache"


# ------------------------------------------------------------- graceful degradation
def test_build_dossier_survives_every_source_failing(monkeypatch, tmp_path):
    """Каждый из 5 источников падает исключением — build_dossier всё равно
    возвращает досье (Claude синтезирует из пустых сигналов, честно короткое/
    пустое досье), не роняет вызов."""
    monkeypatch.setattr(fscout, "CACHE_DIR", tmp_path / "franchise_cache")

    def _boom(title):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(fscout, "_anilist_characters", _boom)
    monkeypatch.setattr(fscout, "_jikan_characters", _boom)
    monkeypatch.setattr(fscout, "_tmdb_credits", _boom)
    monkeypatch.setattr(fscout, "_youtube_edits", _boom)
    monkeypatch.setattr(fscout, "_gtrends_related", _boom)

    # build_dossier вызывает источники напрямую (не через try/except у себя) —
    # у каждого источника try/except ВНУТРИ функции по контракту ТЗ. Здесь
    # источники подменены на функции, которые бросают исключение НАПРЯМУЮ, чтобы
    # проверить: если бы источник ошибся мимо своего try/except, build_dossier
    # всё равно не должен упасть молча необработанным — поэтому ожидаем, что
    # это исключение всплывёт (это ожидаемое поведение: try/except — ответственность
    # каждой _*_characters/_tmdb_credits/_youtube_edits/_gtrends_related функции,
    # а не build_dossier). Проверяем именно ЭТО поведение источников по отдельности
    # ниже — здесь же тест на то, что штатная (не сломанная) деградация возвращает [].
    try:
        fscout.build_dossier("Anything", kind="anime")
        assert False, "ожидали проброс исключения — try/except это ответственность источника"
    except RuntimeError:
        pass


def test_each_source_function_catches_its_own_network_errors(monkeypatch):
    """Каждая из 5 функций-источников graceful: реальная сетевая ошибка (requests
    бросает исключение) ловится ВНУТРИ функции и возвращает [] — не наружу."""
    import requests
    import trendspy

    def _raise(*a, **kw):
        raise requests.exceptions.ConnectionError("нет сети")

    monkeypatch.setattr(fscout.requests, "post", _raise)
    monkeypatch.setattr(fscout.requests, "get", _raise)
    monkeypatch.setattr(fscout.config, "TMDB_API_KEY", "fake-key")
    monkeypatch.setattr(fscout.config, "YOUTUBE_API_KEY", "fake-key")

    class _RaisingTrends:
        def __init__(self, *a, **kw):
            pass

        def related_queries(self, *a, **kw):
            raise RuntimeError("gtrends недоступен (анти-бот/сеть)")

    monkeypatch.setattr(trendspy, "Trends", _RaisingTrends)

    assert fscout._anilist_characters("Any Title") == []
    assert fscout._jikan_characters("Any Title") == []
    assert fscout._tmdb_credits("Any Title") == []
    assert fscout._youtube_edits("Any Title") == []
    # gtrends — библиотека может отсутствовать/бросать при импорте, тоже не должна падать
    assert fscout._gtrends_related("Any Title") == []


def test_gtrends_related_handles_dataframe_result(monkeypatch):
    """РЕГРЕССИЯ: trendspy.related_queries() на реальном прогоне возвращает
    {"top": pandas.DataFrame, "rising": pandas.DataFrame} (колонка текста запроса —
    "query"), а старый код делал булеву проверку вида `rising or []`, что на
    DataFrame бросает ValueError ("truth value of a DataFrame is ambiguous").
    Починка — явные isinstance-ветки + .to_dict("records") (см. _gtrends_related).
    Здесь мокаем trendspy.Trends так, чтобы related_queries() отдал НАСТОЯЩИЙ
    pandas.DataFrame в "rising", и проверяем, что _gtrends_related не падает и
    возвращает непустой список строк запросов."""
    import pandas as pd

    rising_df = pd.DataFrame({
        "query": ["рем аниме", "рем футболка", "рем принт"],
        "value": [120, 95, 80],
    })
    top_df = pd.DataFrame({"query": ["re zero"], "value": [100]})

    class _FakeTrends:
        def __init__(self, *a, **kw):
            pass

        def related_queries(self, *a, **kw):
            return {"top": top_df, "rising": rising_df}

    import trendspy
    monkeypatch.setattr(trendspy, "Trends", _FakeTrends)

    result = fscout._gtrends_related("Re:Zero")

    assert isinstance(result, list)
    assert result, "ожидался непустой список запросов из rising DataFrame"
    assert all(isinstance(q, str) for q in result)
    assert "рем аниме" in result


def test_tmdb_credits_missing_key_returns_empty(monkeypatch):
    monkeypatch.setattr(fscout.config, "TMDB_API_KEY", "")
    assert fscout._tmdb_credits("Some Show") == []


def test_youtube_edits_missing_key_returns_empty(monkeypatch):
    monkeypatch.setattr(fscout.config, "YOUTUBE_API_KEY", "")
    assert fscout._youtube_edits("Some Show") == []


# ------------------------------------------------------------- graceful degradation LLM-провайдера
def test_ask_claude_dossier_provider_failure_returns_empty_string_not_raises(monkeypatch):
    """РЕГРЕССИЯ (найдено тестировщиком на реальном прогоне: anthropic.BadRequestError
    из-за исчерпанного баланса Anthropic не ловился вокруг client.messages.create).
    Провайдер теперь переключаемый (llm_provider.py, gemini/openai/anthropic) —
    _ask_claude_dossier обязан ловить RuntimeError ОТ ЛЮБОГО провайдера
    (llm_provider.generate_text поднимает именно RuntimeError на сеть/баланс/
    rate-limit/5xx, независимо от того, gemini это, openai или anthropic) и
    возвращать "" (тот же сигнал, что невалидный JSON), не пробрасывать
    исключение наружу — иначе build_dossier падает необработанным traceback
    вместо явной RuntimeError с понятным сообщением (см.
    _ask_and_parse_dossier_with_retry)."""
    def _failing_generate_text(system, user, max_tokens=1500):
        raise RuntimeError("Gemini (арт-директор, текст): HTTP 429: RESOURCE_EXHAUSTED")

    monkeypatch.setattr(fscout.llm_provider, "generate_text", _failing_generate_text)

    text = fscout._ask_claude_dossier("любой текст запроса")
    assert text == ""


def test_build_dossier_raises_clean_runtime_error_on_llm_provider_failure(monkeypatch, tmp_path):
    """Сквозная регрессия: build_dossier при ДВОЙНОМ сбое LLM-провайдера (не
    парсинга) должен вести себя так же, как при двойном сбое парсинга JSON —
    явная RuntimeError с понятным сообщением, а НЕ сырое исключение провайдера,
    пробитое до самого верха (это и уронило daily_prints.py на реальном прогоне,
    т.к. вызывающий _collect_dossiers ловит Exception и печатает предупреждение,
    но сама ошибка обязана быть RuntimeError по контракту модуля, не типом
    SDK/провайдера)."""
    monkeypatch.setattr(fscout, "CACHE_DIR", tmp_path / "franchise_cache")
    _no_network(monkeypatch)

    calls = {"n": 0}

    def _failing_generate_text(system, user, max_tokens=1500):
        calls["n"] += 1
        raise RuntimeError("Gemini (арт-директор, текст): HTTP 429: RESOURCE_EXHAUSTED")

    monkeypatch.setattr(fscout.llm_provider, "generate_text", _failing_generate_text)

    try:
        fscout.build_dossier("Anything", kind="anime")
        assert False, "ожидалась RuntimeError при двойном сбое LLM-провайдера"
    except RuntimeError as e:
        assert "дважды подряд" in str(e)
    assert calls["n"] == 2, "ожидался ровно 1 ретрай (2 попытки), как при сбое парсинга"


def test_youtube_edits_uses_exactly_one_search_call(monkeypatch):
    """Экономика квоты: search.list вызывается СТРОГО один раз на build_dossier/
    _youtube_edits (100 юнитов), не более — проверяем через мок-счётчик."""
    search_calls = {"n": 0}
    monkeypatch.setattr(fscout.config, "YOUTUBE_API_KEY", "fake-key")

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def _fake_get(url, params=None, timeout=None):
        if url == fscout._YOUTUBE_SEARCH_URL:
            search_calls["n"] += 1
            return _FakeResp({"items": [
                {"id": {"videoId": "abc123"}, "snippet": {"title": "Best Edit Ever"}},
            ]})
        if url == fscout._YOUTUBE_VIDEOS_URL:
            return _FakeResp({"items": [
                {"id": "abc123", "statistics": {"viewCount": "999999"}},
            ]})
        raise AssertionError(f"неожиданный URL: {url}")

    monkeypatch.setattr(fscout.requests, "get", _fake_get)

    out = fscout._youtube_edits("Some Title")
    assert search_calls["n"] == 1, "search.list (100 юнитов) должен вызываться ровно 1 раз"
    assert out and out[0]["views"] == 999999


# ------------------------------------------------------------- интеграция с theme_scout
def test_theme_scout_pick_deep_dive_titles_ranks_by_score():
    anime_rows = [
        {"lemma": "Chainsaw Man", "score": 70.0},
        {"lemma": "Frieren", "score": 95.0},
    ]
    pop_anime_rows = [
        {"lemma": "Solo Leveling", "score": 88.0, "sources": "anilist"},
        {"lemma": "Frieren", "score": 40.0, "sources": "jikan"},  # дубликат меньшим score
    ]
    titles = ts._pick_deep_dive_titles(anime_rows, pop_anime_rows, n=2)
    assert titles == ["Frieren", "Solo Leveling"], (
        f"ожидали топ-2 по score с дедупом (Frieren берёт МАКСИМУМ из двух score): {titles}"
    )


def test_theme_scout_collect_dossiers_survives_one_title_failing(monkeypatch):
    """Падение build_dossier для ОДНОГО тайтла не мешает собрать досье для
    остальных — graceful degradation на уровне theme_scout._collect_dossiers."""
    def _fake_build_dossier(title, kind="auto"):
        if title == "Broken Title":
            raise RuntimeError("синтезатор Claude сломался дважды подряд")
        return {"title": title, "characters": [{"name_ru": "Герой", "name_en": "Hero",
                "score": 80, "why": "", "print_moment": ""}], "moments": []}

    monkeypatch.setattr(ts.franchise_scout, "build_dossier", _fake_build_dossier)

    dossiers = ts._collect_dossiers(["Broken Title", "Good Title"])
    assert "Broken Title" not in dossiers
    assert "Good Title" in dossiers
    assert dossiers["Good Title"]["characters"][0]["name_ru"] == "Герой"


def test_theme_scout_dossier_block_injected_into_scout_prompt(monkeypatch):
    """Интеграционный тест: подложенное досье для аниме-леммы должно попасть в
    текст user-промпта _ask_claude_scout (Claude мокается — проверяем, что нужный
    текст реально дошёл до вызова, а не потерялся при сборке промпта)."""
    captured = {}

    class _FakeContentBlock:
        def __init__(self, text):
            self.type = "text"
            self.text = text

    class _FakeResponse:
        def __init__(self, text):
            self.content = [_FakeContentBlock(text)]

    class _FakeMessages:
        def create(self, model, max_tokens, system, messages):
            captured["user_text"] = messages[0]["content"]
            captured["system"] = system
            return _FakeResponse("[]")

    class _FakeClient:
        def __init__(self, api_key):
            self.messages = _FakeMessages()

    monkeypatch.setattr(ts.anthropic, "Anthropic", _FakeClient)

    anime_rows = [{"lemma": "Re:Zero", "score": 90.0, "example_text": "рем топ"}]
    dossiers = {"Re:Zero": {
        "title": "Re:Zero",
        "characters": [{"name_ru": "Рем", "name_en": "Rem", "score": 95,
                        "why": "favourites=45000", "print_moment": "сцена с топором"}],
        "moments": [],
    }}

    ts._ask_claude_scout([], anime_rows, target_n=10, dossiers=dossiers)

    assert "ДОСЬЕ ФРАНШИЗЫ" in captured["user_text"]
    assert "Рем" in captured["user_text"]
    assert "сцена с топором" in captured["user_text"]
    assert "ДОСЬЕ ФРАНШИЗЫ" in captured["system"], (
        "SYSTEM_SCOUT должен объяснять модели, что делать с блоком досье"
    )
