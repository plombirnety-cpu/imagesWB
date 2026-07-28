# -*- coding: utf-8 -*-
from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime, timedelta, timezone

import pytest
import requests

import radar_collector
import trend_radar


class _Response:
    def __init__(self, payload=None, *, content=b"", text=""):
        self._payload = payload
        self.content = content
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _InlineExecutor:
    def submit(self, function, *args):
        future = Future()
        function(*args)
        future.set_result(None)
        return future


class _HoldingExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, function, *args):
        self.calls.append((function, args))
        return Future()


class _SeedSource:
    def __init__(self, seeds):
        self.seeds = seeds

    def collect(self):
        return list(self.seeds)


class _FakeTikTok:
    configured = True

    def discover(self, keyword, limit):
        return [
            {
                "post_id": "123",
                "profile_username": "creator",
                "description": f"{keyword} новый звук #сованаскакалке #fyp",
                "create_time": 1785056400,
                "play_count": 150_000,
                "digg_count": 12_000,
                "share_count": 4_200,
                "comment_count": 900,
            }
        ][:limit]

    def comments(self, post_url):
        return [
            {"commenter_user_name": "one", "comment_text": "воздухан нужен"},
            {"commenter_user_name": "two", "comment_text": "где купить воздухан"},
            {"commenter_user_name": "three", "comment_text": "воздухан опять"},
        ]

    post_url = staticmethod(radar_collector.BrightDataClient.post_url)
    published_at = staticmethod(radar_collector.BrightDataClient.published_at)


class _TrackingTikTok:
    configured = True

    def __init__(self):
        self.comment_calls = []

    def discover(self, keyword, limit):
        slug = radar_collector.canonical_key(keyword).replace(" ", "-")
        return [
            {
                "post_id": f"{slug}-{index}",
                "profile_username": f"creator{index}",
                "description": f"{keyword} #{slug}",
                "create_time": 1785056400,
                "play_count": 10_000 + index,
                "digg_count": 1_000,
                "share_count": 100,
                "comment_count": 20,
            }
            for index in range(2)
        ][:limit]

    def comments(self, post_url):
        self.comment_calls.append(post_url)
        return [
            {
                "commenter_user_name": "viewer",
                "comment_text": "новое упоминание",
            }
        ]

    post_url = staticmethod(radar_collector.BrightDataClient.post_url)
    published_at = staticmethod(radar_collector.BrightDataClient.published_at)


class _LineageTikTok:
    configured = True

    def discover(self, keyword, limit):
        return [
            {
                "post_id": str(700 + index),
                "profile_username": f"creator{index}",
                "description": "#thewalkingdead новый вирусный монтаж",
                "create_time": 1785056400 + index,
                "play_count": 90_000 + index,
                "digg_count": 9_000,
                "share_count": 1_200,
                "comment_count": 400,
            }
            for index in range(2)
        ][:limit]

    def comments(self, _post_url):
        return []

    post_url = staticmethod(radar_collector.BrightDataClient.post_url)
    published_at = staticmethod(radar_collector.BrightDataClient.published_at)


def test_tiktok_candidates_expand_search_graph_without_generic_tags():
    terms = radar_collector.confirmed_tiktok_candidates(
        [
            {
                "post_id": "1",
                "profile_username": "one",
                "description": "#fyp #сованаскакалке #воздухан #машины",
                "hashtags": [{"name": "черемша"}, "viral"],
            },
            {
                "post_id": "2",
                "profile_username": "two",
                "description": "#сованаскакалке #черемша #машины",
            },
            {
                "post_id": "3",
                "profile_username": "one",
                "description": "#сованаскакалке",
            },
        ],
        "Воздухан",
    )

    assert terms == ["сованаскакалке", "черемша"]
    assert "машины" not in terms


def test_comment_candidate_needs_two_observations_and_not_generic_word():
    base = {
        "term": "сованаскакалке",
        "score": 80,
        "unique_authors": 4,
        "observation_count": 1,
    }

    assert radar_collector.comment_candidate_is_confirmed(base, "другая тема") is False
    assert radar_collector.comment_candidate_is_confirmed(
        {**base, "observation_count": 2},
        "другая тема",
    ) is True
    assert radar_collector.comment_candidate_is_confirmed(
        {**base, "term": "машины", "observation_count": 2},
        "другая тема",
    ) is False


