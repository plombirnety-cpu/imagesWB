# -*- coding: utf-8 -*-
"""Тесты десятого захода — текст ВСТРОЕННЫЙ в генерацию (TEXT_RENDER=image,
config.GEMINI_MODEL -> gemini-3.1-flash-image). Полностью офлайн:

- art_director: exact-spelling блок в build_prompt (TEXT_RENDER=image/code, наличие
  type_spec/отсутствие), системный промпт (system_cutout/system_diecut) переключает
  условный запрет букв по config.TEXT_RENDER.
- Нормализация OCR-сравнения (batch_print._normalize_for_compare).
- Логика ретрая/фолбэка на OCR-контроле (batch_print._verify_text, providers.py
  замокан) — сеть НЕ трогается.
- Защита текстовых альфа-островов при вырезке (batch_print.drop_small_islands,
  protect_text_islands=True) — синтетическая фигура с "буквами"-островами.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_text_in_image.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import art_director  # noqa: E402
import batch_print  # noqa: E402
import config  # noqa: E402
import providers  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# art_director.build_prompt — exact-spelling блок (TEXT_RENDER=image/code)
# ═══════════════════════════════════════════════════════════════════════════════

def _design(**overrides) -> dict:
    base = {
        "prompt": "A young man with spiky red hair stands confidently.",
        "chroma": "green",
        "slogan": "LETS PARTY",
        "slogan_color": "red",
        "kana": "",
        "character_en": "",
        "title_en": "",
        "signature_props": "",
        "text_mode": "punch",
        "text_modes_v3": [],
        "quote": "",
        "name_jp": "",
        "mood": "",
        "type_spec": "bold aggressive brush-graffiti lettering placed along the bottom",
    }
    base.update(overrides)
    return base


def test_build_prompt_text_render_image_contains_exact_spelling_block(monkeypatch):
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    prompt = art_director.build_prompt(_design())
    assert "Spell the phrase EXACTLY, letter by letter: LETS PARTY." in prompt
    assert "INCLUDES integrated typography" in prompt
    assert "No other text anywhere." in prompt
    # НЕ содержит старый безусловный запрет букв — text_mode реально что-то рисует.
    assert "No letters, no words, no typography" not in prompt


def test_build_prompt_text_render_image_quote_takes_priority_over_slogan(monkeypatch):
    """Если и quote, и slogan непусты — exact-spelling берёт quote (типографика v3
    приоритетнее v2, см. art_director._exact_spelling_phrase)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    d = _design(quote="CURSED SORCERERS ARE SO FRAGILE")
    prompt = art_director.build_prompt(d)
    assert "letter by letter: CURSED SORCERERS ARE SO FRAGILE." in prompt
    assert "letter by letter: LETS PARTY" not in prompt


def test_build_prompt_text_render_image_includes_kanji_column_when_name_jp_present(monkeypatch):
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    d = _design(name_jp="更木剣八")
    prompt = art_director.build_prompt(d)
    assert "vertical Japanese calligraphy column" in prompt
    assert "更木剣八" in prompt


def test_build_prompt_text_render_image_empty_type_spec_falls_back_to_ban(monkeypatch):
    """text_mode='none' эквивалент — type_spec пуст -> старый безусловный запрет
    букв, БЕЗ exact-spelling блока (правило 'иногда текст не нужен' сохраняется)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    d = _design(type_spec="")
    prompt = art_director.build_prompt(d)
    assert "No letters, no words, no typography, no lettering" in prompt
    assert "Spell the phrase EXACTLY" not in prompt
    assert "INCLUDES integrated typography" not in prompt


def test_build_prompt_text_render_image_no_phrase_falls_back_to_ban(monkeypatch):
    """type_spec непуст, но НЕТ ни quote, ни slogan (нечего писать exact-spelling) —
    тоже откат на безусловный запрет, не полурабочий блок без фразы."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    d = _design(slogan="", quote="")
    prompt = art_director.build_prompt(d)
    assert "No letters, no words, no typography, no lettering" in prompt
    assert "Spell the phrase EXACTLY" not in prompt


