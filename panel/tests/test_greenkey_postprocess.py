# -*- coding: utf-8 -*-
"""Регрессии автоматической подготовки принтов через GreenKey."""
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

import greenkey_core
import greenkey_postprocess


@pytest.mark.parametrize(
    ("background", "foreground", "expected_key"),
    [
        ((10, 180, 70), (220, 40, 180), "green"),
        ((0, 90, 255), (230, 30, 210), "blue"),
    ],
)
def test_process_file_removes_chroma_and_preserves_subject(
    tmp_path: Path,
    background: tuple[int, int, int],
    foreground: tuple[int, int, int],
    expected_key: str,
):
    path = tmp_path / "print.png"
    source = Image.new("RGB", (80, 80), background)
    ImageDraw.Draw(source).rectangle((20, 20, 59, 59), fill=foreground)
    source.save(path)

    result = greenkey_postprocess.process_file(path)

    assert result.path == path
    assert result.key == expected_key
    with Image.open(path) as prepared:
        assert prepared.mode == "RGBA"
        assert prepared.size == source.size
        assert prepared.getpixel((2, 2))[3] == 0
        assert prepared.getpixel((40, 40)) == (*foreground, 255)


def test_process_file_keeps_original_when_greenkey_fails(tmp_path: Path, monkeypatch):
    path = tmp_path / "print.png"
    Image.new("RGB", (20, 20), (0, 180, 70)).save(path)
    original = path.read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(greenkey_core, "process", fail)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        greenkey_postprocess.process_file(path)

    assert path.read_bytes() == original
    assert not path.with_name(f".{path.name}.greenkey.tmp").exists()
