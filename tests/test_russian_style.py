from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STYLE_BANK_PATH = PROJECT_ROOT / "docs" / "STYLE_BANK.json"
STYLE_ID = "42_russian_style"


def _style() -> dict:
    bank = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    return next(style for style in bank["styles"] if style["id"] == STYLE_ID)


def test_russian_style_is_manual_autonomous_and_named_exactly() -> None:
    style = _style()

    assert style["name_ru"] == "Русский стиль"
    assert style["manual_only"] is True
    assert style["theme_optional"] is True


def test_russian_style_contract_preserves_meaning_and_uses_cyrillic() -> None:
    style = _style()
    contract = " ".join(
        [
            style["essence"],
            style["text_treatment"],
            style["palette_rule"],
            *style["constraints"],
        ]
    ).lower()

    assert "semantic bridge" in contract
    assert "preserve the user's exact subject" in contract
    assert "modern russian cyrillic" in contract
    assert "zero latin letters" in contract
    assert "5-8" in contract
    assert "60%" in contract
    assert "one primary russian decorative grammar" in contract
    assert "shirt mockup" in contract


def test_russian_style_has_one_catalog_entry() -> None:
    bank = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    matches = [style for style in bank["styles"] if style["id"] == STYLE_ID]

    assert len(matches) == 1


def test_russian_quality_hint_is_semantic_and_style_specific() -> None:
    import art_director as ad

    hint = ad._russian_style_quality_hint(STYLE_ID).lower()

    assert "смысловой контракт" in hint
    assert "semantic bridge" in hint
    assert "50-70%" in hint
    assert "5-8" in hint
    assert "кириллица" in hint
    assert ad._russian_style_quality_hint("41_city_neocrest_ru") == ""


def test_russian_build_prompt_injects_palette_and_semantic_contract() -> None:
    import art_director as ad

    prompt = ad.build_prompt(
        {
            "prompt": "A programmer whose circuit traces become ornament.",
            "style_id": STYLE_ID,
            "style_mix": "",
            "chroma": "green",
            "quote": "",
            "slogan": "",
            "type_spec": "",
            "has_human_figure": True,
        }
    ).lower()

    assert "russian style semantic fusion" in prompt
    assert "mandatory russian palette contract" in prompt
    assert "semantic bridge" in prompt
    assert "medium and bright tones cover at least 60%" in prompt


def test_russian_style_uses_premium_model_only_as_primary(monkeypatch) -> None:
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
    assert batch_print._generation_model_for_design(
        {"style_id": "01_baroque_frame", "style_mix": STYLE_ID}
    ) is None