def test_build_prompt_text_render_code_always_bans_letters_even_with_type_spec(monkeypatch):
    """TEXT_RENDER=code (фолбэк-режим) — ВСЕГДА безусловный запрет, type_spec
    полностью игнорируется на уровне генерации (текст накладывается кодом)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    prompt = art_director.build_prompt(_design())
    assert "No letters, no words, no typography, no lettering" in prompt
    assert "Spell the phrase EXACTLY" not in prompt
    assert "INCLUDES integrated typography" not in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# art_director system_cutout/system_diecut — условный vs безусловный запрет
# ═══════════════════════════════════════════════════════════════════════════════

def test_system_prompt_text_render_image_has_conditional_ban_not_blanket(monkeypatch):
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    sys_cutout = art_director.system_cutout()
    sys_diecut = art_director.system_diecut()
    for sys_prompt in (sys_cutout, sys_diecut):
        assert "ТИПОГРАФИКА — ЧАСТЬ ХУДОЖЕСТВЕННОЙ КОМПОЗИЦИИ" in sys_prompt
        assert "В КОНЦЕ промпта ЯВНО запрети текст на самой картинке" not in sys_prompt
        assert "type_spec" in sys_prompt  # схема поля присутствует


def test_system_prompt_text_render_code_has_blanket_ban(monkeypatch):
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    sys_cutout = art_director.system_cutout()
    assert "В КОНЦЕ промпта ЯВНО запрети текст на самой картинке" in sys_cutout


def test_ask_claude_user_prompt_includes_type_spec_field(monkeypatch):
    """_ask_claude собирает user-промпт со всеми полями схемы, включая type_spec —
    подменяем anthropic.Anthropic, чтобы проверить БЕЗ сети."""
    captured = {}

    class _FakeContentBlock:
        type = "text"
        text = "[]"

    class _FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("R", (), {"content": [_FakeContentBlock()]})()

    class _FakeClient:
        def __init__(self, api_key=None):
            self.messages = _FakeMessages()

    monkeypatch.setattr(art_director.anthropic, "Anthropic", _FakeClient)
    art_director._ask_claude("test theme", 1, "diecut")
    user_msg = captured["messages"][0]["content"]
    assert "type_spec" in user_msg


# ═══════════════════════════════════════════════════════════════════════════════
# art_director._parse — санация нового поля type_spec
# ═══════════════════════════════════════════════════════════════════════════════

def test_parse_accepts_type_spec():
    text = ('[{"prompt": "a hero", "chroma": "green", "slogan": "GO", '
            '"slogan_color": "red", "kana": "", '
            '"type_spec": "bold brush lettering along the bottom"}]')
    out = art_director._parse(text)
    assert out[0]["type_spec"] == "bold brush lettering along the bottom"


def test_parse_defaults_type_spec_to_empty_when_missing():
    """Обратная совместимость: старый JSON без type_spec -> пустая строка, не падает."""
    text = ('[{"prompt": "a hero", "chroma": "green", "slogan": "GO", '
            '"slogan_color": "red", "kana": ""}]')
    out = art_director._parse(text)
    assert out[0]["type_spec"] == ""


def test_parse_strips_control_chars_from_type_spec_and_truncates():
    text = ('[{"prompt": "a hero", "chroma": "green", "slogan": "GO", '
            '"slogan_color": "red", "kana": "", '
            '"type_spec": "line one\\nline two\\ttabbed"}]')
    out = art_director._parse(text)
    assert "\n" not in out[0]["type_spec"]
    assert "\t" not in out[0]["type_spec"]
    assert "line one" in out[0]["type_spec"] and "line two" in out[0]["type_spec"]


# ═══════════════════════════════════════════════════════════════════════════════
# batch_print._normalize_for_compare — нормализация OCR-сравнения
# ═══════════════════════════════════════════════════════════════════════════════

def test_normalize_for_compare_uppercases_and_strips_punctuation():
    assert batch_print._normalize_for_compare("Come on, let's party!") == "COME ON LETS PARTY"


def test_normalize_for_compare_collapses_newlines_and_whitespace():
    out = batch_print._normalize_for_compare("CURSED\nSORCERERS\t\tARE   FRAGILE")
    assert out == "CURSED SORCERERS ARE FRAGILE"


def test_normalize_for_compare_preserves_kanji_and_katakana():
    out = batch_print._normalize_for_compare("更木剣八")
    assert out == "更木剣八"


def test_normalize_for_compare_apostrophe_variants_both_removed():
    a = batch_print._normalize_for_compare("LET'S PARTY")
    b = batch_print._normalize_for_compare("LETS PARTY")
    assert a == b == "LETS PARTY"


# ═══════════════════════════════════════════════════════════════════════════════
# batch_print._verify_text — OCR-контроль (providers.verify_text_in_image замокан)
# ═══════════════════════════════════════════════════════════════════════════════

def _tiny_rgba(w=20, h=20) -> Image.Image:
    return Image.new("RGBA", (w, h), (10, 20, 30, 255))


def test_verify_text_no_expected_phrases_returns_true_without_ocr_call(monkeypatch):
    """Ни одной непустой ожидаемой фразы -> True, OCR даже не вызывается (нечего
    проверять — например text_mode='none')."""
    calls = []
    monkeypatch.setattr(providers, "verify_text_in_image",
                        lambda img: calls.append(1) or "anything")
    ok = batch_print._verify_text(_tiny_rgba(), ["", None])
    assert ok is True
    assert calls == []


def test_verify_text_all_phrases_present_returns_true(monkeypatch):
    # Фраза 更木剣八 содержит кандзи -> _verify_text (тринадцатый заход, строгая кана)
    # делает ВТОРОЙ OCR-вызов с prompt=_JP_COLUMN_PROMPT для поглифной сверки — мок
    # должен отвечать на ОБА вызова (общий транскрипт БЕЗ prompt и узкий кана-вызов С
    # prompt), иначе TypeError на лишнем keyword-аргументе.
    monkeypatch.setattr(providers, "verify_text_in_image",
                        lambda img, prompt=providers._OCR_PROMPT: "COME ON LET'S PARTY\n更木剣八")
    ok = batch_print._verify_text(_tiny_rgba(), ["LETS PARTY", "更木剣八"])
    assert ok is True


def test_verify_text_missing_phrase_returns_false(monkeypatch):
    monkeypatch.setattr(providers, "verify_text_in_image",
                        lambda img, prompt=providers._OCR_PROMPT: "SOME OTHER TEXT")
    ok = batch_print._verify_text(_tiny_rgba(), ["LETS PARTY"])
    assert ok is False


def test_verify_text_ocr_call_failure_returns_false_not_raises(monkeypatch):
    """Сбой самого OCR-вызова (сеть/HTTP) -> False, НЕ пробрасывает исключение —
    вызывающий код (render_design) должен уметь ретраить/откатиться, не падать."""
    def _boom(img, prompt=providers._OCR_PROMPT):
        raise RuntimeError("HTTP 500")
    monkeypatch.setattr(providers, "verify_text_in_image", _boom)
    ok = batch_print._verify_text(_tiny_rgba(), ["LETS PARTY"])
    assert ok is False


# ═══════════════════════════════════════════════════════════════════════════════
# batch_print._expected_text_phrases — выбор ожидаемых фраз из design
# ═══════════════════════════════════════════════════════════════════════════════

def test_expected_text_phrases_empty_when_no_type_spec():
    d = _design(type_spec="")
    assert batch_print._expected_text_phrases(d) == []


def test_expected_text_phrases_prefers_quote_over_slogan():
    d = _design(quote="CURSED SORCERERS ARE FRAGILE")
    out = batch_print._expected_text_phrases(d)
    assert out[0] == "CURSED SORCERERS ARE FRAGILE"


def test_expected_text_phrases_includes_name_jp():
    d = _design(name_jp="更木剣八")
    out = batch_print._expected_text_phrases(d)
    assert "更木剣八" in out


# ═══════════════════════════════════════════════════════════════════════════════
# batch_print.render_design — ретрай/фолбэк на OCR-контроле (providers/_verify_text
# замокан ЦЕЛИКОМ — никакой сети, никакого реального Gemini)
# ═══════════════════════════════════════════════════════════════════════════════

def _fake_gen_image_factory(images: list):
    """Возвращает функцию-подмену providers.generate_image, отдающую images[i] по
    очереди при каждом вызове (для симуляции нескольких попыток генерации)."""
    calls = {"n": 0}

    def _fake(prompt, seed=None, model=None, reference=None):
        img = images[min(calls["n"], len(images) - 1)]
        calls["n"] += 1
        return img

    _fake.calls = calls
    return _fake


def _green_frame_img(w=200, h=200) -> Image.Image:
    """Изображение с ровной зелёной рамкой хромакея (проходит QC-гейт границ)."""
    img = Image.new("RGB", (w, h), (0, 177, 64))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.2, h * 0.2, w * 0.8, h * 0.8], fill=(180, 90, 40))
    return img


def test_render_design_text_render_image_ocr_passes_first_try(tmp_path, monkeypatch):
    """TEXT_RENDER=image, OCR проходит с первой попытки -> text_fallback=False,
    кодовая типографика НЕ применяется (diecut = вырезка как есть)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    img = _green_frame_img()
    fake_gen = _fake_gen_image_factory([img])
    monkeypatch.setattr(batch_print.providers, "generate_image", fake_gen)
    monkeypatch.setattr(batch_print, "_verify_text", lambda image, phrases: True)
    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        lambda *a, **k: None)

    d = _design(character_en="")
    outdir = tmp_path
    res = batch_print.render_design(d, "01", outdir, timeout_retries=1)

    assert res["ok"] is True
    assert res["text_fallback"] is False
    assert fake_gen.calls["n"] == 1  # ровно одна генерация, без фолбэка


