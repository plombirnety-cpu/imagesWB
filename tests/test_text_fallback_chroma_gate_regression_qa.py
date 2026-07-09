# -*- coding: utf-8 -*-
"""Регресс-тест тестировщика (приёмка банка стилей + печатное качество, живая
контрольная партия 2026-07-09, out_batch/daily_2026-07-09/0008_рудеус_грейрат__
mushoku): render_design принимает text-fallback генерацию БЕЗ ПРОВЕРКИ цвета
хромакея — если фолбэк-попытка (генерация без текст-блока после провала OCR за
все основные попытки) пришла с ЧУЖИМ фоном (не тем chroma, что просил design),
код всё равно использует её как raw_img для дальнейшей вырезки.

Живой сценарий (out_batch/daily_2026-07-09/queue.jsonl, tag=0008_рудеус_грейрат__
mushoku, design["chroma"]="blue"):
  1. Все 3 основные попытки генерации дали border coverage=1.00 (фон правильный,
     синий) — НО OCR-контроль спеллинга (_verify_text) на всех трёх вернул False,
     потому что сам OCR-вызов падал HTTP 503 "This model is currently
     experiencing high demand" (Gemini overload) — _verify_text трактует сбой
     САМОГО OCR-вызова как False (задокументированное поведение), это НЕ
     реальный провал спеллинга.
  2. Ни одна попытка не прошла OCR -> render_design ушёл в честный
     text-fallback: ОДНА дополнительная генерация без текст-блока.
  3. Фолбэк-генерация пришла с БЕЛЫМ фоном вместо запрошенного синего
     (лог: "фон не хромакей: рамка ~(255, 255, 255) (dist=115.8 >= 70.0),
     ожидался blue" -> fb_cov=0.00).
  4. render_design (batch_print.py, ветка `elif expected_phrases:` в
     render_design) БЕЗУСЛОВНО присвоил `raw_img, best_cov = fb_img, fb_cov`
     — никакой проверки fb_cov против порога или против best_cov ОСНОВНЫХ
     попыток (которые были coverage=1.00, правильный синий фон!) не делается.
  5. chroma_remove.cutout_green() запущен на raw с БЕЛЫМ фоном, ключ
     автоопределения границы взял белый как хромакей -> вырезка съела куски
     светлой рубашки/кожи/волос персонажа (Rudeus Greyrat) -> итоговый
     diecut/print.png выглядит как "раздробленное на осколки" тело —
     подтверждено визуально на out_batch/daily_2026-07-09/
     0008_рудеус_грейрат__mushoku_diecut.png (checkerboard-композит).

Корневая причина: text-fallback ветка render_design НЕ уважает QC-гейт цвета
рамки (_border_chroma_coverage) — тот же гейт, что защищает ОСНОВНОЙ цикл
попыток (одиннадцатый заход, "фон не хромакей"), в фолбэк-ветке отключён по
факту (используется fb_img "как есть", независимо от fb_cov).

Запуск:
    cd print-factory-nb && python -m pytest tests/test_text_fallback_chroma_gate_regression_qa.py -v
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


def _design(**overrides) -> dict:
    base = {
        "prompt": "A young mage stands confidently, mana orbs orbiting his hands.",
        "chroma": "blue",
        "slogan": "MANA RUNS DEEP",
        "slogan_color": "white",
        "kana": "",
        "character_en": "",
        "title_en": "",
        "signature_props": "",
        "text_mode": "punch",
        "text_modes_v3": [],
        "quote": "",
        "name_jp": "ルーデウス",
        "mood": "",
        "type_spec": "bold heraldic lettering placed along the bottom",
    }
    base.update(overrides)
    return base


def _blue_frame_img(w=220, h=220) -> Image.Image:
    """Правильный синий хромакей-фон (то, что реально просил design['chroma']) —
    имитирует 3 основные попытки живого бага (border coverage=1.00 каждая)."""
    img = Image.new("RGB", (w, h), (0, 71, 255))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.15, h * 0.1, w * 0.85, h * 0.95], fill=(210, 170, 130))
    return img


def _white_frame_img(w=220, h=220) -> Image.Image:
    """Белый фон — имитирует РЕАЛЬНУЮ фолбэк-генерацию живого бага (0008): вместо
    запрошенного chroma='blue' модель нарисовала белый фон, border coverage=0.00
    (цветовой гейт _border_chroma_coverage должен отсеять такой кадр)."""
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.15, h * 0.1, w * 0.85, h * 0.95], fill=(210, 170, 130))
    return img


def _fake_gen_image_factory(images: list):
    calls = {"n": 0}

    def _fake(prompt, seed=None, model=None, reference=None):
        img = images[min(calls["n"], len(images) - 1)]
        calls["n"] += 1
        return img

    _fake.calls = calls
    return _fake


def test_text_fallback_rejects_wrong_chroma_background_instead_of_using_it_blindly(
        tmp_path, monkeypatch):
    """РЕГРЕСС (нашёл тестировщик, живая партия 2026-07-09, out_batch/
    daily_2026-07-09/0008_рудеус_грейрат__mushoku_diecut.png): когда все основные
    попытки генерации имеют ПРАВИЛЬНЫЙ хромакей-фон (border coverage=1.00) и
    проваливаются ТОЛЬКО по OCR (например из-за временного 503 у самой OCR-
    модели, не реальной ошибки спеллинга), а text-fallback генерация (доп.
    попытка БЕЗ текст-блока) возвращает КАРТИНКУ С ДРУГИМ ФОНОМ (не тем chroma,
    что просил design) — render_design НЕ ДОЛЖЕН слепо принимать эту фолбэк-
    картинку с coverage=0.00 как финальный raw, если хотя бы одна из ОСНОВНЫХ
    попыток имела нормальный фон (coverage=1.00). Итоговый raw_img обязан иметь
    border coverage хромакея того же порядка, что и лучшая основная попытка —
    иначе вырезка (chroma_remove.cutout_green) неизбежно повреждает фигуру,
    принимая чужой цвет фона за хромакей.

    ДО фикса: `render_design` в ветке `elif expected_phrases:` безусловно
    присваивает `raw_img, best_cov = fb_img, fb_cov` независимо от fb_cov —
    тест падает, потому что итоговый result["coverage"] оказывается 0.00, хотя
    среди попыток БЫЛА картинка с coverage=1.00 (просто без текста в фолбэке).
    ПОСЛЕ ожидаемого фикса — фолбэк с явно неверным хромакей-фоном либо
    отклоняется в пользу лучшей основной попытки (даже без текста), либо сам
    фолбэк-путь проверяется тем же цветовым гейтом с ретраем."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")

    good_bg = _blue_frame_img()
    bad_bg_fallback = _white_frame_img()

    # 2 основные попытки (timeout_retries=1 -> 1+1=2 попыток) — ОБЕ с правильным
    # синим фоном (border coverage=1.00), но ОБЕ проваливают OCR (симулируем
    # 503-подобный сбой OCR-сервиса — _verify_text всегда False независимо от
    # картинки) -> должен сработать text-fallback (3-й вызов generate_image).
    fake_gen = _fake_gen_image_factory([good_bg, good_bg, bad_bg_fallback])
    monkeypatch.setattr(batch_print.providers, "generate_image", fake_gen)
    monkeypatch.setattr(batch_print, "_verify_text", lambda image, phrases: False)
    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        lambda *a, **k: None)

    d = _design()
    res = batch_print.render_design(d, "0008", tmp_path, timeout_retries=1)

    assert res["ok"] is True
    assert res["text_fallback"] is True
    # Ровно 3 попытки: 2 основные (OCR не сошёлся на обеих) + 1 фолбэк-генерация.
    assert fake_gen.calls["n"] == 3

    # ГЛАВНАЯ ПРОВЕРКА РЕГРЕССА: итоговый coverage НЕ должен быть 0.00, если
    # среди попыток (пусть и без текста) была картинка с правильным хромакей-
    # фоном (coverage=1.00). Живой баг: result["coverage"] был 0.00 (фолбэк с
    # белым фоном принят слепо), хотя 2 основные попытки имели coverage=1.00.
    assert res["coverage"] >= 0.90, (
        f"итоговый border coverage={res['coverage']:.2f} — фолбэк-генерация с "
        f"НЕВЕРНЫМ хромакей-фоном (coverage=0.00) была использована как raw "
        f"вместо основной попытки с правильным фоном (coverage=1.00), хотя "
        f"такая попытка существовала. Это воспроизводит живой брак "
        f"out_batch/daily_2026-07-09/0008_рудеус_грейрат__mushoku_diecut.png — "
        f"вырезка на чужом цвете фона повреждает фигуру персонажа."
    )

    # Косвенная проверка того же самого через сохранённый raw.png: реальный
    # фон итогового raw обязан быть БЛИЗКО к синему хромакею (эталон
    # design["chroma"]="blue"), а не белым (то, что вернул баганый фолбэк).
    raw_img = Image.open(res["raw"]).convert("RGB")
    corner = np.array(raw_img)[2, 2]
    dist_to_white = float(np.linalg.norm(corner.astype(float) - np.array([255, 255, 255])))
    assert dist_to_white > 60.0, (
        f"итоговый raw.png имеет почти БЕЛЫЙ угол кадра {tuple(corner)} — это "
        f"фон бракованной фолбэк-генерации (design['chroma']='blue' не "
        f"уважается), воспроизводит живой баг 0008_рудеус_грейрат__mushoku"
    )



