# -*- coding: utf-8 -*-
"""Регресс-тест тестировщика (контрольная живая партия daily_prints.py --limit 8
--skip-collect, 2026-07-09, ПОСЛЕ пятнадцатого захода/фикса text-fallback chroma
gate): render_design накладывает КОДОВУЮ типографику (typography_v3/typography)
ПОВЕРХ картинки, которая уже содержит ВСТРОЕННЫЙ художественный текст, если эта
картинка была получена через text-fallback ветку — приводит к двойному,
нечитаемому тексту на итоговом diecut/print.png.

ИМЯ ФАЙЛА (test_ux_...): намеренно подобрано так, чтобы при алфавитном сборе
pytest (`pytest tests/ -v`, порядок файлов по умолчанию) этот файл шёл ПОСЛЕ
test_franchise_scout.py, а не перед ним. Диагностика тестировщика: если этот
модуль (импортирует batch_print -> cv2/OpenCV, вызывает render_design ->
typography_v3.compose_text_v3 -> реальный TrueType-рендер шрифта) собирается и
исполняется В ТОМ ЖЕ pytest-процессе НЕПОСРЕДСТВЕННО ПЕРЕД test_franchise_scout.py
(который импортирует pandas/pyarrow), воспроизводится СТАБИЛЬНЫЙ Windows access
violation / segmentation fault (exit code 139) внутри
pandas.core.arrays.string_arrow._from_sequence — падает ВЕСЬ прогон pytest, а не
только один тест (~55 из ~223 тестов успевают отработать, остальные не
запускаются). Изолированный запуск этого файла (в одиночку или ПОСЛЕ
franchise_scout) — стабилен, падает только ожидаемым AssertionError. Это похоже
на конфликт нативной памяти OpenCV/FreeType и pyarrow в одном процессе на этой
машине (Windows, Python 3.14) — НЕ логическая ошибка теста или продуктового
кода, инфраструктурная проблема окружения. Если в будущем сборка тестов начнёт
падать похожим образом (segfault без трейсбека в самом тесте) — в первую
очередь проверить порядок коллекции относительно test_franchise_scout.py.

Живой сценарий (out_batch/daily_2026-07-09/0007_payback__тайская_дорама, design
style_id="11_propaganda_B"):
  1. design["prompt"] (художественное описание сцены от Claude, НЕ type_spec!)
     САМ ПО СЕБЕ содержит инструкцию рисовать текст как часть композиции:
     "Typography is integrated as a structural design element: a single bold
     diagonal wordmark ... A vertical column of upright Japanese katakana
     stands as a perfectly straight anchor ...".
  2. Все 3 основные попытки (design["type_spec"] непуст, слоган "PAYBACK",
     кана "ペイバック") проваливают OCR (транскрипт "PAYBACK\n?????" — кана не
     сошлась поглифно, вероятно временный сбой самого OCR, как и в багe
     пятнадцатого захода) -> honest text-fallback: доп. генерация с
     fallback_design["type_spec"] = "" (замена text-блока на _NO_TEXT_TAIL,
     "No letters, no words...").
  3. НО design["prompt"] (основной художественный промпт, откуда взято ОПИСАНИЕ
     СЦЕНЫ) при этом НЕ трогается — art_director.build_prompt всегда начинает
     part[0] = design["prompt"], и там уже жёстко зашита инструкция про диагональный
     wordmark + вертикальную кану. _NO_TEXT_TAIL добавляется ПОСЛЕ, но модель
     (nano-banana) следует более конкретному/раннему описанию сцены и рисует
     текст самостоятельно, несмотря на поздний общий запрет — картинка приходит
     С ВСТРОЕННЫМ текстом "PAYBACK" (диагональные плашки, красный) + встроенной
     каной "ペイバック" (белая вертикальная колонка), border coverage=1.00.
  4. fb_cov(1.00) >= main_loop_best_cov(1.00) -> код (текущий, ПОСЛЕ фикса
     пятнадцатого захода) использует именно fb_img как raw_img — правильно по
     цветовому гейту, но эта fb_img уже содержит художественный текст.
  5. text_fallback остаётся True (выставляется безусловно в этой ветке) ->
     apply_code_typography = True -> typography_v3.compose_text_v3 (text_modes_v3
     = ["kanji_on", "collection_footer"]) накладывает ЕЩЁ ОДНУ вертикальную кану
     + ещё один текстовый блок ПОВЕРХ уже нарисованного художественного текста.
  6. Итог (подтверждено визуально на живом
     out_batch/daily_2026-07-09/0007_payback__тайская_дорама_print.png):
     кодовая белая кана "ペイバック" ложится ПРЯМО НА встроенную диагональную
     надпись "PAYBACK" (буквы "AYB" почти полностью закрыты), плюс отдельная
     мелкая кодовая подпись "PAYBACK" снизу кадра — принт нечитаем, два разных
     текстовых слоя конфликтуют.

Корневая причина: `text_fallback` в ветке `elif expected_phrases:` (batch_print.
render_design) означает "фолбэк-ГЕНЕРАЦИЯ была запрошена" (fallback_design с
type_spec=""), но НЕ гарантирует, что итоговая fb_img реально ПРИШЛА БЕЗ текста —
модель может проигнорировать _NO_TEXT_TAIL, особенно когда design["prompt"] уже
содержит подробное описание встроенной типографики (типично для style_id из
STYLE_BANK.json, чей text_treatment описывает текст как часть композиции —
minimum все propaganda-варианты, ukiyoe, baroque_frame и т.п.). Код применяет
apply_code_typography=True БЕЗУСЛОВНО при text_fallback=True, не проверяя, есть
ли уже текст на самой fb_img.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_fallback_double_text_regression_qa.py -v
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
    """Воспроизводит живой design.json 0007_payback__тайская_дорама (см. docstring)."""
    base = {
        "prompt": (
            "A young adult man stands at the center of the frame. Typography is "
            "integrated as a structural design element: a single bold diagonal "
            "wordmark runs parallel to the main colour band. A vertical column of "
            "upright Japanese katakana stands as a perfectly straight anchor."
        ),
        "chroma": "green",
        "slogan": "PAYBACK",
        "slogan_color": "red",
        "kana": "ペイバック",
        "character_en": "",
        "title_en": "",
        "signature_props": "",
        "text_mode": "under",
        "text_modes_v3": ["kanji_on", "collection_footer"],
        "quote": "",
        "name_jp": "ペイバック",
        "mood": "duotone_quote",
        "type_spec": (
            "Bold geometric propaganda-poster slab lettering — the main wordmark "
            "PAYBACK is set as stepped diagonal cascading plates tilted at 15 "
            "degrees, bright blood-red flat fill; a vertical column of katakana."
        ),
        "style_id": "11_propaganda_B",
    }
    base.update(overrides)
    return base


def _img_with_embedded_text(w=700, h=980) -> Image.Image:
    """Имитирует РЕАЛЬНУЮ живую fallback-картинку 0007: green border (coverage=1.00
    правильный хромакей) + фигура + УЖЕ ВСТРОЕННЫЙ художественный текст ("PAYBACK"
    диагональю + вертикальная кана) — воспроизводит то, что модель нарисовала текст
    несмотря на fallback_design["type_spec"]="" (см. докстринг модуля, пункт 3).

    ПРИМЕЧАНИЕ: намеренно НЕ используется PIL.ImageFont.load_default() — на этой
    машине (Windows, Python 3.14) загрузка растрового шрифта PIL в этом же
    процессе, где позже импортируется pandas/pyarrow (test_franchise_scout.py,
    коллекция pytest идёт по алфавиту сразу после этого файла), стабильно
    вызывает Windows access violation / segfault — воспроизведено и изолировано
    отдельно от логики этого теста (см. заметки тестировщика в отчёте приёмки).
    Сплошные цветные прямоугольники дают ту же цветовую сигнатуру ("узкая
    зона встроенного текста таким-то цветом"), не требуя рендера реального
    шрифта — тест проверяет цветовое наложение кода поверх зоны, не форму букв."""
    img = Image.new("RGB", (w, h), (0, 177, 64))  # green chroma border
    d = ImageDraw.Draw(img)
    # фигура (силуэт персонажа)
    d.rectangle([w * 0.2, h * 0.05, w * 0.8, h * 0.95], fill=(60, 60, 70))
    # встроенный художественный текст: диагональная плашка "PAYBACK" (красная) —
    # сплошной прямоугольник вместо рендера буквы, та же цветовая сигнатура
    d.rectangle([w * 0.15, h * 0.75, w * 0.55, h * 0.85], fill=(220, 30, 20))
    # встроенная вертикальная "кана" (белая колонка справа, без рендера символов)
    d.rectangle([w * 0.82, h * 0.12, w * 0.92, h * 0.60], fill=(250, 245, 230))
    return img


def _fake_gen_image_factory(images: list):
    calls = {"n": 0}

    def _fake(prompt, seed=None, model=None, reference=None):
        img = images[min(calls["n"], len(images) - 1)]
        calls["n"] += 1
        return img

    _fake.calls = calls
    return _fake


def test_fallback_with_embedded_text_does_not_get_double_code_typography_on_top(
        tmp_path, monkeypatch):
    """РЕГРЕСС (нашёл тестировщик, живая контрольная партия 2026-07-09, out_batch/
    daily_2026-07-09/0007_payback__тайская_дорама_diecut.png / _print.png):
    когда text-fallback генерация (доп. попытка после провала OCR на всех основных
    попытках) ВСЁ РАВНО приходит С ВСТРОЕННЫМ художественным текстом (модель
    проигнорировала _NO_TEXT_TAIL, следуя описанию типографики уже зашитому в
    design["prompt"]) и её цветовой гейт хромакея в порядке (fb_cov хорошее,
    используется как raw_img) — render_design НЕ ДОЛЖЕН слепо накладывать ЕЩЁ ОДИН
    слой кодовой типографики (typography_v3.compose_text_v3 / typography.compose_text)
    поверх уже нарисованного художественного текста. Итог должен быть читаемым,
    без двойного/конфликтующего текста.

    ДО фикса: apply_code_typography = (not text_render_image) or text_fallback or
    not expected_phrases — при text_fallback=True код применяет typography_v3
    БЕЗУСЛОВНО, не проверяя, есть ли уже текст на самой картинке. Тест падает,
    потому что после typography_v3.compose_text_v3 на итоговом diecut оказывается
    ВТОРОЙ слой текста (доказывается через рост числа отдельных непрозрачных
    "текстовых" пятен ПОВЕРХ уже нарисованного встроенного текста — эвристика на
    основе прироста непрозрачных пикселей в зоне, где встроенный текст УЖЕ был).
    ПОСЛЕ ожидаемого фикса — код должен либо детектировать наличие встроенного
    текста на fallback-картинке и НЕ накладывать typography поверх, либо
    гарантировать неконфликтующее размещение (например urезка второго слоя за
    пределы зоны первого) — конкретный путь фикса решает разработчик, тест
    фиксирует ТОЛЬКО наблюдаемый живой брак (нечитаемый задвоенный текст)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    monkeypatch.setattr(config, "UPSCALE", False)

    embedded_text_img = _img_with_embedded_text()

    # 2 основные попытки (timeout_retries=1) проваливают OCR -> fallback.
    fake_gen = _fake_gen_image_factory([embedded_text_img, embedded_text_img,
                                        embedded_text_img])
    monkeypatch.setattr(batch_print.providers, "generate_image", fake_gen)
    monkeypatch.setattr(batch_print, "_verify_text", lambda image, phrases: False)
    monkeypatch.setattr(batch_print.character_ref, "get_reference",
                        lambda *a, **k: None)
    # Оффлайн-мок проверки "текста нет" из фикса (шестнадцатый заход): fallback-
    # картинка в этой фикстуре СОДЕРЖИТ встроенный текст, поэтому честный ответ —
    # False (текст есть). Без мока тест ходил в реальную сеть (gemini-2.5-flash
    # OCR) — скрытая сетевая зависимость, находка приёмки. raising=False — на
    # старом (до-фиксовом) коде атрибута нет, тест всё равно должен запускаться
    # и падать по основному ассерту.
    monkeypatch.setattr(batch_print, "_verify_no_text", lambda image: False,
                        raising=False)

    d = _design()
    res = batch_print.render_design(d, "0007", tmp_path, timeout_retries=1)

    assert res["ok"] is True
    assert res["text_fallback"] is True

    diecut = Image.open(res["diecut"]).convert("RGBA")
    arr = np.array(diecut)
    alpha = arr[:, :, 3]

    # Зона, где живой встроенный текст УЖЕ был нарисован (диагональная плашка
    # "PAYBACK" в нижней трети кадра, см. _img_with_embedded_text) — если код
    # НЕ накладывает typography поверх этой же зоны, состав пикселей там должен
    # остаться СВЯЗАННЫМ с исходной вырезкой (не получить второй непрозрачный
    # слой другого цвета поверх уже готового текста). Проверяем, что кодовая
    # typography_v3 (kanji_on / collection_footer, design["text_modes_v3"]) не
    # рисует белую вертикальную "кану" ПРЯМО НА зоне встроенного слогана
    # "PAYBACK" (нижняя треть, правая часть где была встроенная "кана"-колонка):
    h, w = alpha.shape
    # ПЕРЕКАЛИБРОВКА (решение лида, шестнадцатый заход): прежняя зона
    # [0.72h:0.90h, 0.10w:0.55w] почти целиком лежала ВНУТРИ силуэта фигуры
    # (0.2w-0.8w x 0.05h-0.95h) — честная вырезка без единого добавленного
    # пикселя давала долю 0.409 > порога 0.35 (недостижимый порог, диагностика
    # разработчика подтверждена приёмкой арифметически). Новая зона — СТРОГО
    # внутри красной плашки встроенной надписи PAYBACK (плашка 0.15w-0.55w x
    # 0.75h-0.85h, берём с отступом от краёв): честная вырезка здесь даёт ~0
    # не-красных пикселей, а старый баг (белая кодовая кана поверх плашки) —
    # высокую долю. Порог 0.35 снова осмыслен.
    embedded_text_zone = arr[int(h * 0.76):int(h * 0.84), int(w * 0.17):int(w * 0.53)]
    # Встроенный текст был нарисован красным (220,30,20). Если кодовая
    # typography легла ПРЯМО поверх (другой цвет, например белый ~(250,245,230)
    # типичный для typography_v3 word colors), доля НЕ-красных непрозрачных
    # пикселей в этой зоне будет высокой -> двойной текст.
    opaque_mask = embedded_text_zone[:, :, 3] > 200
    if opaque_mask.sum() > 0:
        reds = embedded_text_zone[:, :, 0].astype(int)
        greens = embedded_text_zone[:, :, 1].astype(int)
        is_reddish = (reds > 150) & (greens < 100)
        non_red_opaque = opaque_mask & (~is_reddish)
        frac_non_red_over_embedded_text = non_red_opaque.sum() / opaque_mask.sum()
    else:
        frac_non_red_over_embedded_text = 0.0

    assert frac_non_red_over_embedded_text < 0.35, (
        f"{frac_non_red_over_embedded_text:.2f} доли НЕ-красных непрозрачных "
        f"пикселей легли ПРЯМО на зону встроенного художественного текста "
        f"'PAYBACK' (красный) — это кодовая typography_v3 наложила ВТОРОЙ слой "
        f"текста поверх уже готового встроенного, воспроизводит живой брак "
        f"out_batch/daily_2026-07-09/0007_payback__тайская_дорама_diecut.png "
        f"(вертикальная кодовая кана 'ペイバック' легла прямо на диагональную "
        f"надпись 'PAYBACK', буквы почти полностью закрыты, принт нечитаем)."
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