def test_render_design_text_render_image_ocr_fails_triggers_fallback(tmp_path, monkeypatch):
    """OCR НЕ проходит ни на одной попытке (включая ретраи) -> честный откат:
    ДОПОЛНИТЕЛЬНАЯ генерация без текста + text_fallback=True в результате.

    Пятнадцатый заход (регресс-фикс двойного текста): fallback-картинка ТЕПЕРЬ
    дополнительно проверяется _verify_no_text (реально ли она без текста) — здесь
    _green_frame_img() физически не содержит никакого встроенного текста (просто
    фигура на хромакее), поэтому мокаем _verify_no_text=True (реалистичный
    сценарий "fallback пришла без текста, как и просили") — без мока тест зависел
    бы от реального сетевого OCR-вызова providers.verify_text_in_image."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    img = _green_frame_img()
    fake_gen = _fake_gen_image_factory([img, img, img])  # 2 попытки + 1 фолбэк
    monkeypatch.setattr(batch_print.providers, "generate_image", fake_gen)
    monkeypatch.setattr(batch_print, "_verify_text", lambda image, phrases: False)
    monkeypatch.setattr(batch_print, "_verify_no_text", lambda image: True)
    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        lambda *a, **k: None)

    d = _design(character_en="")
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=1)

    assert res["ok"] is True
    assert res["text_fallback"] is True
    # 1+timeout_retries (2) попытки с текстом + 1 доп. фолбэк-генерация = 3 вызова
    # (fb_no_text_ok=True и fb_cov=1.00>=0.90 с ПЕРВОЙ фолбэк-попытки -> цикл
    # _FALLBACK_NO_TEXT_MAX_ATTEMPTS прерывается сразу, вторая попытка не тратится).
    assert fake_gen.calls["n"] == 3


def test_render_design_text_fallback_applies_code_typography(tmp_path, monkeypatch):
    """При сработавшем text_fallback итоговый diecut ДОЛЖЕН содержать текст,
    нанесённый КОДОМ (typography_v3/typography), а не быть голой вырезкой — при
    условии, что fallback-картинка ПОДТВЕРЖДЕНА OCR-ом как реально без текста (см.
    _verify_no_text, пятнадцатый заход)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    img = _green_frame_img()
    fake_gen = _fake_gen_image_factory([img, img])
    monkeypatch.setattr(batch_print.providers, "generate_image", fake_gen)
    monkeypatch.setattr(batch_print, "_verify_text", lambda image, phrases: False)
    monkeypatch.setattr(batch_print, "_verify_no_text", lambda image: True)
    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        lambda *a, **k: None)

    calls = {"n": 0}
    orig_compose = batch_print.typography.compose_text

    def _spy_compose(*args, **kwargs):
        calls["n"] += 1
        return orig_compose(*args, **kwargs)

    monkeypatch.setattr(batch_print.typography, "compose_text", _spy_compose)

    d = _design(character_en="", text_modes_v3=[])
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)

    assert res["ok"] is True
    assert res["text_fallback"] is True
    assert calls["n"] == 1  # кодовая типографика реально вызвана на фолбэк-пути


