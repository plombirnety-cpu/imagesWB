# -*- coding: utf-8 -*-
"""Финальная подготовка Print Factory через встроенное ядро GreenKey."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

import greenkey_core


@dataclass(frozen=True)
class GreenKeyResult:
    """Метаданные успешно подготовленного PNG."""

    path: Path
    detected_bg: tuple[int, int, int]
    key: str


def process_file(path: str | Path, *, sharp: bool = True) -> GreenKeyResult:
    """Атомарно заменить хромакейный PNG прозрачным результатом GreenKey.

    Исходный файл остаётся нетронутым, если чтение, обработка или сохранение
    завершились ошибкой. ``sharp=True`` соответствует дефолту обновлённого
    GreenKey: без апскейла и мыла, в исходном разрешении Print Factory.
    """
    source = Path(path)
    with Image.open(source) as opened:
        opened.load()
        rgb = opened.convert("RGB")

    rgba, detected_bg, key_code = greenkey_core.process(rgb, sharp=sharp)
    if rgba.mode != "RGBA":
        raise ValueError(f"GreenKey вернул режим {rgba.mode!r} вместо RGBA")

    min_alpha, _ = rgba.getchannel("A").getextrema()
    if min_alpha == 255:
        raise ValueError("GreenKey не создал прозрачные пиксели")

    temporary = source.with_name(f".{source.name}.greenkey.tmp")
    try:
        rgba.save(temporary, format="PNG")
        temporary.replace(source)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return GreenKeyResult(
        path=source,
        detected_bg=detected_bg,
        key="blue" if key_code == 2 else "green",
    )
