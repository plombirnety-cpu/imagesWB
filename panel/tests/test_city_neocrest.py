from __future__ import annotations

from PIL import Image

import orchestrator


STYLE_ID = "41_city_neocrest_ru"


def test_plan_tasks_city_neocrest_bypasses_franchise_and_rotates_cities(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        orchestrator.franchise_scout,
        "build_dossier",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("city style must not run franchise dossier")
        ),
    )

    tasks = orchestrator.plan_tasks(
        styles=[STYLE_ID],
        count=4,
        theme="Владивосток",
        characters="Екатеринбург",
    )

    assert [task.label for task in tasks] == [
        "Владивосток",
        "Екатеринбург",
        "Владивосток",
        "Екатеринбург",
    ]
    assert all(task.source == "city" for task in tasks)
    assert all(task.style_id == STYLE_ID for task in tasks)
    assert "BRIDGE CROWN" in tasks[0].style_brief
    assert "MONUMENTAL SUN" in tasks[1].style_brief
    assert all("zero latin letters" in task.style_brief.lower() for task in tasks)
    assert all("7-10 large readable modules" in task.style_brief for task in tasks)
    assert all("86-92%" in task.style_brief for task in tasks)
    assert all("8-9 deliberate print colours" in task.style_brief for task in tasks)
    assert all("large open chroma gaps" not in task.style_brief for task in tasks)


def test_render_city_neocrest_forces_exact_cyrillic_title_and_no_character_fields(
    tmp_path,
    monkeypatch,
) -> None:
    captured: dict = {}

    def fake_make_ideas(theme, *args, **kwargs):
        captured["theme"] = theme
        captured["style_pref"] = kwargs.get("style_pref")
        return [{
            "prompt": "generic postcard with English labels",
            "chroma": "blue",
            "style_id": "01_baroque_frame",
            "style_mix": "39_rock_band_print",
            "quote": "VLADIVOSTOK",
            "slogan": "PACIFIC GATEWAY",
            "character_en": "City Mascot",
            "title_en": "Travel Franchise",
            "signature_props": "flag",
            "has_human_figure": True,
        }]

    def fake_render_design(design, tag, outdir, **kwargs):
        captured["design"] = dict(design)
        path = outdir / f"{tag}.png"
        Image.new("RGB", (4, 4), (0, 96, 255)).save(path)
        return {"ok": True, "green": str(path), "error": None}

    monkeypatch.setattr(orchestrator.art_director, "make_ideas", fake_make_ideas)
    monkeypatch.setattr(orchestrator.batch_print, "render_design", fake_render_design)
    task = orchestrator.DesignTask(
        index=1,
        label="Владивосток",
        style_id=STYLE_ID,
        tag="01_vladivostok_city_neocrest",
        source="city",
        style_brief="ASSIGNED CITY NEOCREST VARIANT: test",
    )

    result = orchestrator._render_once(task, tmp_path)
    design = captured["design"]

    assert result["ok"] is True
    assert captured["style_pref"] == STYLE_ID
    assert "ASSIGNED CITY NEOCREST VARIANT" in captured["theme"]
    assert design["quote"] == "ВЛАДИВОСТОК"
    assert design["slogan"] == ""
    assert design["style_id"] == STYLE_ID
    assert design["style_mix"] == ""
    assert design["character_en"] == ""
    assert design["title_en"] == ""
    assert design["signature_props"] == ""
    assert design["has_human_figure"] is False
    assert design["chroma"] == "blue"
    assert "lower 22-26% plaque" in design["type_spec"]
    assert "vivid mid-tone city-colour fill" in design["type_spec"]
    assert "warm-ivory fill" not in design["type_spec"]


def test_plan_tasks_city_neocrest_requires_a_city() -> None:
    try:
        orchestrator.plan_tasks(
            styles=[STYLE_ID],
            count=1,
            theme="",
            characters="",
        )
    except ValueError as error:
        assert "город" in str(error).lower()
    else:
        raise AssertionError("empty city must be rejected")