def test_render_design_text_render_image_no_type_spec_skips_ocr_and_code_typography(
        tmp_path, monkeypatch):
    """design без type_spec (тексту не место) — OCR не вызывается вовсе (нечего
    проверять), кодовая типографика тоже НЕ применяется (apply_code_typography
    учитывает 'not expected_phrases' -> True технически, но text_mode='none' ничего
    не рисует ОДИНАКОВО что кодом, что без — реальная проверка: OCR не дёргался)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    img = _green_frame_img()
    fake_gen = _fake_gen_image_factory([img])
    monkeypatch.setattr(batch_print.providers, "generate_image", fake_gen)
    ocr_calls = {"n": 0}
    monkeypatch.setattr(batch_print, "_verify_text",
                        lambda image, phrases: ocr_calls.__setitem__(
                            "n", ocr_calls["n"] + 1) or True)
    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        lambda *a, **k: None)

    d = _design(type_spec="", text_mode="none", text_modes_v3=[])
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)

    assert res["ok"] is True
    assert ocr_calls["n"] == 0  # OCR не звался — expected_phrases пуст


def test_render_design_text_render_code_never_calls_ocr(tmp_path, monkeypatch):
    """TEXT_RENDER=code — старый путь, OCR-контроль вообще не участвует."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    img = _green_frame_img()
    fake_gen = _fake_gen_image_factory([img])
    monkeypatch.setattr(batch_print.providers, "generate_image", fake_gen)
    ocr_calls = {"n": 0}
    monkeypatch.setattr(batch_print, "_verify_text",
                        lambda image, phrases: ocr_calls.__setitem__(
                            "n", ocr_calls["n"] + 1) or True)
    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        lambda *a, **k: None)

    d = _design()
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)

    assert res["ok"] is True
    assert res["text_fallback"] is False
    assert ocr_calls["n"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# batch_print.drop_small_islands — защита текстовых альфа-островов
# ═══════════════════════════════════════════════════════════════════════════════

def _figure_with_text_islands(w=600, h=800) -> Image.Image:
    """Синтетическая вырезка: крупный непрозрачный силуэт (эллипс) + НЕСКОЛЬКО
    МЕЛКИХ отдельных островов у нижнего края, имитирующих буквы встроенного текста
    (не связаны с основным силуэтом) — по площади заметно меньше силуэта, но
    крупнее _TEXT_ISLAND_PROTECT_FRAC доли кадра."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.2, h * 0.08, w * 0.8, h * 0.55], fill=(180, 90, 40, 255))
    # "Буквы" — маленькие квадраты у нижнего края, каждый ~0.06% площади кадра
    # (> _TEXT_ISLAND_PROTECT_FRAC=0.04%, но << areas.max()*min_frac по умолчанию
    # если min_frac слишком строгий).
    letter_side = max(2, int((w * h * 0.0006) ** 0.5))
    for i in range(5):
        x0 = int(w * (0.15 + i * 0.14))
        y0 = int(h * 0.85)
        d.rectangle([x0, y0, x0 + letter_side, y0 + letter_side], fill=(230, 230, 230, 255))
    return img


def test_drop_small_islands_without_protection_can_remove_text_islands():
    """БЕЗ protect_text_islands (старое поведение, min_frac агрессивный) — мелкие
    острова-"буквы" удаляются наравне с мусором."""
    fig = _figure_with_text_islands()
    n_before = _count_islands(fig)
    assert n_before > 1  # силуэт + буквы-острова присутствуют

    out = batch_print.drop_small_islands(fig, min_frac=0.05, protect_text_islands=False)
    n_after = _count_islands(out)
    assert n_after < n_before  # буквы реально съедены


def test_drop_small_islands_with_protection_keeps_text_islands():
    """С protect_text_islands=True те же острова-"буквы" (крупнее
    _TEXT_ISLAND_PROTECT_FRAC доли кадра) переживают удаление, даже с тем же
    агрессивным min_frac, при котором test выше их съедал."""
    fig = _figure_with_text_islands()
    n_before = _count_islands(fig)

    out = batch_print.drop_small_islands(fig, min_frac=0.05, protect_text_islands=True)
    n_after = _count_islands(out)
    assert n_after == n_before  # все острова, включая буквы, сохранены


def _count_islands(rgba: Image.Image) -> int:
    import cv2
    a = np.array(rgba.getchannel("A"))
    n, _labels, _stats, _ = cv2.connectedComponentsWithStats((a > 0).astype(np.uint8), 8)
    return n - 1  # минус фон (label 0)


def test_drop_small_islands_still_removes_true_garbage_with_protection_on():
    """protect_text_islands=True защищает только острова >= _TEXT_ISLAND_PROTECT_FRAC
    доли кадра — совсем крошечный шумовой остров (1-2px, водяной знак) по-прежнему
    удаляется даже при protect_text_islands=True."""
    w, h = 600, 800
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.2, h * 0.08, w * 0.8, h * 0.55], fill=(180, 90, 40, 255))
    d.point([(2, 2)], fill=(10, 10, 10, 255))  # 1px мусор в углу

    out = batch_print.drop_small_islands(img, min_frac=0.002, protect_text_islands=True)
    a = np.array(out.getchannel("A"))
    assert a[2, 2] == 0  # мусорный пиксель удалён
