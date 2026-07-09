# -*- coding: utf-8 -*-
"""Регресс-тест тестировщика (приёмка банка стилей + печатное качество, 2026-07-09).

Живая контрольная партия daily_prints.py --limit 8 --skip-collect поймала реальный
дефект в typography_v3.ring_text на дизайне 0005_by_индия___портрет_певиц (style_id
"09_ring_medallion", out_batch/daily_2026-07-09/): кольцевой текст лёг ВНЕ нарисованного
генерацией кольца — отдельные буквы разбросаны по чёрному фону в углах кадра (около
кончиков "молний"-ауры персонажа), а не по видимому кольцу-медальону. Не читается как
единая надпись по кругу (нарушает критерий приёмки "кольцевой текст кодом, читается
один раз").

Корневая причина (typography_v3._ring_radius_and_center, вызывается из ring_text):
радиус/центр кольца считаются от bbox АЛЬФА-КАНАЛА ВСЕЙ картинки (typography._alpha_bbox),
включая любые визуальные элементы, торчащие ЗА пределы декоративного кольца — ауру/
молнии/спецэффекты вокруг персонажа (STYLE_BANK.json "09_ring_medallion".essence
буквально просит "crackling neon aura energy pulses... streaking upward", то есть эффекты
ЗА пределами кольца — штатная часть стиля, не редкий случай). Если такие эффекты есть,
bbox-диагональ раздувается, расчётный радиус оказывается БОЛЬШЕ фактического радиуса
нарисованного кольца — буквы улетают в пустой фон снаружи кольца.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_ring_text_regression_qa.py -v
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import typography_v3 as t3  # noqa: E402


def _figure_with_ring_and_aura(w=900, h=900):
    """Синтетика, воспроизводящая структуру реального бага: центральная фигура +
    ЯВНО нарисованное тонкое кольцо (как рисует nano-banana для style_id
    "09_ring_medallion") радиусом ring_r + аура-лучи, торчащие ЗА пределы кольца
    (как молнии/спецэффекты в STYLE_BANK "09_ring_medallion".essence — "crackling
    neon aura energy... streaking upward"). Возвращает (картинка, ring_r) — ring_r
    нужен вызывающему коду для точного assert "буквы легли НА кольцо"."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = w // 2, h // 2
    ring_r = int(min(w, h) * 0.30)

    d.ellipse([cx - 60, cy - 60, cx + 60, cy + 60], fill=(200, 50, 50, 255))
    d.ellipse([cx - ring_r - 10, cy - ring_r - 10, cx + ring_r + 10, cy + ring_r + 10],
              outline=(200, 170, 80, 255), width=14)
    for ang in range(0, 360, 45):
        rad = math.radians(ang)
        x2 = cx + int((ring_r * 1.7) * math.cos(rad))
        y2 = cy + int((ring_r * 1.7) * math.sin(rad))
        x1 = cx + int(ring_r * 0.9 * math.cos(rad))
        y1 = cy + int(ring_r * 0.9 * math.sin(rad))
        d.line([x1, y1, x2, y2], fill=(255, 0, 255, 200), width=6)
    return img, ring_r


def test_ring_text_places_letters_on_the_drawn_ring_not_outside_it():
    """РЕГРЕСС (нашёл тестировщик, живая партия 2026-07-09, out_batch/daily_2026-07-09/
    0005_by_индия___портрет_певиц_diecut.png): когда картинка содержит аура-эффекты
    (молнии/лучи) ЗА пределами нарисованного декоративного кольца — типичная часть
    STYLE_BANK "09_ring_medallion".essence — ring_text ДОЛЖЕН положить буквы НА
    визуальное кольцо (около радиуса ring_r), а не в пустой фон снаружи (около
    кончиков ауры, радиус ~1.7*ring_r).

    ДО фикса: буквы расположены на радиусе, раздутом bbox всей ауры (~1.6-1.9*ring_r),
    что заметно ДАЛЬШЕ от центра, чем сам нарисованный ring_r — тест падает.
    ПОСЛЕ фикса (радиус кольца должен определяться по самому кольцу/силуэту фигуры,
    не по bbox всей ауры) — буквы лягут в пределах разумного допуска вокруг ring_r,
    тест проходит."""
    fig, ring_r = _figure_with_ring_and_aura()
    out = t3.ring_text(fig, "MADARA")

    a = np.array(out.getchannel("A"))
    h, w = a.shape
    cy_out, cx_out = h // 2, w // 2  # canvas_pad симметричен -> центр не сместился

    # Маска "буквы кольца" = непрозрачные пиксели МИНУС сама фигура/кольцо/аура
    # оригинала (сравниваем с исходным fig, вставленным в тот же центр canvas_pad).
    canvas_pad = (w - fig.width) // 2
    orig_full = np.zeros((h, w), dtype=np.uint8)
    orig_a = np.array(fig.getchannel("A"))
    orig_full[canvas_pad:canvas_pad + fig.height, canvas_pad:canvas_pad + fig.width] = orig_a
    letters_mask = (a > 40) & (orig_full <= 40)

    assert letters_mask.any(), "ring_text не дорисовал ни одного пикселя букв"

    yy, xx = np.where(letters_mask)
    radii = np.sqrt((xx - cx_out) ** 2 + (yy - cy_out) ** 2)
    median_letter_radius = float(np.median(radii))

    # Допуск: буквы должны лежать в пределах +-35% от фактического радиуса
    # нарисованного кольца (ring_r) — не на кончиках ауры (~1.7*ring_r).
    lower = ring_r * 0.65
    upper = ring_r * 1.35
    assert lower <= median_letter_radius <= upper, (
        f"буквы кольца легли на медианном радиусе {median_letter_radius:.0f}px, "
        f"ожидался диапазон [{lower:.0f}, {upper:.0f}] вокруг фактического кольца "
        f"ring_r={ring_r}px — буквы уехали в пустой фон за пределами кольца "
        f"(воспроизводит живой баг out_batch/daily_2026-07-09/"
        f"0005_by_индия___портрет_певиц_diecut.png, style_id=09_ring_medallion)"
    )


def test_ring_text_long_quote_sentence_stays_readable_as_single_ring_pass():
    """РЕГРЕСС-документация: batch_print.render_design берёт ring_phrase =
    design.get('quote') or design.get('slogan') БЕЗ проверки длины/уместности для
    кольца (см. batch_print.py, блок 'Медальон-гибрид'). В живой партии design['quote']
    был полным предложением 'La voz que no se olvida' (18 не-пробельных букв) —
    ring_text() формально прошёл (один проход по кругу, буквы не повторились), но
    результат на реальной картинке нечитаем как кольцевая надпись (см. соседний тест
    про радиус). Этот тест фиксирует факт: ring_text() сам НЕ имеет защиты от длинных
    фраз (не обрезает, не предупреждает) — фиксируем текущее поведение как задокументи-
    рованный риск для дальнейшей работы над интеграцией (art_director/batch_print
    должны либо выбирать короткую фразу для кольца, либо ring_text должен сам ограничи-
    вать длину)."""
    fig, ring_r = _figure_with_ring_and_aura()
    long_quote = "La voz que no se olvida"
    out = t3.ring_text(fig, long_quote)

    a = np.array(out.getchannel("A"))
    h, w = a.shape
    cy_out, cx_out = h // 2, w // 2
    canvas_pad = (w - fig.width) // 2
    orig_full = np.zeros((h, w), dtype=np.uint8)
    orig_a = np.array(fig.getchannel("A"))
    orig_full[canvas_pad:canvas_pad + fig.height, canvas_pad:canvas_pad + fig.width] = orig_a
    letters_mask = (a > 40) & (orig_full <= 40)
    assert letters_mask.any()

    yy, xx = np.where(letters_mask)
    radii = np.sqrt((xx - cx_out) ** 2 + (yy - cy_out) ** 2)
    median_letter_radius = float(np.median(radii))
    # Тот же радиус-баг воспроизводится независимо от длины фразы (радиус не зависит
    # от количества букв) — длинная фраза сама по себе НЕ первопричина, но усугубляет
    # нечитаемость (больше отдельных мелких букв разбросано по неверному радиусу).
    lower = ring_r * 0.65
    upper = ring_r * 1.35
    assert lower <= median_letter_radius <= upper, (
        f"длинная фраза кольца тоже мимо кольца: медиана радиуса букв "
        f"{median_letter_radius:.0f}px вне диапазона [{lower:.0f}, {upper:.0f}]"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
