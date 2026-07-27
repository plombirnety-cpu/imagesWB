# -*- coding: utf-8 -*-
from __future__ import annotations

from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient

import app as panel_app
import trend_radar


class _InlineExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, function, *args, **kwargs):
        self.calls.append((function, args, kwargs))
        future = Future()
        future.set_result(None)
        return future


class _CollectorStub:
    def __init__(self):
        self.run_calls = []

    def status(self):
        return {
            "enabled": True,
            "running": False,
            "next_run_at": None,
            "interval_seconds": 10800,
            "providers": {
                "google_trends": {"configured": True, "geo": "RU"},
                "telegram": {"configured": False, "channels": 0},
                "tiktok": {"configured": False, "provider": "Bright Data"},
            },
            "latest_run": None,
        }

    def queue_run(self, trigger):
        self.run_calls.append(trigger)
        return {"queued": True, "run_id": "automatic123"}


@pytest.fixture
def radar_client(tmp_path, monkeypatch):
    store = trend_radar.TrendRadarStore(tmp_path / "radar.sqlite3")
    radar_executor = _InlineExecutor()
    collector = _CollectorStub()
    monkeypatch.setattr(panel_app, "_radar_store", store)
    monkeypatch.setattr(panel_app, "_radar_executor", radar_executor)
    monkeypatch.setattr(panel_app, "_radar_collector", collector)
    monkeypatch.setattr(panel_app.settings, "ACCESS_PASSWORD_SHA256", "")
    return TestClient(panel_app.app), store, radar_executor, collector


def _payload(**overrides):
    payload = {
        "term": "Воздухан",
        "source_type": "tiktok",
        "source_url": "https://www.tiktok.com/@author/video/123",
        "author": "@author",
        "comments": "@u1: нужен воздухан\n@u2: что за воздухан",
        "views": 1000,
        "likes": 100,
        "comments_count": 20,
    }
    payload.update(overrides)
    return payload


def test_signal_endpoint_persists_card_and_queues_oembed(radar_client):
    client, _, radar_executor, _ = radar_client

    response = client.post("/api/radar/signals", json=_payload())

    assert response.status_code == 202
    result = response.json()
    assert result["duplicate"] is False
    assert result["trend"]["lifecycle"] == "UNVERIFIED"
    assert len(radar_executor.calls) == 1
    trends = client.get("/api/radar/trends").json()
    assert len(trends) == 1
    assert trends[0]["display_name"] == "Воздухан"


def test_signal_endpoint_rejects_spoofed_tiktok_host(radar_client):
    client, _, radar_executor, _ = radar_client

    response = client.post(
        "/api/radar/signals",
        json=_payload(source_url="https://www.tiktok.com.attacker.example/video/123"),
    )

    assert response.status_code == 400
    assert not radar_executor.calls


def test_duplicate_signal_is_idempotent(radar_client):
    client, _, radar_executor, _ = radar_client

    first = client.post("/api/radar/signals", json=_payload())
    second = client.post("/api/radar/signals", json=_payload())

    assert first.status_code == second.status_code == 202
    assert second.json()["duplicate"] is True
    assert second.json()["updated"] is False
    assert len(radar_executor.calls) == 1
    assert client.get("/api/radar/trends").json()[0]["observation_count"] == 1


def test_batch_endpoint_imports_new_signals_and_updates_existing(radar_client):
    client, _, radar_executor, _ = radar_client
    client.post("/api/radar/signals", json=_payload())

    response = client.post(
        "/api/radar/signals/batch",
        json={
            "signals": [
                _payload(views=9_000, likes=800, shares=120, comments_count=300),
                _payload(
                    term="Шлёпозавр",
                    source_url="https://www.tiktok.com/@other/video/456",
                    author="@other",
                    views=2_000,
                ),
            ],
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["received"] == 2
    assert payload["created"] == 1
    assert payload["updated"] == 1
    assert payload["errors"] == []
    assert len(radar_executor.calls) == 2
    trends = client.get("/api/radar/trends").json()
    assert {trend["display_name"] for trend in trends} == {"Воздухан", "Шлёпозавр"}
    vozdukhan = next(trend for trend in trends if trend["display_name"] == "Воздухан")
    assert vozdukhan["observations"][0]["views"] == 9_000


def test_owner_must_approve_before_generation(radar_client, monkeypatch):
    client, _, _, _ = radar_client
    trend_id = client.post("/api/radar/signals", json=_payload()).json()["trend_id"]
    generation_executor = _InlineExecutor()
    monkeypatch.setattr(panel_app, "_executor", generation_executor)

    blocked = client.post(f"/api/radar/trends/{trend_id}/generate", json={"count": 3})
    approved = client.post(f"/api/radar/trends/{trend_id}/approve")
    started = client.post(f"/api/radar/trends/{trend_id}/generate", json={"count": 3})

    assert blocked.status_code == 409
    assert approved.status_code == 200
    assert approved.json()["approved"] is True
    assert started.status_code == 200
    assert len(generation_executor.calls) == 1
    job_id = started.json()["job_id"]
    assert generation_executor.calls[0][0] == panel_app._run_job
    submitted_args = generation_executor.calls[0][1]
    assert submitted_args[2] == 3
    assert "Воздухан" in submitted_args[5]
    with panel_app._jobs_lock:
        panel_app._jobs.pop(job_id, None)


def test_reject_disables_previous_approval(radar_client):
    client, _, _, _ = radar_client
    trend_id = client.post("/api/radar/signals", json=_payload()).json()["trend_id"]

    client.post(f"/api/radar/trends/{trend_id}/approve")
    rejected = client.post(f"/api/radar/trends/{trend_id}/reject")

    assert rejected.status_code == 200
    assert rejected.json()["approved"] is False
    assert rejected.json()["rejected"] is True
    assert client.get("/api/radar/trends").json() == []


def test_automatic_collector_status_and_manual_trigger(radar_client):
    client, store, _, collector = radar_client
    store.upsert_seed("Новый звук", "google_trends")

    status = client.get("/api/radar/collector/status")
    seeds = client.get("/api/radar/seeds")
    started = client.post("/api/radar/collector/run")

    assert status.status_code == 200
    assert status.json()["providers"]["google_trends"]["configured"] is True
    assert seeds.json()[0]["display_name"] == "Новый звук"
    assert started.status_code == 202
    assert started.json()["run_id"] == "automatic123"
    assert collector.run_calls == ["owner"]
