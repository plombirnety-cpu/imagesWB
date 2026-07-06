# -*- coding: utf-8 -*-
"""Удаление фона нейросетью rembg — СЕМАНТИЧЕСКАЯ сегментация субъекта.

Раньше был хромакей по цвету — он съедал сам принт: светлые/зеленоватые части
субъекта (кости скелета, белый кот) попадали в диапазон фона и стирались.
rembg находит ОБЪЕКТ целиком и вырезает только фон вокруг, не трогая внутренности.

Модель isnet-general-use (как в видео-движке студии). Сессия кэшируется (модель
грузится один раз). post_process_mask чистит края маски.
"""
from PIL import Image
from rembg import new_session, remove

_session = None


def _session_get():
    global _session
    if _session is None:
        # isnet-anime — лучше держит аниме/cel-shading края, чем isnet-general.
        _session = new_session("isnet-anime")
    return _session


def cut_out(img_pil: Image.Image) -> Image.Image:
    """RGB-картинка -> RGBA с вырезанным фоном (сам субъект не повреждается)."""
    out = remove(img_pil.convert("RGB"), session=_session_get(), post_process_mask=True)
    return out.convert("RGBA")
