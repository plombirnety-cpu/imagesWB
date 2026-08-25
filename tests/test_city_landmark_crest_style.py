from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STYLE_BANK_PATH = PROJECT_ROOT / "docs" / "STYLE_BANK.json"
STYLE_ID = "41_city_neocrest_ru"


def _style() -> dict:
    bank = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    return next(style for style in bank["styles"] if style["id"] == STYLE_ID)


def test_city_landmark_crest_is_manual_russian_city_style() -> None:
    style = _style()

    assert style["name_ru"] == "Городской неогерб — насыщенный (рус.)"
    assert style["manual_only"] is True
    assert style["theme_optional"] is False


def test_city_landmark_crest_enforces_cyrillic_and_landmark_accuracy() -> None:
    style = _style()
    contract = " ".join(
        [
            style["essence"],
            style["text_treatment"],
            style["palette_rule"],
            *style["constraints"],
        ]
    ).lower()

    assert "cyrillic" in contract
    assert "zero latin letters" in contract
    assert "fantasy hybrid" in contract
    assert "8-9" in contract
    assert "86-92%" in contract
    assert "at least four saturated hues" in contract
    assert "near-black covers no more than 15%" in contract
    assert "three-stage" in contract


def test_city_neocrest_quality_hint_is_rich_and_style_specific() -> None:
    import art_director as ad

    hint = ad._city_neocrest_quality_hint(STYLE_ID).lower()

    assert "rich city neo-crest v2" in hint
    assert "7-10" in hint
    assert "86-92%" in hint
    assert "8-9" in hint
    assert "near-black" in hint
    assert ad._city_neocrest_quality_hint("40_profession_technical_archive") == ""


def test_city_neocrest_build_prompt_injects_mandatory_colour_contract() -> None:
    import art_director as ad

    prompt = ad.build_prompt({
        "prompt": "Dense accurate city composition.",
        "style_id": STYLE_ID,
        "style_mix": "",
        "chroma": "green",
        "type_spec": "",
        "has_human_figure": False,
    }).lower()

    assert "mandatory city colour contract" in prompt
    assert "medium and bright colours cover at least 65%" in prompt
    assert "never use black as an interior background fill" in prompt


def test_city_landmark_crest_has_exactly_one_catalog_entry() -> None:
    bank = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    matches = [style for style in bank["styles"] if style["id"] == STYLE_ID]

    assert len(matches) == 1


def test_city_neocrest_uses_premium_model_only_as_primary_style(monkeypatch) -> None:
    import batch_print

    monkeypatch.setattr(batch_print.config, "IMAGE_PROVIDER", "gemini")
    monkeypatch.setattr(
        batch_print.config,
        "GEMINI_MODEL_PREMIUM",
        "test-premium-image-model",
    )

    assert batch_print._generation_model_for_design({"style_id": STYLE_ID}) == (
        "test-premium-image-model"
    )
    assert batch_print._generation_model_for_design({
        "style_id": "01_baroque_frame",
        "style_mix": STYLE_ID,
    }) is None
