# -*- coding: utf-8 -*-
"""Регрессия живой партии 1116ed9302ac: style 34 не должен превращаться в
прямоугольную журнальную карточку, а IMAGE_OTHER нельзя повторять без изменений.

Все тесты офлайн: синтетические PIL-кадры и мок HTTP Gemini.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import art_director  # noqa: E402
import batch_print  # noqa: E402
import providers  # noqa: E402


STYLE_ID = "34_anime_magazine_cover"


def _style34_design(**overrides) -> dict:
    design = {
        "prompt": "A close portrait of Satoru Gojo with white hair and blue eyes.",
        "chroma": "green",
        "slogan": "HONORED ONE",
        "kana": "ゴジョウ",
        "name_jp": "五条悟",
        "character_en": "Satoru Gojou",
        "title_en": "Jujutsu Kaisen",
        "signature_props": "black blindfold, bright blue eyes",
        "style_id": STYLE_ID,
        "style_mix": "",
        "type_spec": "large katakana title and small editorial captions",
        "quote": "",
        "text_mode": "none",
        "text_modes_v3": [],
        "has_human_figure": False,
    }
    design.update(overrides)
    return design


def _synthetic_layout(*, touches_bottom: bool) -> Image.Image:
    img = Image.new("RGB", (400, 600), (0, 177, 64))
    draw = ImageDraw.Draw(img)
    bottom = 599 if touches_bottom else 515
    left = 0 if touches_bottom else 82
    right = 399 if touches_bottom else 318
    draw.rounded_rectangle((left, 90, right, bottom), radius=55, fill=(190, 55, 40))
    if not touches_bottom:
        # Фигурный «эффект-контур» под бюстом, но с чистым chroma moat до края.
        draw.polygon([(75, 500), (125, 465), (165, 530), (215, 470),
                      (270, 525), (325, 490), (300, 545), (105, 545)],
                     fill=(255, 130, 15))
    return img


def test_style34_prompt_contract_is_diecut_print_not_rectangular_cover():
    hint = art_director._MAGAZINE_COVER_QUALITY_HINT.lower()
    suffix = batch_print._MAGAZINE_PRINT_PROMPT_SUFFIX.lower()
    for text in (hint, suffix):
        assert "signature-effect cradle" in text
        assert "chroma moat" in text
        assert "not a rectangular" in text
        assert "bottom edge" in text


def test_style34_layout_gate_accepts_tanjiro_shape_and_rejects_full_bleed_bottom():
    good, good_metrics = batch_print._magazine_print_layout_quality(
        _synthetic_layout(touches_bottom=False), chroma="green")
    bad, bad_metrics = batch_print._magazine_print_layout_quality(
        _synthetic_layout(touches_bottom=True), chroma="green")

    assert good is True, good_metrics
    assert bad is False, bad_metrics
    assert good_metrics["bottom"] >= 0.85
    assert bad_metrics["bottom"] < 0.20


class _ImageOtherResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {"candidates": [{
            "content": {},
            "finishReason": "IMAGE_OTHER",
            "finishMessage": "Unable to show the generated image.",
        }]}


def test_gemini_image_other_is_structured_and_not_retried_identically(monkeypatch):
    calls = {"post": 0, "sleep": 0}

    def fake_post(*args, **kwargs):
        calls["post"] += 1
        return _ImageOtherResponse()

    monkeypatch.setattr(providers.config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(providers.requests, "post", fake_post)
    monkeypatch.setattr(providers.time, "sleep",
                        lambda *_: calls.__setitem__("sleep", calls["sleep"] + 1))

    with pytest.raises(providers.GeminiImageRejected) as exc_info:
        providers._generate_gemini("draw a safe portrait", reference=Image.new("RGB", (8, 8)))

    assert exc_info.value.finish_reason == "IMAGE_OTHER"
    assert calls == {"post": 1, "sleep": 0}


def test_image_other_recovery_drops_reference_then_uses_safe_editorial_prompt():
    reference = Image.new("RGB", (8, 8), (10, 20, 30))
    design = _style34_design()
    original = "violent cursed battle with weapon and blood"

    first_prompt, first_ref = batch_print._recover_image_other(
        design, original, reference, rejection_count=1)
    second_prompt, second_ref = batch_print._recover_image_other(
        design, original, reference, rejection_count=2)

    assert first_ref is None
    assert first_prompt != original
    assert "pg-rated" in first_prompt.lower()
    assert second_ref is None
    assert "satoru gojou" in second_prompt.lower()
    assert "non-violent editorial portrait" in second_prompt.lower()
    assert "blood" not in second_prompt.lower()
    assert "weapon" not in second_prompt.lower()


def test_render_design_retries_full_bleed_then_keeps_diecut_layout(tmp_path, monkeypatch):
    images = [
        _synthetic_layout(touches_bottom=True),
        _synthetic_layout(touches_bottom=False),
    ]
    calls = {"n": 0}

    def fake_generate(*args, **kwargs):
        image = images[calls["n"]]
        calls["n"] += 1
        return image

    monkeypatch.setattr(batch_print.providers, "generate_image", fake_generate)
    monkeypatch.setattr(batch_print.config, "TEXT_RENDER", "code")
    monkeypatch.setattr(batch_print, "_verify_anatomy", lambda *a, **k: (True, {}))
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)

    result = batch_print.render_design(
        _style34_design(),
        "style34-good",
        tmp_path,
        timeout_retries=1,
        green_only=True,
    )

    assert result["ok"] is True
    assert calls["n"] == 2
    saved = Image.open(result["green"]).convert("RGB")
    assert batch_print._magazine_print_layout_quality(saved, "green")[0] is True


def test_render_design_hard_rejects_full_bleed_style34(tmp_path, monkeypatch):
    monkeypatch.setattr(
        batch_print.providers,
        "generate_image",
        lambda *a, **k: _synthetic_layout(touches_bottom=True),
    )
    monkeypatch.setattr(batch_print.config, "TEXT_RENDER", "code")
    monkeypatch.setattr(batch_print, "_verify_anatomy", lambda *a, **k: (True, {}))
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)

    result = batch_print.render_design(
        _style34_design(),
        "style34-bad",
        tmp_path,
        timeout_retries=0,
        green_only=True,
    )

    assert result["ok"] is False
    assert "прямоугольная" in result["error"]
    assert not (tmp_path / "style34-bad.png").exists()


def test_render_design_image_other_changes_prompt_and_drops_reference(
    tmp_path, monkeypatch,
):
    captured = []

    def fake_generate(prompt, seed=None, reference=None, **kwargs):
        captured.append((prompt, reference))
        if len(captured) == 1:
            raise providers.GeminiImageRejected("IMAGE_OTHER", "rephrase")
        return _synthetic_layout(touches_bottom=False)

    reference = Image.new("RGB", (20, 20), (200, 200, 200))
    monkeypatch.setattr(batch_print.providers, "generate_image", fake_generate)
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: reference)
    monkeypatch.setattr(batch_print.config, "TEXT_RENDER", "code")
    monkeypatch.setattr(batch_print, "_verify_anatomy", lambda *a, **k: (True, {}))

    result = batch_print.render_design(
        _style34_design(),
        "style34-image-other",
        tmp_path,
        timeout_retries=1,
        green_only=True,
    )

    assert result["ok"] is True
    assert len(captured) == 2
    assert captured[0][1] is reference
    assert captured[1][1] is None
    assert captured[1][0] != captured[0][0]
    assert "pg-rated" in captured[1][0].lower()
