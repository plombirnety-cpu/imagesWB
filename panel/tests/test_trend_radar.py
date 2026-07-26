# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import trend_radar


NOW = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)


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
):
    return trend_radar.SignalInput(
        term=term,
        source_type=source,
        source_url=url,
        author=author,
        published_at=published,
        comments=comments,
        caption=caption,
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


def test_duplicate_url_does_not_create_duplicate_observation(store):
    first = store.ingest_signal(_signal(), now=NOW)
    second = store.ingest_signal(_signal(), now=NOW + timedelta(minutes=1))
    assert second["duplicate"] is True
    assert second["trend_id"] == first["trend_id"]
    assert store.get_trend(first["trend_id"])["observation_count"] == 1


def test_generation_requires_owner_approval(store):
    result = store.ingest_signal(_signal(term="Воздухан"), now=NOW)
    with pytest.raises(PermissionError):
        store.generation_prompt(result["trend_id"])
    store.set_decision(result["trend_id"], "approve")
    prompt = store.generation_prompt(result["trend_id"])
    assert "Воздухан" in prompt
    assert "не стикер" in prompt
    assert "green/blue chroma" in prompt
