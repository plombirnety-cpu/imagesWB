from PIL import Image

from generate_rock_top40 import SERIES, VARIANTS, build_jobs, compose_exact_wordmark, parse_only_jobs, prompt_for


def test_full_series_is_eight_groups_by_five_variants():
    jobs = build_jobs(False)
    assert len(SERIES) == 8
    assert len(VARIANTS) == 5
    assert len(jobs) == 40
    assert len(set(jobs)) == 40
    assert parse_only_jobs("queen:1,korol_i_shut:2") == [("queen", 0), ("korol_i_shut", 1)]


def test_every_prompt_has_anti_ai_print_contract_and_exact_name():
    for slug, group in SERIES.items():
        for index in range(5):
            prompt = prompt_for(group, index)
            assert group["name"] not in prompt
            assert "exactly 3-5 broad flat ink masses" in prompt
            assert "no photorealism" in prompt.lower()
            assert "NO HALFTONE" in prompt
            assert "7% chroma moat" in prompt
            assert "render absolutely no letters" in prompt.lower()
            assert "top 22%" in prompt


def test_lineup_uses_blue_chroma_and_other_variants_use_green():
    queen = SERIES["queen"]
    assert "#0000FF" in prompt_for(queen, 1)
    for index in (0, 2, 3, 4):
        assert "#00FF00" in prompt_for(queen, index)


def test_local_wordmark_changes_reserved_top_zone_for_latin_and_cyrillic():
    for slug in ("queen", "korol_i_shut"):
        base = Image.new("RGB", (848, 1264), "#00FF00")
        composed = compose_exact_wordmark(base, SERIES[slug])
        crop = composed.crop((0, 0, 848, int(1264 * 0.22)))
        assert len(set(crop.getdata())) > 4


def test_all_cyrillic_groups_use_real_cyrillic_font():
    for slug in ("korol_i_shut", "kino", "aria", "piknik"):
        assert SERIES[slug]["font"] == "FreeSansBold.ttf"