def test_confirmed_tiktok_seed_has_priority_over_broad_google(tmp_path):
    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    now = datetime.now(timezone.utc)
    store.upsert_seed("широкий запрос", "google_trends", now=now)
    store.upsert_seed("сованаскакалке", "tiktok_hashtag", now=now)

    assert store.discovery_terms(1, now=now + timedelta(minutes=1)) == [
        "сованаскакалке",
    ]


def test_google_trends_rss_becomes_search_seeds():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel><item><title>Новая фраза</title></item>
    <item><title>Сова на скакалке</title></item></channel></rss>""".encode()
    source = radar_collector.GoogleTrendsSource(
        geo="RU", get=lambda *args, **kwargs: _Response(content=xml),
    )

    seeds = source.collect()

    assert [seed.term for seed in seeds] == ["Новая фраза", "Сова на скакалке"]
    assert all(seed.source_type == "google_trends" for seed in seeds)


def test_google_trends_rss_preserves_volume_and_start_time():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss xmlns:ht="https://trends.google.com/trending/rss"><channel><item>
      <title>Резкий новый мем</title>
      <ht:approx_traffic>5,000+</ht:approx_traffic>
      <pubDate>Mon, 27 Jul 2026 12:40:00 -0700</pubDate>
    </item></channel></rss>""".encode()
    source = radar_collector.GoogleTrendsSource(
        geo="RU", get=lambda *args, **kwargs: _Response(content=xml),
    )

    seed = source.collect()[0]

    assert seed.term == "Резкий новый мем"
    assert seed.search_volume == 5_000
    assert seed.published_at == "2026-07-27T19:40:00Z"


def test_telegram_keeps_explicit_hashtags_without_generic_word_noise():
    page = """
    <div class="tgme_widget_message" data-post="memes/1">
      <div class="tgme_widget_message_text">Сегодня появился #Воздухан</div>
    </div>
    <div class="tgme_widget_message" data-post="memes/2">
      <div class="tgme_widget_message_text">Всем срочно нужен воздухан</div>
    </div>
    """
    source = radar_collector.TelegramPublicSource(
        ["memes"], get=lambda *args, **kwargs: _Response(text=page),
    )

    seeds = source.collect()

    assert any(seed.term.casefold() == "воздухан" for seed in seeds)
    assert not any(seed.term.casefold() == "появился" for seed in seeds)
    assert not any(seed.term.casefold() == "нужен" for seed in seeds)


def test_memsearch_first_fragment_becomes_curated_meme_phrase():
    page = """
    <div class="tgme_widget_message" data-post="memsearch/1">
      <div class="tgme_widget_message_text">Сова на скакалке, гадания, мем, тикток 👤 user</div>
    </div>
    <div class="tgme_widget_message" data-post="memsearch/2">
      <div class="tgme_widget_message_text">Друзья! В приюте пополнение, котята</div>
    </div>
    """
    source = radar_collector.TelegramPublicSource(
        ["memsearch"], get=lambda *args, **kwargs: _Response(text=page),
    )

    seeds = source.collect()

    assert [seed.term for seed in seeds] == ["Сова на скакалке"]
    assert seeds[0].source_type == "telegram_memes"
    assert seeds[0].source_url == "https://t.me/memsearch/1"


def test_bright_data_discovery_request_and_tiktok_url_shape():
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Response(
            [{"post_id": "9", "profile_username": "hero", "description": "мем"}]
        )

    client = radar_collector.BrightDataClient("secret", request=request)
    records = client.discover("воздухан", 5)

    assert calls[0][0] == "POST"
    assert calls[0][2]["params"]["dataset_id"] == "gd_lu702nij2f790tmv9h"
    assert calls[0][2]["json"]["input"][0]["search_keyword"] == "воздухан"
    assert client.post_url(records[0]) == "https://www.tiktok.com/@hero/video/9"
    assert calls[0][2]["headers"]["Authorization"] == "Bearer secret"


