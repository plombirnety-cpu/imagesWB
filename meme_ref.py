# -*- coding: utf-8 -*-
"""meme_ref.py — референс-картинка ОРИГИНАЛА интернет-мема для генерации ПО
РЕФЕРЕНСУ. Аналог `character_ref.py` (аниме-персонажи), но для НЕ-аниме вирусных
мемов (жалоба владельца 2026-07-11): сова на скакалке, кот со слюной, Backrooms и
т.п. генерятся НЕ похожими на оригинал — модель по одному текстовому описанию
рисует СВОЮ версию (например серую сову вместо фиолетовой из оригинала), точно
та же причина, что чинит `character_ref.py` для аниме-персонажей.

Отличие от `character_ref.py`: для мемов НЕТ каталога-справочника типа Jikan/
AniList (это не персонаж вымышленного тайтла, а конкретная вирусная картинка) —
референс всегда РУЧНОЙ. Владелец кладёт файл-оригинал в `data/meme_refs/<slug>.png`
самостоятельно; get_reference() только читает его с диска, никакой сети.

get_reference(slug) -> PIL.Image | None:
    1. Источник — ЛОКАЛЬНЫЙ файл `data/meme_refs/<slug>.png`.
    2. Файла нет ИЛИ slug пуст/содержит недопустимые символы ИЛИ файл битый ->
       None с предупреждением в консоль — вызывающий код (batch_print.render_design)
       продолжает генерацию по чистому текстовому описанию, КАК СЕЙЧАС (graceful
       degradation, ничего не падает) — так владелец может запускать пайплайн ДО
       того, как положит все референсы мемов.

Передача в генерацию — ТЕМ ЖЕ существующим механизмом, что и character_ref:
providers.generate_image(reference=<PIL.Image>) кодирует референс как
inline_data (JPEG base64) ПЕРЕД текстом промпта (см. providers.py). Здесь
реализована ТОЛЬКО загрузка картинки, ничего нового в providers.py не добавлено.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
MEME_REF_DIR = HERE / "data" / "meme_refs"

# Безопасные имена файлов — латиница/цифры/подчёркивание/дефис (тот же принцип,
# что character_ref._slug, но здесь slug приходит УЖЕ готовым из trends_plan.json/
# design, не из свободного имени персонажа — просто защита от path traversal и
# кривых значений, не транслитерация).
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _ref_path(slug: str) -> Path | None:
    """slug -> data/meme_refs/<slug>.png. None, если slug пуст или содержит
    символы вне [A-Za-z0-9_-] (предупреждение печатает get_reference)."""
    slug = (slug or "").strip()
    if not slug or not _SLUG_RE.match(slug):
        return None
    return MEME_REF_DIR / f"{slug}.png"


def get_reference(slug: str) -> Image.Image | None:
    """Референс-картинка оригинала мема (PIL.Image, RGB) для подмешивания в
    providers.generate_image(reference=...) — ПРИОРИТЕТ над текстовым описанием
    (см. batch_print._MEME_REFERENCE_PREFIX). None при отсутствии/недопустимости
    slug, отсутствии файла или битом файле — вызывающий код продолжает генерацию
    по тексту, как раньше (graceful degradation)."""
    path = _ref_path(slug)
    if path is None:
        print(f"  !! meme_ref: недопустимый slug {slug!r} — генерация по описанию",
              flush=True)
        return None
    if not path.exists():
        print(f"  meme_ref {slug!r} задан, но файла {path} нет — генерация по "
              f"текстовому описанию (положите референс-картинку оригинала мема и "
              f"перезапустите)", flush=True)
        return None
    try:
        img = Image.open(path)
        img.load()
        return img.convert("RGB")
    except Exception as e:  # noqa: BLE001 — битый файл референса не должен ронять генерацию
        print(f"  !! meme_ref: файл {path.name} повреждён ({e}) — генерация по "
              f"описанию", flush=True)
        return None
