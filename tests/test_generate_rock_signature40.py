from PIL import Image, ImageDraw

from generate_rock_signature40 import (
    SIGNATURES,
    _remove_connected_white_edge,
    build_jobs,
    parse_only_jobs,
    prompt_for,
)


def test_signature_series_has_eight_bands_and_five_unique_concepts_each():
    assert len(SIGNATURES) == 8
    assert len(build_jobs(False)) == 40
    assert len(build_jobs(True)) == 8
    for signature in SIGNATURES.values():
        assert len(signature["concepts"]) == 5
        assert len(set(signature["concepts"])) == 5


def test_every_prompt_is_band_specific_and_print_ready():
    for slug in SIGNATURES:
        prompts = [prompt_for(slug, index) for index in range(5)]
        assert len(set(prompts)) == 5
        for prompt in prompts:
            assert SIGNATURES[slug]["canon"] in prompt
            assert "4-6 broad spot-colour ink separations" in prompt
            assert "no letters, words" in prompt.lower()
            assert "halftone dots" in prompt.lower()
            assert "top 21%" in prompt


def test_blue_chroma_is_reserved_for_blue_violet_groups():
    assert "#0000FF" in prompt_for("linkin_park", 0)
    assert "#0000FF" in prompt_for("piknik", 0)
    assert "#00FF00" in prompt_for("queen", 0)


def test_only_parser_uses_one_based_indices():
    assert parse_only_jobs("queen:1,piknik:5") == [("queen", 0), ("piknik", 4)]


def test_connected_white_rim_is_removed_but_internal_cream_survives():
    image = Image.new("RGBA", (40, 40), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, 35, 35), fill=(0, 255, 0, 0))
    draw.rectangle((15, 15, 24, 24), fill=(245, 242, 238, 255))
    cleaned = _remove_connected_white_edge(image)
    assert cleaned.getpixel((0, 0))[3] == 0
    assert cleaned.getpixel((20, 20))[3] == 255


def test_process_expected_chroma_uses_calibrated_key_and_cleans_alpha(tmp_path):
    from generate_rock_signature40 import process_expected_chroma

    path = tmp_path / "queen.png"
    image = Image.new("RGB", (80, 100), (11, 163, 77))
    draw = ImageDraw.Draw(image)
    draw.ellipse((20, 20, 60, 80), fill=(180, 35, 42))
    image.save(path)
    assert process_expected_chroma(path, "queen") == "green"
    processed = Image.open(path)
    assert processed.mode == "RGBA"
    assert processed.getchannel("A").getextrema() == (0, 255)