def test_bright_data_sync_scrape_waits_full_contract_without_short_retries():
    timeouts = []

    def slow_request(method, url, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise requests.ReadTimeout("provider still preparing the synchronous result")

    client = radar_collector.BrightDataClient(
        "secret",
        timeout=30,
        request=slow_request,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(requests.ReadTimeout):
        client.discover("движуха", 5)

    assert timeouts == [75]


def test_collector_stores_seeds_and_tiktok_metrics_without_auto_comments(tmp_path):
    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    collector = radar_collector.RadarCollector(
        store,
        radar_collector.CollectorConfig(
            discovery_terms_per_run=1,
            posts_per_term=5,
            comments_posts_per_run=1,
        ),
        google=_SeedSource(
            [
                radar_collector.SeedTerm(
                    "Воздухан",
                    "google_trends",
                    "https://example.test",
                    search_volume=5_000,
                    published_at=datetime.now(timezone.utc).isoformat(),
                )
            ]
        ),
        telegram=_SeedSource([]),
        tiktok=_FakeTikTok(),
        executor=_InlineExecutor(),
    )

    queued = collector.queue_run("test")

    assert queued["queued"] is True
    run = store.collector_run(queued["run_id"])
    assert run["status"] == "succeeded"
    assert run["seeds_found"] == 1
    assert run["signals_created"] == 1
    assert run["phase"] == "completed"
    assert run["steps_total"] == 1
    assert run["steps_done"] == 1
    assert run["heartbeat_at"]
    trend = store.list_trends()[0]
    assert trend["display_name"] == "Воздухан"
    assert trend["observations"][0]["views"] == 150_000
    terms = {item["term"] for item in trend["emerging_terms"]}
    assert terms == set()


def test_collector_links_duplicate_tiktok_videos_back_to_google_origin(tmp_path):
    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    published = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
    child_term = "thewalkingdead"
    for index in range(2):
        store.ingest_signal(
            trend_radar.SignalInput(
                term=child_term,
                source_type="tiktok",
                source_url=(
                    f"https://www.tiktok.com/@creator{index}/video/{700 + index}"
                ),
                author=f"creator{index}",
                published_at=published + timedelta(seconds=index),
                views=90_000 + index,
                shares=1_200,
            ),
            now=published + timedelta(hours=1),
        )

    collector = radar_collector.RadarCollector(
        store,
        radar_collector.CollectorConfig(
            discovery_terms_per_run=1,
            posts_per_term=2,
            comments_posts_per_run=0,
        ),
        google=_SeedSource(
            [
                radar_collector.SeedTerm(
                    "Ходячие мертвецы",
                    "google_trends",
                    "https://trends.google.com/trending?geo=RU",
                    search_volume=5_000,
                    published_at=published.isoformat(),
                )
            ]
        ),
        telegram=_SeedSource([]),
        tiktok=_LineageTikTok(),
        executor=_InlineExecutor(),
    )

    queued = collector.queue_run("test")

    assert store.collector_run(queued["run_id"])["status"] == "succeeded"
    child = next(
        item for item in store.list_trends()
        if item["display_name"] == child_term
    )
    assert child["google_origin_key"] == trend_radar.canonical_key(
        "Ходячие мертвецы"
    )
    assert child["opportunity"]["google"]["spike"] is True
    assert child["opportunity"]["tiktok"]["confirmed"] is True
    assert child["opportunity"]["qualified"] is True
    assert store.google_origin_for_seed("thewalkingdead") == trend_radar.canonical_key(
        "Ходячие мертвецы"
    )


def test_full_discovery_never_fetches_comments(tmp_path):
    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    tiktok = _TrackingTikTok()
    collector = radar_collector.RadarCollector(
        store,
        radar_collector.CollectorConfig(
            discovery_terms_per_run=3,
            posts_per_term=2,
            comments_posts_per_run=3,
        ),
        google=_SeedSource(
            [
                radar_collector.SeedTerm(
                    name,
                    "google_trends",
                    search_volume=5_000,
                    published_at=datetime.now(timezone.utc).isoformat(),
                )
                for name in ("Первый мем", "Второй мем", "Третий мем")
            ]
        ),
        telegram=_SeedSource([]),
        tiktok=tiktok,
        executor=_InlineExecutor(),
    )

    collector.queue_run("test")

    assert tiktok.comment_calls == []
    assert collector.status()["budget"]["posts"]["requests"] == 3
    assert collector.status()["budget"]["comments"]["requests"] == 0


def test_manual_comments_are_confirmed_limited_and_persisted(tmp_path):
    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    signal = store.ingest_signal(
        trend_radar.SignalInput(
            term="Воздухан",
            source_type="tiktok",
            source_url="https://www.tiktok.com/@creator/video/77",
            author="creator",
            views=80_000,
            comments_count=20,
        )
    )
    tiktok = _TrackingTikTok()
    collector = radar_collector.RadarCollector(
        store,
        radar_collector.CollectorConfig(comment_max_expected=500),
        google=_SeedSource([]), telegram=_SeedSource([]), tiktok=tiktok,
        executor=_InlineExecutor(),
    )

    with pytest.raises(PermissionError):
        collector.queue_comments(signal["trend_id"])
    queued = collector.queue_comments(signal["trend_id"], confirmed=True)

    assert queued["expected_records"] == 20
    assert len(tiktok.comment_calls) == 1
    job = collector.comment_job(queued["job_id"])
    assert job["status"] == "succeeded"
    assert job["records"] == 1
    assert collector.status()["budget"]["comments"]["remaining"] == 0


def test_timed_out_manual_comments_consume_slot_without_retry(tmp_path):
    class _TimeoutTikTok(_TrackingTikTok):
        def comments(self, post_url):
            self.comment_calls.append(post_url)
            raise requests.ReadTimeout("snapshot is still processing")

    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    signal = store.ingest_signal(
        trend_radar.SignalInput(
            term="Новый звук",
            source_type="tiktok",
            source_url="https://www.tiktok.com/@creator/video/88",
            comments_count=100,
        )
    )
    tiktok = _TimeoutTikTok()
    collector = radar_collector.RadarCollector(
        store, radar_collector.CollectorConfig(comments_daily_limit=1),
        google=_SeedSource([]), telegram=_SeedSource([]), tiktok=tiktok,
        executor=_InlineExecutor(),
    )

    queued = collector.queue_comments(signal["trend_id"], confirmed=True)
    assert collector.comment_job(queued["job_id"])["status"] == "timed_out"
    with pytest.raises(trend_radar.BrightDataBudgetExceeded):
        collector.queue_comments(signal["trend_id"], confirmed=True)
    assert len(tiktok.comment_calls) == 1


def test_enabled_scheduler_waits_full_interval_after_restart(tmp_path):
    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    executor = _HoldingExecutor()
    collector = radar_collector.RadarCollector(
        store,
        radar_collector.CollectorConfig(
            enabled=True, interval_seconds=900, initial_delay_seconds=1,
        ),
        google=_SeedSource([]), telegram=_SeedSource([]),
        tiktok=radar_collector.BrightDataClient(""), executor=executor,
    )

    before = datetime.now(timezone.utc)
    collector.start()
    try:
        next_run = datetime.fromisoformat(
            collector.status()["next_run_at"].replace("Z", "+00:00")
        )
        assert (next_run - before).total_seconds() >= 895
        assert executor.calls == []
    finally:
        collector.stop()
def test_queue_does_not_allow_overlapping_runs(tmp_path):
    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    executor = _HoldingExecutor()
    collector = radar_collector.RadarCollector(
        store,
        radar_collector.CollectorConfig(),
        google=_SeedSource([]),
        telegram=_SeedSource([]),
        tiktok=radar_collector.BrightDataClient(""),
        executor=executor,
    )

    first = collector.queue_run("owner")
    second = collector.queue_run("owner")

    assert first["queued"] is True
    assert second["queued"] is False
    assert second["reason"] == "already_running"
    assert len(executor.calls) == 1


def test_cancel_marks_pending_run_as_stopping(tmp_path):
    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    executor = _HoldingExecutor()
    collector = radar_collector.RadarCollector(
        store,
        radar_collector.CollectorConfig(),
        google=_SeedSource([]), telegram=_SeedSource([]),
        tiktok=radar_collector.BrightDataClient(""), executor=executor,
    )
    queued = collector.queue_run("owner")
    assert collector.cancel_run()["cancelled"] is True
    assert store.collector_run(queued["run_id"])["phase"] == "stopping"


def test_without_tiktok_key_free_sources_still_run(tmp_path):
    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    collector = radar_collector.RadarCollector(
        store,
        radar_collector.CollectorConfig(),
        google=_SeedSource(
            [radar_collector.SeedTerm("Черемша", "google_trends")]
        ),
        telegram=_SeedSource([]),
        tiktok=radar_collector.BrightDataClient(""),
        executor=_InlineExecutor(),
    )

    queued = collector.queue_run("test")

    run = store.collector_run(queued["run_id"])
    assert run["status"] == "succeeded"
    assert run["seeds_found"] == 1
    assert run["signals_created"] == 0
    assert collector.status()["providers"]["tiktok"]["configured"] is False
