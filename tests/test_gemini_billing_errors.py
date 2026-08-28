# -*- coding: utf-8 -*-
"""Регрессии обработки исчерпанного Gemini API Prepay-баланса.

Все запросы замоканы: тесты не читают реальный ключ, не выходят в сеть и ничего
не списывают.  Главное поведение — постоянная биллинговая 429 распознаётся
отдельно от обычного rate limit, не ретраится и даёт оператору понятную ссылку.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import llm_provider
import providers


_PREPAY_ERROR = (
    '{"error":{"code":429,"message":"Your prepayment credits are depleted. '
    'Please go to AI Studio to manage your project and billing.",'
    '"status":"RESOURCE_EXHAUSTED"}}'
)


class _FakeResponse:
    def __init__(self, status_code: int, text: str, json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        return self._json_data


def _assert_operator_friendly_prepay_error(exc: BaseException) -> None:
    message = str(exc)
    lowered = message.lower()
    assert "законч" in lowered
    assert "prepay" in lowered
    assert "https://aistudio.google.com/billing" in message


def test_art_director_prepay_429_is_not_retried_and_has_billing_link(monkeypatch):
    calls = []
    sleeps = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeResponse(429, _PREPAY_ERROR)

    monkeypatch.setattr(llm_provider.config, "GEMINI_API_KEY", "offline-test-key")
    monkeypatch.setattr(llm_provider.config, "ART_DIRECTOR_MODEL", "offline-model")
    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    monkeypatch.setattr(llm_provider.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError) as exc_info:
        llm_provider._generate_gemini_text("system", "user", max_tokens=10)

    assert len(calls) == 1
    assert sleeps == []
    _assert_operator_friendly_prepay_error(exc_info.value)


def test_art_director_regular_429_remains_retriable(monkeypatch):
    responses = [
        _FakeResponse(429, '{"error":{"message":"rate limit exceeded"}}'),
        _FakeResponse(200, "", {
            "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
        }),
    ]
    sleeps = []

    def fake_post(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(llm_provider.config, "GEMINI_API_KEY", "offline-test-key")
    monkeypatch.setattr(llm_provider.config, "ART_DIRECTOR_MODEL", "offline-model")
    monkeypatch.setattr(llm_provider.requests, "post", fake_post)
    monkeypatch.setattr(llm_provider.time, "sleep", sleeps.append)

    assert llm_provider._generate_gemini_text("system", "user", max_tokens=10) == "OK"
    assert sleeps == [4]
    assert responses == []


def test_image_generation_prepay_429_is_not_retried_and_has_billing_link(monkeypatch):
    calls = []
    sleeps = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeResponse(429, _PREPAY_ERROR)

    monkeypatch.setattr(providers.config, "GEMINI_API_KEY", "offline-test-key")
    monkeypatch.setattr(providers.requests, "post", fake_post)
    monkeypatch.setattr(providers.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError) as exc_info:
        providers._generate_gemini("draw a print", seed=1)

    assert len(calls) == 1
    assert sleeps == []
    _assert_operator_friendly_prepay_error(exc_info.value)


def test_multi_reference_prepay_429_is_not_retried_and_has_billing_link(monkeypatch):
    calls = []
    sleeps = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeResponse(429, _PREPAY_ERROR)

    monkeypatch.setattr(providers.config, "IMAGE_PROVIDER", "gemini")
    monkeypatch.setattr(providers.config, "GEMINI_API_KEY", "offline-test-key")
    monkeypatch.setattr(providers.requests, "post", fake_post)
    monkeypatch.setattr(providers.time, "sleep", sleeps.append)

    reference = Image.new("RGB", (8, 8), (10, 20, 30))
    with pytest.raises(RuntimeError) as exc_info:
        providers.generate_image_with_references("put print on shirt", [reference])

    assert len(calls) == 1
    assert sleeps == []
    _assert_operator_friendly_prepay_error(exc_info.value)


def test_panel_exposes_clickable_ai_studio_billing_link():
    html = (Path(__file__).resolve().parents[1] / "panel" / "static" / "index.html").read_text(
        encoding="utf-8",
    )

    assert "https://aistudio.google.com/billing" in html
    assert "Пополнить Gemini API" in html
