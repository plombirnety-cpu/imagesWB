# -*- coding: utf-8 -*-
"""Офлайн-контракт profession style 40; платные API здесь не вызываются."""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import art_director as ad  # noqa: E402
import batch_print  # noqa: E402
import config  # noqa: E402


STYLE_ID = "40_profession_technical_archive"
STYLE_BANK_PATH = PROJECT_ROOT / "docs" / "STYLE_BANK.json"


def _style() -> dict:
    data = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    return next(style for style in data["styles"] if style["id"] == STYLE_ID)


def test_profession_archive_style_has_approved_v2_contract():
    style = _style()
    contract = " ".join(
        [style["essence"], style["text_treatment"], *style["constraints"]]
    ).lower()

    assert style["theme_optional"] is False
    assert "exactly four" in contract
    assert "38-45%" in contract
    assert "1, 2, 3 and 4" in contract
    assert "20-30%" in contract
    assert "clip-art" in contract
    assert "точное русское название профессии" in contract


def test_profession_archive_quality_hint_is_style_specific():
    hint = ad._profession_archive_quality_hint(STYLE_ID).lower()

    assert "quote" in hint
    assert "type_spec" in hint
    assert "has_human_figure=false" in hint
    assert "style_mix" in hint
    assert "1, 2, 3, 4" in hint
    assert "clip art" in hint
    assert ad._profession_archive_quality_hint("39_rock_band_print") == ""


def test_ask_claude_includes_profession_archive_quality_contract(monkeypatch):
    captured = {}

    def fake_generate_text(system, user, max_tokens=1500):
        captured["system"] = system
        captured["user"] = user
        return "[]"

    monkeypatch.setattr(ad.llm_provider, "generate_text", fake_generate_text)
    ad._ask_claude("ХИРУРГ", 1, "cutout", style_pref=STYLE_ID)

    assert "ровно четыре узнаваемых рабочих предмета" in captured["user"].lower()
    assert "единственный видимый текст" in captured["user"].lower()
    assert f'id="{STYLE_ID}"' in captured["system"]


def test_build_prompt_reinforces_open_archive_and_exact_title():
    prompt = ad.build_prompt({
        "prompt": "An asymmetric surgical tool archive.",
        "chroma": "green",
        "style_id": STYLE_ID,
        "style_mix": "",
        "signature_props": "",
        "type_spec": "One central uppercase Cyrillic title",
        "quote": "ХИРУРГ",
        "slogan": "",
        "name_jp": "",
        "kana": "",
        "text_mode": "none",
        "text_modes_v3": [],
        "has_human_figure": False,
    }).lower()

    assert "profession technical archive apparel print" in prompt
    assert "spell the phrase exactly, letter by letter: хирург" in prompt
    assert "no other text anywhere" in prompt
    assert "solid, perfectly uniform bright green chroma-key" in prompt


def test_profession_archive_uses_premium_gemini_only(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_MODEL_PREMIUM", "premium-test-model")

    assert batch_print._generation_model_for_design({"style_id": STYLE_ID}) == (
        "premium-test-model"
    )
    assert batch_print._generation_model_for_design({
        "style_id": "01_baroque_frame",
        "style_mix": STYLE_ID,
    }) == "premium-test-model"
    assert batch_print._generation_model_for_design({
        "style_id": "39_rock_band_print",
    }) is None

    monkeypatch.setattr(config, "IMAGE_PROVIDER", "pollinations")
    assert batch_print._generation_model_for_design({"style_id": STYLE_ID}) is None
