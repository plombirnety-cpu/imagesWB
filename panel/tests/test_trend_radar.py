# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

import trend_radar


NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def store(tmp_path):
    return trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")


def _signal(
    term="Новый мем",
    *,
    url="https://www.tiktok.com/@author/video/123",
    source="tiktok",
    author="@author",
    published=None,
    comments="",
    caption="",
    views=0,
    likes=0,
    shares=0,
    comments_count=0,
):
    return trend_radar.SignalInput(
        term=term,
        source_type=source,
        source_url=url,
        author=author,
        published_at=published,
        comments=comments,
        caption=caption,
        views=views,
        likes=likes,
        shares=shares,
        comments_count=comments_count,
    )


def test_source_url_validation_blocks_wrong_hosts_and_non_https():
    assert trend_radar.validate_source_url(
        "tiktok", "https://www.tiktok.com/@a/video/1",
    ).startswith("https://")
    with pytest.raises(ValueError):
        trend_radar.validate_source_url("tiktok", "http://www.tiktok.com/@a/video/1")
    with pytest.raises(ValueError):
        trend_radar.validate_source_url(
            "tiktok", "https://www.tiktok.com.attacker.example/video/1",
        )


def test_schema_migrates_v1_database_and_seeds_first_measurement(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE trends (
                id TEXT PRIMARY KEY, canonical_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL, created_at TEXT NOT NULL,
                radar_first_seen_at TEXT NOT NULL, earliest_published_at TEXT,
                last_seen_at TEXT NOT NULL, lifecycle TEXT NOT NULL DEFAULT 'UNVERIFIED',
                score REAL NOT NULL DEFAULT 0, novelty_score REAL NOT NULL DEFAULT 0,
                burst_score REAL NOT NULL DEFAULT 0, spread_score REAL NOT NULL DEFAULT 0,
                merch_score REAL NOT NULL DEFAULT 0, approved INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE observations (
                id TEXT PRIMARY KEY, trend_id TEXT NOT NULL REFERENCES trends(id),
                source_type TEXT NOT NULL, source_url TEXT NOT NULL UNIQUE,
                author TEXT NOT NULL DEFAULT '', caption TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL, published_at TEXT,
                views INTEGER NOT NULL DEFAULT 0, likes INTEGER NOT NULL DEFAULT 0,
                shares INTEGER NOT NULL DEFAULT 0, thumbnail_url TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id TEXT NOT NULL REFERENCES observations(id),
                trend_id TEXT NOT NULL REFERENCES trends(id), author TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL, normalized_text TEXT NOT NULL, observed_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL, UNIQUE(observation_id, author, fingerprint)
            );
            CREATE TABLE ingest_jobs (
                id TEXT PRIMARY KEY, trend_id TEXT NOT NULL REFERENCES trends(id),
                observation_id TEXT NOT NULL REFERENCES observations(id),
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        now_text = NOW.isoformat().replace("+00:00", "Z")
        db.execute(
            """
            INSERT INTO trends (
                id, canonical_key, display_name, created_at, radar_first_seen_at,
                last_seen_at
            ) VALUES ('trend1', 'мем', 'Мем', ?, ?, ?)
            """,
            (now_text, now_text, now_text),
        )
        db.execute(
            """
            INSERT INTO observations (
                id, trend_id, source_type, source_url, observed_at, views, likes, shares
            ) VALUES ('obs1', 'trend1', 'tiktok',
                      'https://www.tiktok.com/@a/video/1', ?, 123, 12, 3)
            """,
            (now_text,),
        )

    migrated = trend_radar.TrendRadarStore(db_path)
    trend = migrated.get_trend("trend1")

    assert trend["velocity_score"] == 0
    assert trend["observations"][0]["comments_count"] == 0
    assert trend["measurement_count"] == 1


def test_store_marks_interrupted_collector_run_failed_on_restart(tmp_path):
    db_path = tmp_path / "radar.sqlite3"
    first = trend_radar.TrendRadarStore(db_path)
    run_id = first.create_collector_run("schedule", now=NOW)
    first.update_collector_run(run_id, status="running")

    restarted = trend_radar.TrendRadarStore(db_path)
    run = restarted.collector_run(run_id)

    assert run["status"] == "failed"
    assert run["error"] == "прерван перезапуском"


def test_single_signal_without_origin_date_is_unverified(store):
    result = store.ingest_signal(
        _signal(comments="\n".join(["@a: воздухан"] * 12)),
        now=NOW,
    )
    trend = store.get_trend(result["trend_id"])
    assert trend["lifecycle"] == "UNVERIFIED"
    assert trend["earliest_published_at"] is None
    assert trend["score"] <= 49


def test_verified_date_still_needs_independent_evidence(store):
    result = store.ingest_signal(
        _signal(published=NOW - timedelta(days=2)),
        now=NOW,
    )
    assert store.get_trend(result["trend_id"])["lifecycle"] == "UNVERIFIED"

    store.ingest_signal(
        _signal(
            url="https://youtu.be/new-meme",
            source="youtube",
            author="@other",
            published=NOW - timedelta(days=1),
        ),
        now=NOW,
    )
    trend = store.get_trend(result["trend_id"])
    assert trend["lifecycle"] == "NEW"
    assert trend["age_days"] >= 1


def test_old_meme_can_never_be_marked_new_only_because_comments_repeat(store):
    published = NOW - timedelta(days=90)
    first = store.ingest_signal(
        _signal(
            term="Котость",
            published=published,
            comments="\n".join(f"@u{i}: котость котость" for i in range(20)),
        ),
        now=NOW,
    )
    store.ingest_signal(
        _signal(
            term="Котость",
            url="https://youtu.be/kotost-old",
            source="youtube",
            author="@archive",
            published=published + timedelta(days=2),
            comments="\n".join(f"@v{i}: снова котость" for i in range(20)),
        ),
        now=NOW,
    )
    trend = store.get_trend(first["trend_id"])
    assert trend["age_days"] >= 88
    assert trend["lifecycle"] in {"MATURE", "RESURGENCE"}
    assert trend["lifecycle"] not in {"NEW", "RISING"}


def test_comment_terms_count_unique_repetition_and_ignore_copy_paste(store):
    result = store.ingest_signal(
        _signal(
            comments="\n".join(
                [
                    "@a: новая бурмалда пришла",
                    "@b: что за бурмалда",
                    "@c: опять бурмалда",
                    "@spam: бурмалда",
                    "@spam: бурмалда",
                    "@spam: бурмалда",
                ]
            )
        ),
        now=NOW,
    )
    trend = store.get_trend(result["trend_id"])
    term = next(item for item in trend["emerging_terms"] if item["term"] == "бурмалда")
    assert term["mentions_6h"] == 4
    assert term["unique_authors"] == 4
    assert term["score"] > 0


def test_rejected_trend_disappears_stays_rejected_and_suppresses_seed(store):
    store.upsert_seed("Движуха", "telegram_memes", now=NOW)
    result = store.ingest_signal(
        _signal(term="Движуха", caption="#движуха"),
        now=NOW,
    )

    rejected = store.set_decision(result["trend_id"], "reject")

    assert rejected["rejected"] is True
    assert store.list_trends() == []
    assert store.list_seeds() == []
    assert store.discovery_terms(4, now=NOW + timedelta(minutes=1)) == []

    store.ingest_signal(
        _signal(term="Движуха", caption="#движуха снова", views=100),
        now=NOW + timedelta(hours=1),
    )
    assert store.get_trend(result["trend_id"])["rejected"] is True
    assert store.list_trends() == []


def test_discovery_terms_rotate_instead_of_repeating_top_four(store):
    for index in range(8):
        store.upsert_seed(
            f"Мем {index}",
            "telegram_memes",
            now=NOW + timedelta(seconds=index),
        )

    first = store.discovery_terms(4, now=NOW + timedelta(minutes=1))
    second = store.discovery_terms(4, now=NOW + timedelta(minutes=2))

    assert len(first) == len(second) == 4
    assert set(first).isdisjoint(second)
    seeds = {row["display_name"]: row for row in store.list_seeds(20)}
    assert all(seeds[name]["query_count"] == 1 for name in first + second)


def test_fresh_google_spike_moves_to_front_of_tiktok_check_queue(store):
    store.upsert_seed("Обычный подтверждённый тег", "tiktok_hashtag", now=NOW)
    store.upsert_seed("Резкий рост", "google_trends", now=NOW)
    store.record_google_trend(
        "Резкий рост",
        5_000,
        published_at=NOW,
        now=NOW,
    )

    selected = store.discovery_terms(1, now=NOW + timedelta(minutes=1))

    assert selected == ["Резкий рост"]


def test_trend_payload_separates_specific_hashtags_from_generic_noise(store):
    result = store.ingest_signal(
        _signal(
            term="Движуха",
            caption="#fyp #viral #движуха #максимкац",
            author="@one",
        ),
        now=NOW,
    )
    store.ingest_signal(
        _signal(
            term="Движуха",
            url="https://www.tiktok.com/@two/video/456",
            author="@two",
            caption="#рек #движуха #максимкац",
        ),
        now=NOW,
    )

    trend = store.get_trend(result["trend_id"])

    assert trend["author_count"] == 2
    assert {item["tag"] for item in trend["hashtags"]} == {
        "#движуха", "#максимкац",
    }


class _OEmbedResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "type": "video",
            "title": "Новый Воздухан #мем",
            "author_name": "Автор",
            "thumbnail_url": "https://p16.example/thumb.jpeg",
            "provider_name": "TikTok",
            "author_url": "https://www.tiktok.com/@author",
        }


def test_oembed_job_is_idempotent_and_enriches_signal(store):
    result = store.ingest_signal(_signal(caption="", author=""), now=NOW)
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return _OEmbedResponse()

    store.run_ingest_job(result["job_id"], fetch=fetch, sleep=lambda _: None)
    store.run_ingest_job(result["job_id"], fetch=fetch, sleep=lambda _: None)

    assert len(calls) == 1
    assert calls[0][0] == "https://www.tiktok.com/oembed"
    assert calls[0][1]["params"]["url"].startswith("https://www.tiktok.com/")
    assert store.job(result["job_id"])["status"] == "succeeded"
    observation = store.get_trend(result["trend_id"])["observations"][0]
    assert observation["caption"] == "Новый Воздухан #мем"
    assert observation["author"] == "Автор"
    assert observation["thumbnail_url"].startswith("https://")


def test_repeated_url_updates_metrics_without_duplicate_observation(store):
    first = store.ingest_signal(
        _signal(views=1_000, likes=100, shares=5, comments_count=20),
        now=NOW,
    )
    second = store.ingest_signal(
        _signal(
            views=5_000,
            likes=600,
            shares=45,
            comments_count=120,
            comments="@fresh1: появился шлёпозавр\n@fresh2: опять шлёпозавр",
        ),
        now=NOW + timedelta(hours=2),
    )

    assert second["duplicate"] is True
    assert second["updated"] is True
    assert second["new_comments"] == 2
    assert second["trend_id"] == first["trend_id"]
    trend = store.get_trend(first["trend_id"])
    assert trend["observation_count"] == 1
    assert trend["measurement_count"] == 2
    assert trend["observations"][0]["views"] == 5_000
    assert trend["velocity"]["views_per_hour"] == 2_000
    assert trend["velocity"]["shares_per_hour"] == 20
    assert trend["velocity_score"] > 0
    assert any(item["term"] == "шлепозавр" for item in trend["emerging_terms"])


def test_repeated_lower_counters_never_create_negative_velocity(store):
    first = store.ingest_signal(
        _signal(views=5_000, likes=500, shares=50, comments_count=100),
        now=NOW,
    )
    store.ingest_signal(
        _signal(views=4_000, likes=450, shares=40, comments_count=90),
        now=NOW + timedelta(hours=2),
    )

    trend = store.get_trend(first["trend_id"])
    assert trend["observations"][0]["views"] == 5_000
    assert trend["velocity"]["views_per_hour"] == 0
    assert trend["velocity"]["shares_per_hour"] == 0


def test_near_immediate_counter_revision_is_not_treated_as_velocity(store):
    first = store.ingest_signal(
        _signal(views=1_000, likes=100, shares=10, comments_count=20),
        now=NOW,
    )
    store.ingest_signal(
        _signal(views=5_000, likes=500, shares=50, comments_count=100),
        now=NOW + timedelta(minutes=2),
    )

    trend = store.get_trend(first["trend_id"])
    assert trend["measurement_count"] == 2
    assert trend["velocity"]["views_per_hour"] == 0
    assert trend["velocity_score"] == 0


def test_old_meme_with_new_metric_spike_is_resurgence_not_rising(store):
    published = NOW - timedelta(days=60)
    first = store.ingest_signal(
        _signal(
            term="Старый мем",
            published=published,
            views=100_000,
            shares=1_000,
        ),
        now=NOW,
    )
    store.ingest_signal(
        _signal(
            term="Старый мем",
            url="https://youtu.be/old-meme-proof",
            source="youtube",
            author="@archive",
            published=published + timedelta(days=1),
            views=50_000,
            shares=500,
        ),
        now=NOW,
    )
    store.ingest_signal(
        _signal(
            term="Старый мем",
            views=500_000,
            shares=12_000,
        ),
        now=NOW + timedelta(hours=2),
    )

    trend = store.get_trend(first["trend_id"])
    assert trend["velocity_score"] >= 70
    assert trend["lifecycle"] == "RESURGENCE"


def test_print_opportunity_requires_google_spike_and_two_viral_tiktoks(store):
    term = "Сова на скакалке"
    store.record_google_trend(
        term,
        5_000,
        published_at=NOW - timedelta(hours=2),
        now=NOW,
    )
    first = store.ingest_signal(
        _signal(
            term=term,
            url="https://www.tiktok.com/@one/video/101",
            author="@one",
            published=NOW - timedelta(hours=4),
            caption="Сова снова прыгает #сованаскакалке",
            views=70_000,
            likes=8_000,
            shares=900,
        ),
        now=NOW,
    )
    store.ingest_signal(
        _signal(
            term=term,
            url="https://www.tiktok.com/@two/video/202",
            author="@two",
            published=NOW - timedelta(hours=3),
            caption="Все повторяют сову #сованаскакалке",
            views=90_000,
            likes=9_000,
            shares=1_100,
        ),
        now=NOW,
    )

    trend = store.get_trend(first["trend_id"])
    opportunity = trend["opportunity"]

    assert opportunity["qualified"] is True
    assert opportunity["google"]["spike"] is True
    assert opportunity["google"]["is_new"] is True
    assert opportunity["tiktok"]["viral_videos"] == 2
    assert opportunity["tiktok"]["author_count"] == 2
    assert opportunity["tiktok"]["total_views"] == 160_000
    assert opportunity["confidence"] >= 70
    assert opportunity["idea"]["headline"] == "СОВА НА СКАКАЛКЕ"
    assert "фигурный принт" in opportunity["idea"]["composition"].casefold()
    assert [item["id"] for item in store.list_opportunities()] == [
        first["trend_id"],
    ]


def test_google_only_or_single_viral_video_is_not_a_print_opportunity(store):
    term = "Одинокий сигнал"
    store.record_google_trend(
        term,
        10_000,
        published_at=NOW - timedelta(hours=1),
        now=NOW,
    )
    result = store.ingest_signal(
        _signal(
            term=term,
            author="@only",
            published=NOW - timedelta(hours=1),
            views=500_000,
            shares=8_000,
        ),
        now=NOW,
    )

    opportunity = store.get_trend(result["trend_id"])["opportunity"]

    assert opportunity["google"]["spike"] is True
    assert opportunity["tiktok"]["confirmed"] is False
    assert opportunity["qualified"] is False
    assert opportunity["idea"] is None
    assert store.list_opportunities() == []


def test_google_acceleration_qualifies_after_repeated_measurement(store):
    term = "Вторая волна"
    store.record_google_trend(
        term, 2_000,
        published_at=NOW - timedelta(days=3),
        now=NOW - timedelta(hours=3),
    )
    store.record_google_trend(
        term, 5_000,
        published_at=NOW - timedelta(days=3),
        now=NOW,
    )
    store.ingest_signal(
        _signal(term=term, published=NOW, views=60_000, shares=500),
        now=NOW,
    )
    second = store.ingest_signal(
        _signal(
            term=term,
            url="https://www.tiktok.com/@other/video/222",
            author="@other",
            published=NOW,
            views=70_000,
            shares=600,
        ),
        now=NOW,
    )

    google = store.get_trend(second["trend_id"])["opportunity"]["google"]
    assert google["accelerating"] is True
    assert google["growth_percent"] == 150.0


def test_generation_requires_owner_approval(store):
    result = store.ingest_signal(_signal(term="Воздухан"), now=NOW)
    with pytest.raises(PermissionError):
        store.generation_prompt(result["trend_id"])
    store.set_decision(result["trend_id"], "approve")
    prompt = store.generation_prompt(result["trend_id"])
    assert "Воздухан" in prompt
    assert "не стикер" in prompt
    assert "green/blue chroma" in prompt
