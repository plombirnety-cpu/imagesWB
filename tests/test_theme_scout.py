# -*- coding: utf-8 -*-
"""Тесты theme_scout.py — только офлайн-логика (сборка/чередование заданий),
БЕЗ сети и БЕЗ реальных вызовов Claude (_ask_and_parse_with_retry подменяется
через monkeypatch на детерминированные заглушки).

Запуск:
    cd print-factory-nb && python -m pytest tests/test_theme_scout.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import theme_scout as ts  # noqa: E402


# ------------------------------------------------------------- _interleave_by_source
def test_interleave_by_source_alternates_groups_in_order_of_first_appearance():
    """Round-robin по группам source, в порядке их ПЕРВОГО появления во входном
    списке — trend/pop:anilist/pop:jikan должны чередоваться 1-к-1-к-1, а не идти
    блоками (один большой блок trend, потом маленькие pop-блоки)."""
    tasks = (
        [{"theme": f"trend-{i}", "source": "trend"} for i in range(5)]
        + [{"theme": f"anilist-{i}", "source": "trend:pop:anilist"} for i in range(2)]
        + [{"theme": f"jikan-{i}", "source": "trend:pop:jikan"} for i in range(2)]
    )
    out = ts._interleave_by_source(tasks)

    assert len(out) == len(tasks)
    # Первые 3 задания среза (round-robin) должны покрывать ВСЕ 3 источника,
    # не только "trend" (это и есть репро находки тестировщика на --limit 10).
    first_three_sources = {t["source"] for t in out[:3]}
    assert first_three_sources == {"trend", "trend:pop:anilist", "trend:pop:jikan"}


def test_interleave_by_source_small_slice_contains_every_source():
    """Ключевая приёмка находки тестировщика: срез МАЛОЙ длины (аналог --limit 10)
    из плана, где один источник ("trend") доминирует по объёму (десятки заданий),
    должен всё равно содержать представителей pop-источников, а не 0."""
    tasks = (
        [{"theme": f"trend-{i}", "source": "trend"} for i in range(60)]
        + [{"theme": f"anilist-{i}", "source": "trend:pop:anilist"} for i in range(20)]
        + [{"theme": f"jikan-{i}", "source": "trend:pop:jikan"} for i in range(35)]
    )
    out = ts._interleave_by_source(tasks)
    small_slice = out[:10]

    sources_in_slice = {t["source"] for t in small_slice}
    assert "trend:pop:anilist" in sources_in_slice, (
        f"pop:anilist отсутствует в первых 10 заданиях после чередования: {small_slice}"
    )
    assert "trend:pop:jikan" in sources_in_slice, (
        f"pop:jikan отсутствует в первых 10 заданиях после чередования: {small_slice}"
    )
    assert "trend" in sources_in_slice


def test_interleave_by_source_preserves_relative_order_within_group():
    """Порядок заданий ВНУТРИ одной группы source не должен меняться —
    чередуются только группы, не переставляются задания друг с другом."""
    tasks = [
        {"theme": "trend-A", "source": "trend"},
        {"theme": "pop-A", "source": "trend:pop:anilist"},
        {"theme": "trend-B", "source": "trend"},
        {"theme": "pop-B", "source": "trend:pop:anilist"},
    ]
    out = ts._interleave_by_source(tasks)
    trend_order = [t["theme"] for t in out if t["source"] == "trend"]
    pop_order = [t["theme"] for t in out if t["source"] == "trend:pop:anilist"]
    assert trend_order == ["trend-A", "trend-B"]
    assert pop_order == ["pop-A", "pop-B"]


def test_interleave_by_source_empty_list_returns_empty_list():
    assert ts._interleave_by_source([]) == []


def test_interleave_by_source_single_group_unchanged():
    tasks = [{"theme": f"t{i}", "source": "evergreen"} for i in range(5)]
    out = ts._interleave_by_source(tasks)
    assert [t["theme"] for t in out] == [t["theme"] for t in tasks]


def test_interleave_by_source_total_count_preserved_with_uneven_group_sizes():
    """Сумма заданий после чередования должна остаться той же, даже если группы
    очень разного размера (реалистичный случай: trend=111, pop:anilist=20,
    pop:jikan=35 — числа из реального контрольного прогона тестировщика)."""
    tasks = (
        [{"theme": f"trend-{i}", "source": "trend"} for i in range(111)]
        + [{"theme": f"anilist-{i}", "source": "trend:pop:anilist"} for i in range(20)]
        + [{"theme": f"jikan-{i}", "source": "trend:pop:jikan"} for i in range(35)]
    )
    out = ts._interleave_by_source(tasks)
    assert len(out) == 166
    assert sum(1 for t in out if t["source"] == "trend") == 111
    assert sum(1 for t in out if t["source"] == "trend:pop:anilist") == 20
    assert sum(1 for t in out if t["source"] == "trend:pop:jikan") == 35


# ------------------------------------------------------------- build_daily_plan (моки)
def test_build_daily_plan_small_target_includes_pop_sources(monkeypatch):
    """Приёмочный сценарий из шага 4 задания (аналог --dry-run --limit 10): даже
    при небольшом target_n итоговый план должен содержать trend:pop:* задания,
    если тематизатор их вообще вернул — не только "trend". Сеть/Claude/CSV
    полностью замоканы (это чистый юнит-тест сборки, не сетевой прогон)."""
    monkeypatch.setattr(ts, "read_trend_rows", lambda: (
        [{"lemma": "media-topic", "score": 90.0, "example_text": ""}],
        [{"lemma": "anime-topic", "score": 80.0, "example_text": ""}],
        [{"lemma": "anilist-topic", "score": 95.0, "example_text": "", "sources": "anilist"}],
        [],
    ))

    def _fake_ask_and_parse(media_rows, anime_rows, target_n, pop_rows=None, pop_is_anime=False):
        if pop_rows:
            # Симулируем реальность: тематизатор возвращает МЕНЬШЕ заданий по
            # pop-блоку, чем по общему блоку media+anime (типичный дисбаланс,
            # который и приводил к вытеснению pop при --limit 10).
            return [{"theme": f"anilist-task-{i}", "format": "diecut"} for i in range(3)]
        # Общий блок media+anime возвращает МНОГО заданий (десятки, как в реальном
        # прогоне тестировщика) — раньше это съедало весь маленький --target/--limit.
        return [{"theme": f"trend-task-{i}", "format": "diecut"} for i in range(40)]

    monkeypatch.setattr(ts, "_ask_and_parse_with_retry", _fake_ask_and_parse)
    monkeypatch.setattr(ts, "_read_evergreen", lambda: [])

    plan = ts.build_daily_plan(target_n=10)

    assert len(plan) == 10
    sources_in_plan = {t["source"] for t in plan}
    assert "trend:pop:anilist" in sources_in_plan, (
        f"trend:pop:anilist отсутствует в плане из {len(plan)} заданий: {plan}"
    )
    assert "trend" in sources_in_plan
