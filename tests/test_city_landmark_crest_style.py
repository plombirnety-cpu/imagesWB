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

    assert style["name_ru"] == "Городской неогерб (рус.)"
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
    assert "6-7" in contract
    assert "three-stage" in contract


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
