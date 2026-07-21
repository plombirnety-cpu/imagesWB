# -*- coding: utf-8 -*-
"""Тесты банка утверждённых стилей (docs/STYLE_BANK.json + art_director.py).
Полностью офлайн — никакой сети, арт-директор мокается через
monkeypatch(art_director.llm_provider.generate_text) — провайдер переключаемый
(см. llm_provider.py), art_director._ask_claude больше не зовёт anthropic
напрямую (как в test_text_in_image.py, test_theme_scout.py).

Покрывает:
1. Каталог docs/STYLE_BANK.json валиден и полон (12+ стилей, обязательные поля).
2. Ротация (recent_styles) — RecentStyles.snapshot()/record() и _pick_style_candidates
   не отдают недавние style_id, пока в банке есть свежие варианты.
3. design (результат art_director.make_ideas/_parse) содержит style_id/style_mix,
   build_prompt вшивает essence/text_treatment выбранного стиля в финальный промпт.
4. Обратная совместимость: если docs/STYLE_BANK.json отсутствует — старое поведение
   (style_id/style_mix пустые, никакого блока стилей в системном промпте/промпте
   картинки), без падений.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_style_bank.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

import art_director as ad  # noqa: E402

STYLE_BANK_PATH = PROJECT_ROOT / "docs" / "STYLE_BANK.json"


@pytest.fixture(autouse=True)
def _reset_style_bank_cache():
    """Каждый тест начинает с чистого модульного кэша банка стилей (art_director
    кэширует docs/STYLE_BANK.json один раз за процесс) — иначе тесты, которые
    подменяют _STYLE_BANK_PATH на несуществующий файл, портят состояние для
    остальных тестов модуля (порядок выполнения pytest не гарантирован)."""
    orig_path = ad._STYLE_BANK_PATH
    orig_cache = dict(ad._style_bank_cache)
    ad._style_bank_cache = {"loaded": False, "data": None}
    yield
    ad._STYLE_BANK_PATH = orig_path
    ad._style_bank_cache = orig_cache


def _fake_generate_text(response_text: str):
    """Заглушка llm_provider.generate_text (см. test_text_in_image.py) — возвращает
    response_text как готовый ответ арт-директора, без сети."""
    def _fn(system, user, max_tokens=1500):
        return response_text
    return _fn


# ═══════════════════════════════════════════════════════════════════════════════
# 1. docs/STYLE_BANK.json — каталог валиден и полон
# ═══════════════════════════════════════════════════════════════════════════════

def test_style_bank_file_exists_and_is_valid_json():
    assert STYLE_BANK_PATH.exists(), f"{STYLE_BANK_PATH} должен существовать"
    data = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "styles" in data and isinstance(data["styles"], list)


def test_style_bank_has_at_least_12_styles():
    data = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    assert len(data["styles"]) >= 12, (
        f"банк стилей должен содержать 12+ утверждённых стилей, "
        f"найдено {len(data['styles'])}"
    )


_REQUIRED_STYLE_FIELDS = (
    "id", "name_ru", "essence", "text_treatment", "palette_rule",
    "mood_tags", "constraints",
)


def test_style_bank_every_style_has_required_fields():
    data = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    for s in data["styles"]:
        for field in _REQUIRED_STYLE_FIELDS:
            assert field in s, f"стиль {s.get('id')!r} без обязательного поля {field!r}"
        assert s["id"].strip(), "id стиля не может быть пустым"
        assert s["essence"].strip(), f"{s['id']}: essence не может быть пустым"
        assert s["text_treatment"].strip(), f"{s['id']}: text_treatment не может быть пустым"
        assert isinstance(s["mood_tags"], list) and s["mood_tags"], (
            f"{s['id']}: mood_tags должен быть непустым списком"
        )
        assert isinstance(s["constraints"], list) and s["constraints"], (
            f"{s['id']}: constraints должен быть непустым списком"
        )


def test_style_bank_ids_are_unique():
    data = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    ids = [s["id"] for s in data["styles"]]
    assert len(ids) == len(set(ids)), f"есть дублирующиеся id: {ids}"


def test_style_bank_has_mixing_allowed_flag():
    data = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    assert data.get("mixing", {}).get("allowed") is True, (
        "mixing.allowed должен быть True — владелец разрешил миксовать стили"
    )


def test_style_bank_ring_medallion_is_marked_hybrid():
    """09 ring_medallion — единственный (или один из) обязательный ГИБРИД: кольцевой
    текст рисует код, в промпте кольцо должно быть пустым (задание лида)."""
    data = json.loads(STYLE_BANK_PATH.read_text(encoding="utf-8"))
    ring = next((s for s in data["styles"] if s["id"] == "09_ring_medallion"), None)
    assert ring is not None, "09_ring_medallion должен присутствовать в банке"
    assert ring.get("hybrid_ring_text") is True


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Ротация (RecentStyles + _pick_style_candidates)
# ═══════════════════════════════════════════════════════════════════════════════

def test_pick_style_candidates_excludes_recent_when_enough_fresh_styles_exist():
    bank = ad._load_style_bank()
    all_ids = [s["id"] for s in bank["styles"]]
    recent = all_ids[:3]  # первые 3 id считаем "недавними"
    candidates = ad._pick_style_candidates("тема без явных совпадений mood_tags", recent)
    candidate_ids = [c["id"] for c in candidates]
    assert not (set(candidate_ids) & set(recent)), (
        f"недавние стили {recent} не должны попадать в кандидаты {candidate_ids}, "
        f"пока в банке достаточно свежих вариантов"
    )


def test_pick_style_candidates_falls_back_to_recent_if_bank_too_small():
    """Если ПОЧТИ весь банк попал в recent_styles — кандидатов на выбор не должно
    остаться пустым списком (лучше дать Claude чуть повторяющихся стилей, чем ни
    одного) — см. docstring _pick_style_candidates про donабор из stale."""
    bank = ad._load_style_bank()
    all_ids = [s["id"] for s in bank["styles"]]
    recent = all_ids  # ВЕСЬ банк "недавний"
    candidates = ad._pick_style_candidates("любая тема", recent)
    assert len(candidates) > 0, "кандидаты не должны быть пустыми даже при recent=весь банк"


def test_pick_style_candidates_no_bank_returns_empty(monkeypatch):
    monkeypatch.setattr(ad, "_STYLE_BANK_PATH", PROJECT_ROOT / "docs" / "NOPE.json")
    assert ad._pick_style_candidates("тема", []) == []


def test_recent_styles_window_respects_size_limit():
    rs = ad.RecentStyles(window=3)
    for sid in ("a", "b", "c", "d", "e"):
        rs.record(sid)
    snap = rs.snapshot()
    assert len(snap) == 3
    assert snap == ["c", "d", "e"]  # deque(maxlen=3) — только последние 3


def test_recent_styles_record_ignores_empty_style_id():
    rs = ad.RecentStyles(window=5)
    rs.record("")
    rs.record(None)
    assert rs.snapshot() == []


def test_recent_styles_rotation_end_to_end_with_mocked_claude(monkeypatch):
    """Полный сценарий ротации: 3 последовательных вызова make_ideas с
    recent_styles=RecentStyles.snapshot() между ними — Claude (мокнутый) каждый раз
    получает СУЖЕННЫЙ системный промпт БЕЗ недавних id в блоке кандидатов. Проверяем,
    что кандидаты в system-промпте реально меняются, когда меняется recent_styles."""
    bank = ad._load_style_bank()
    first_id = bank["styles"][0]["id"]

    rs = ad.RecentStyles(window=5)

    # Первый вызов: recent пуст, банк должен предложить кандидатов (в т.ч., возможно,
    # first_id).
    sys_prompt_1 = ad.system_diecut("эпика мифология", rs.snapshot())
    rs.record(first_id)

    # Второй вызов: first_id теперь в recent_styles — не должен встречаться в блоке
    # БАНК СТИЛЕЙ системного промпта (может быть достаточно свежих альтернатив, банк
    # содержит 12+ стилей, кандидатов 6 — первый id почти наверняка исключится).
    sys_prompt_2 = ad.system_diecut("эпика мифология", rs.snapshot())

    assert f'id="{first_id}"' in sys_prompt_1
    assert f'id="{first_id}"' not in sys_prompt_2, (
        "недавно использованный style_id не должен снова попадать в блок кандидатов "
        "сразу на следующем вызове, пока в банке есть 12+ альтернатив"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. design содержит style_id / build_prompt вшивает essence+text_treatment
# ═══════════════════════════════════════════════════════════════════════════════

def test_parse_accepts_and_sanitizes_style_id_and_style_mix():
    bank = ad._load_style_bank()
    valid_id = bank["styles"][0]["id"]
    valid_id_2 = bank["styles"][1]["id"]
    text = json.dumps([{
        "prompt": "a hero stands", "chroma": "green",
        "style_id": valid_id, "style_mix": valid_id_2,
    }])
    designs = ad._parse(text)
    assert designs[0]["style_id"] == valid_id
    assert designs[0]["style_mix"] == valid_id_2


def test_parse_rejects_unknown_style_id():
    text = json.dumps([{
        "prompt": "a hero stands", "chroma": "green",
        "style_id": "totally_made_up_style_that_does_not_exist",
    }])
    designs = ad._parse(text)
    assert designs[0]["style_id"] == ""


def test_parse_rejects_style_mix_equal_to_style_id():
    bank = ad._load_style_bank()
    valid_id = bank["styles"][0]["id"]
    text = json.dumps([{
        "prompt": "a hero stands", "chroma": "green",
        "style_id": valid_id, "style_mix": valid_id,
    }])
    designs = ad._parse(text)
    assert designs[0]["style_id"] == valid_id
    assert designs[0]["style_mix"] == ""


def test_make_ideas_result_design_dict_contains_style_id(monkeypatch):
    """Сквозной сценарий: make_ideas (Claude мокнут) -> design dict содержит поле
    style_id — то же самое, что попадёт в *_design.json через batch_print.render_design
    (design_json_path.write_text(json.dumps(design, ...)))."""
    bank = ad._load_style_bank()
    valid_id = bank["styles"][0]["id"]
    response = json.dumps([{
        "prompt": "a warrior stands proudly", "chroma": "green",
        "slogan": "GO", "slogan_color": "red",
        "style_id": valid_id, "style_mix": "",
    }])
    monkeypatch.setattr(ad.llm_provider, "generate_text", _fake_generate_text(response))

    designs = ad.make_ideas("тестовая тема", 1, "diecut")
    assert "style_id" in designs[0]
    assert designs[0]["style_id"] == valid_id


def test_build_prompt_injects_style_essence_and_text_treatment():
    bank = ad._load_style_bank()
    style = bank["styles"][0]
    design = {
        "prompt": "A young man stands in a heroic pose.",
        "chroma": "green", "style_id": style["id"], "style_mix": "",
        "signature_props": "", "type_spec": "", "quote": "", "slogan": "",
        "name_jp": "",
    }
    prompt = ad.build_prompt(design)
    # essence — длинная строка, проверяем характерный кусок (первые полсотни символов
    # достаточно уникальны, не завязываемся на весь текст дословно).
    assert style["essence"][:60] in prompt
    assert style["text_treatment"][:60] in prompt


def test_build_prompt_ring_medallion_forces_empty_ring_instruction():
    """09_ring_medallion (hybrid_ring_text) — художественный промпт ОБЯЗАН явно
    требовать пустое кольцо без букв (текст на кольце добавляет код отдельно)."""
    design = {
        "prompt": "A warrior stands centered inside a ring.",
        "chroma": "green", "style_id": "09_ring_medallion", "style_mix": "",
        "signature_props": "", "type_spec": "", "quote": "", "slogan": "",
        "name_jp": "",
    }
    prompt = ad.build_prompt(design)
    assert "NO LETTERS" in prompt
    assert "COMPLETELY PLAIN" in prompt


def test_build_prompt_style_mix_injects_both_styles():
    bank = ad._load_style_bank()
    style_a, style_b = bank["styles"][0], bank["styles"][2]
    design = {
        "prompt": "A young man stands in a heroic pose.",
        "chroma": "green", "style_id": style_a["id"], "style_mix": style_b["id"],
        "signature_props": "", "type_spec": "", "quote": "", "slogan": "",
        "name_jp": "",
    }
    prompt = ad.build_prompt(design)
    assert style_a["essence"][:60] in prompt
    assert style_b["essence"][:60] in prompt
    assert "MIXED WITH A SECOND STYLE" in prompt


def test_build_prompt_no_style_id_adds_no_style_block():
    design = {
        "prompt": "A young man stands in a heroic pose.",
        "chroma": "green", "style_id": "", "style_mix": "",
        "signature_props": "", "type_spec": "", "quote": "", "slogan": "",
        "name_jp": "",
    }
    prompt = ad.build_prompt(design)
    assert "VISUAL STYLE" not in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Обратная совместимость — банк отсутствует
# ═══════════════════════════════════════════════════════════════════════════════

def test_missing_style_bank_file_is_graceful_not_raising(monkeypatch, capsys):
    monkeypatch.setattr(ad, "_STYLE_BANK_PATH", PROJECT_ROOT / "docs" / "DOES_NOT_EXIST.json")
    bank = ad._load_style_bank()
    assert bank is None
    captured = capsys.readouterr()
    assert "не найден" in captured.out or "не найден" in captured.err


def test_missing_style_bank_style_block_is_empty(monkeypatch):
    monkeypatch.setattr(ad, "_STYLE_BANK_PATH", PROJECT_ROOT / "docs" / "DOES_NOT_EXIST.json")
    assert ad._style_bank_block("любая тема", []) == ""


def test_missing_style_bank_system_prompt_has_no_style_block(monkeypatch):
    monkeypatch.setattr(ad, "_STYLE_BANK_PATH", PROJECT_ROOT / "docs" / "DOES_NOT_EXIST.json")
    sys_prompt = ad.system_diecut("любая тема", [])
    assert "БАНК УТВЕРЖДЁННЫХ СТИЛЕЙ" not in sys_prompt


def test_missing_style_bank_parse_falls_back_to_empty_style_id(monkeypatch):
    monkeypatch.setattr(ad, "_STYLE_BANK_PATH", PROJECT_ROOT / "docs" / "DOES_NOT_EXIST.json")
    text = json.dumps([{"prompt": "a hero", "chroma": "green", "style_id": "01_baroque_frame"}])
    designs = ad._parse(text)
    assert designs[0]["style_id"] == "", (
        "без банка style_id не может пройти валидацию (_style_by_id всегда None)"
    )


def test_missing_style_bank_build_prompt_unaffected(monkeypatch):
    monkeypatch.setattr(ad, "_STYLE_BANK_PATH", PROJECT_ROOT / "docs" / "DOES_NOT_EXIST.json")
    design = {
        "prompt": "A young man stands in a heroic pose.",
        "chroma": "green", "style_id": "", "style_mix": "",
        "signature_props": "", "type_spec": "", "quote": "", "slogan": "",
        "name_jp": "",
    }
    prompt = ad.build_prompt(design)
    assert "VISUAL STYLE" not in prompt
    assert prompt.startswith("A young man stands in a heroic pose.")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. style_pref (mega_plan_800/build_mega_plan.py) — форсированный стиль
# ═══════════════════════════════════════════════════════════════════════════════

def test_pick_style_candidates_style_pref_returns_only_that_style():
    candidates = ad._pick_style_candidates("любая тема, не совпадает по mood_tags",
                                            recent_styles=["19_tarot"],  # даже в recent
                                            style_pref="19_tarot")
    assert len(candidates) == 1
    assert candidates[0]["id"] == "19_tarot"


def test_pick_style_candidates_invalid_style_pref_falls_back_to_normal():
    candidates = ad._pick_style_candidates("тема", style_pref="not_a_real_style_id")
    assert len(candidates) > 1 or (candidates and candidates[0]["id"] != "not_a_real_style_id")


def test_style_bank_block_style_pref_forces_wording_and_id():
    block = ad._style_bank_block("любая тема", style_pref="19_tarot")
    assert 'style_id ДОЛЖЕН быть ровно "19_tarot"' in block
    assert 'id="19_tarot"' in block
    assert "БАНК УТВЕРЖДЁННЫХ СТИЛЕЙ ВЛАДЕЛЬЦА — ОБЯЗАН выбрать" not in block


def test_system_diecut_threads_style_pref_through():
    sys_prompt = ad.system_diecut("тема", [], style_pref="09_ring_medallion")
    assert 'id="09_ring_medallion"' in sys_prompt
    assert 'style_id ДОЛЖЕН быть ровно "09_ring_medallion"' in sys_prompt


def test_magazine_cover_quality_hint_is_style_specific():
    hint = ad._magazine_cover_quality_hint("34_anime_magazine_cover")
    assert "ВЕРТИКАЛЬ 2:3" in hint
    assert "70-85%" in hint
    assert "character_ref" in hint
    assert "green/blue chroma" in hint
    assert ad._magazine_cover_quality_hint("19_tarot") == ""


def test_ask_claude_includes_magazine_cover_grid(monkeypatch):
    captured = {}

    def fake_generate_text(system, user, max_tokens):
        captured["user"] = user
        return "[]"

    monkeypatch.setattr(ad.llm_provider, "generate_text", fake_generate_text)
    ad._ask_claude("Тандзиро Камадо", 1, "cutout",
                   style_pref="34_anime_magazine_cover")

    assert "ВЕРТИКАЛЬ 2:3" in captured["user"]
    assert "огромный катакана-заголовок сверху" in captured["user"]
    assert "идеально ровный выбранный green/blue chroma" in captured["user"]


def test_make_ideas_style_pref_forces_style_id_even_if_claude_ignores(monkeypatch):
    """Claude (мокнутый) возвращает ДРУГОЙ style_id (симулирует несоблюдение
    инструкции) — make_ideas обязан переписать его на style_pref (двойная
    гарантия, см. make_ideas docstring)."""
    bank = ad._load_style_bank()
    other_id = next(s["id"] for s in bank["styles"] if s["id"] != "19_tarot")
    response = json.dumps([{
        "prompt": "a mystic figure stands", "chroma": "green",
        "slogan": "FATE", "slogan_color": "purple",
        "style_id": other_id, "style_mix": "",
    }])
    monkeypatch.setattr(ad.llm_provider, "generate_text", _fake_generate_text(response))

    designs = ad.make_ideas("Лев — знак зодиака", 1, "diecut", style_pref="19_tarot")
    assert designs[0]["style_id"] == "19_tarot"


def test_make_ideas_no_style_pref_keeps_old_behavior(monkeypatch):
    """style_pref=None (дефолт) — style_id остаётся тем, что вернул Claude (старое
    поведение, обратная совместимость)."""
    bank = ad._load_style_bank()
    valid_id = bank["styles"][0]["id"]
    response = json.dumps([{
        "prompt": "a warrior stands", "chroma": "green",
        "style_id": valid_id, "style_mix": "",
    }])
    monkeypatch.setattr(ad.llm_provider, "generate_text", _fake_generate_text(response))

    designs = ad.make_ideas("тема", 1, "diecut")
    assert designs[0]["style_id"] == valid_id
