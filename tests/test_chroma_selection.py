# -*- coding: utf-8 -*-
"""Регрессия выбора green/blue chroma до платной генерации изображения."""
import json

import art_director


def _parse_chroma(prompt: str, chroma: str = "green") -> str:
    payload = json.dumps([{
        "prompt": prompt,
        "chroma": chroma,
        "slogan": "TEST",
        "slogan_color": "orange",
        "kana": "",
    }])
    return art_director._parse(payload)[0]["chroma"]


def test_green_background_wording_does_not_force_blue():
    """Живой дефект style-34: сам prose про green background не является частью героя."""
    prompt = (
        "A silver-haired warrior on a perfectly uniform bright green chroma-key "
        "background extending to every edge."
    )
    assert _parse_chroma(prompt) == "green"


def test_inosuke_green_eyes_do_not_sacrifice_blue_character_details():
    """Мелкие зелёные глаза не должны съедать синие волосы/серый мех Иноске."""
    prompt = (
        "Inosuke has bright green eyes, wild black hair fading to bright blue tips, "
        "and a grey boar mask. The composition floats on a bright green chroma-key "
        "background."
    )
    assert _parse_chroma(prompt) == "green"


def test_explicit_green_background_wins_over_contradictory_blue_json_field():
    """Даже несогласованный ответ LLM blue+green-background должен дать green."""
    prompt = (
        "Inosuke has bright green eyes and blue-tipped hair. The composition floats "
        "on a bright green chroma-key background."
    )
    assert _parse_chroma(prompt, chroma="blue") == "green"


def test_large_green_garment_still_forces_blue_for_tanjiro():
    prompt = "Tanjiro wears his signature green-and-black checkered haori."
    assert _parse_chroma(prompt) == "blue"


def test_green_skin_and_large_green_effect_still_force_blue():
    assert _parse_chroma("A goblin hero with vivid green skin.") == "blue"
    assert _parse_chroma("A warrior surrounded by a large emerald energy aura.") == "blue"


def test_deliberate_blue_choice_is_preserved():
    assert _parse_chroma("A silver-haired warrior.", chroma="blue") == "blue"
