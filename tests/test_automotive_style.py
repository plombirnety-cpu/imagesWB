# -*- coding: utf-8 -*-
"""Офлайн-контракт автономного автомобильного style 37."""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import art_director as ad  # noqa: E402


STYLE_ID = "37_auto_racing_editorial"
STYLE_BANK_PATH = PROJECT_ROOT / "docs" / "STYLE_BANK.json"


def _style() -> dict:
    data = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    return next(style for style in data["styles"] if style["id"] == STYLE_ID)


def test_automotive_style_is_theme_optional_and_semantic():
    style = _style()

    assert style["theme_optional"] is True
    assert "meaningful" in style["essence"].lower()
    assert "one hero automobile" in style["essence"].lower()
    assert "not a rectangular photograph" in style["essence"].lower()
    assert "sticker outline" in style["essence"].lower()
    assert "lorem ipsum" in style["essence"].lower()


def test_automotive_quality_hint_is_style_specific():
    hint = ad._automotive_editorial_quality_hint(STYLE_ID).lower()

    assert "ровно одну" in hint
    assert "смысловую" in hint
    assert "type_spec" in hint
    assert "прямоугольной фотографии" in hint
    assert "neutral dual-contrast type" in hint
    assert "charcoal inner stroke" in hint
    assert "warm-ivory outer keyline" in hint
    assert ad._automotive_editorial_quality_hint("34_anime_magazine_cover") == ""


def test_ask_claude_includes_automotive_quality_contract(monkeypatch):
    captured = {}

    def fake_generate_text(system, user, max_tokens=1500):
        captured["system"] = system
        captured["user"] = user
        return "[]"

    monkeypatch.setattr(ad.llm_provider, "generate_text", fake_generate_text)
    ad._ask_claude(
        "Автономная авто-серия: выбери один болид",
        1,
        "cutout",
        style_pref=STYLE_ID,
    )

    user = captured["user"].lower()
    assert "смысловую текстовую иерархию" in user
    assert "одну конкретную реальную машину" in user
    assert "стены спонсоров" in user
    assert f'id="{STYLE_ID}"' in captured["system"]


def test_build_prompt_reinforces_car_print_not_photo_or_sticker():
    prompt = ad.build_prompt({
        "prompt": "A red racing car in a low three-quarter view.",
        "chroma": "green",
        "style_id": STYLE_ID,
        "style_mix": "",
        "signature_props": "",
        "type_spec": "MODEL NAME above; HERITAGE IN MOTION below",
        "quote": "",
        "slogan": "",
        "name_jp": "",
        "text_mode": "none",
        "text_modes_v3": [],
        "has_human_figure": False,
    }).lower()

    assert "automotive editorial streetwear print" in prompt
    assert "not a rectangular photograph" in prompt
    assert "never put a white sticker outline" in prompt
    assert "solid, perfectly uniform bright green chroma-key" in prompt


def test_automotive_style_requires_universal_neutral_lettering():
    style = _style()
    text_treatment = style["text_treatment"].lower()
    constraints = " ".join(style["constraints"]).lower()

    assert "neutral silver" in text_treatment
    assert "charcoal inner stroke" in text_treatment
    assert "warm-ivory outer keyline" in text_treatment
    assert "body colour" in text_treatment
    assert "двойной контрастный контур" in constraints