# ═══════════════════════════════════════════════════════════════════════════════
# art_director._parse — quote-санация калечит НЕ-ASCII буквы (акцентированную
# латиницу: é, ñ, ü и т.п.) в легитимных не-английских цитатах
# ═══════════════════════════════════════════════════════════════════════════════

def _wrap_json_array(obj_json: str) -> str:
    return f"[{obj_json}]"


def test_parse_quote_preserves_accented_latin_letters_not_just_ascii():
    """РЕГРЕСС (нашёл тестировщик, живая партия 2026-07-09, out_batch/
    daily_2026-07-09/0005_by_индия___портрет_певиц): design["quote"] пришёл от
    Claude как 'La reina del género urbano' (легитимная испанская цитата — design
    описывает By India, испаноязычную артистку; сам системный промпт art_director
    ЯВНО разрешает quote = "каноничная реплика персонажа", без ограничения языком/
    алфавитом). art_director._parse санирует quote регулярным выражением
    `re.sub(r"[^A-Za-z0-9 !?'\\-]", "", ...)`, которое пропускает ТОЛЬКО голый
    ASCII — буква 'é' (и любая другая акцентированная латиница/не-ASCII) вырезается
    молча. Итоговый design["quote"] на живой генерации оказался
    'La reina del gnero urbano' (буква пропала, испанское слово 'género'
    превратилось в бессмысленное 'gnero').

    Этот испорченный quote затем используется КАК ТОЧНАЯ инструкция спеллинга для
    генерации (art_director._exact_spelling_phrase -> "Spell the phrase EXACTLY,
    letter by letter: La reina del gnero urbano.") — модель послушно нарисовала
    ИМЕННО испорченный текст (подтверждено на raw.png живой генерации:
    'La reina del gnero urbano', без 'é', см. out_batch/daily_2026-07-09/
    0005_by_индия___портрет_певиц_raw.png). ВТОРИЧНЫЙ эффект того же бага:
    OCR-контроль спеллинга (_verify_text) сравнивает транскрипт с этой же
    испорченной ожидаемой фразой — попытка генерации, где модель ПРАВИЛЬНО
    написала 'género' (с é), проваливает OCR-сравнение (несовпадение с
    испорченным эталоном) и уходит в ненужный retry, а порченная версия
    ('gnero') проходит проверку как "верная".

    ДО фикса: 'é' вырезается -> тест падает. ПОСЛЕ ожидаемого фикса —
    санация quote должна сохранять акцентированную латиницу (например через
    unicode-категорию буквы вместо жёсткого ASCII-only regex), запрещая только
    реально опасные символы (управляющие символы, кавычки, разметку)."""
    raw = _wrap_json_array(
        '{"prompt": "A pop star performs on a spotlit stage.", "chroma": "green", '
        '"slogan": "BY INDIA", "slogan_color": "yellow", "kana": "", '
        '"character_en": "", "title_en": "", '
        '"quote": "La reina del g\\u00e9nero urbano"}'
    )
    designs = art_director._parse(raw)
    assert len(designs) == 1
    quote = designs[0]["quote"]
    assert "género" in quote, (
        f"design['quote']={quote!r} — буква 'é' пропала при санации в "
        f"art_director._parse (regex [^A-Za-z0-9 !?'\\-] вырезает всю "
        f"акцентированную латиницу), воспроизводит живой брак "
        f"out_batch/daily_2026-07-09/0005_by_индия___портрет_певиц_raw.png "
        f"('La reina del género urbano' -> 'La reina del gnero urbano')"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
