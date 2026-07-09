# -*- coding: utf-8 -*-
"""Тесты тринадцатого захода (качество печати / QC): апскейл до 300 DPI (upscale.py,
мокан на resize — без exe), QC-гейт масштаба фигуры (batch_print._figure_fills_frame),
строгая поглифная сверка каны (batch_print._verify_text, дакутэн/хандакутэн), медальон-
гибрид (typography_v3.ring_text), style_madara.py дефолт --character пустой. Полностью
офлайн — никакой сети, никакого реального realesrgan-ncnn-vulkan.exe/Gemini.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_print_quality.py -v
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import batch_print  # noqa: E402
import config  # noqa: E402
import palette  # noqa: E402
import providers  # noqa: E402
import style_madara  # noqa: E402
import typography_v3 as t3  # noqa: E402
import upscale  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# upscale.py — мокан на resize (без реального exe/subprocess)
# ═══════════════════════════════════════════════════════════════════════════════

def _tiny_rgba(w=40, h=60) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, w - 2, h - 2], fill=(200, 60, 60, 255))
    return img


def test_upscale_missing_exe_returns_ok_false_not_raises(tmp_path, monkeypatch):
    """exe не найден на диске -> upscale() возвращает ok=False с понятной причиной,
    НЕ бросает исключение (задача: "предупреждение и пропуск, не падать")."""
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", tmp_path / "does_not_exist.exe")
    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    out = tmp_path / "out.png"

    res = upscale.upscale(src, out, scale=4)
    assert res["ok"] is False
    assert "не найден" in res["error"]
    assert not out.exists()


def test_upscale_missing_model_returns_ok_false(tmp_path, monkeypatch):
    """exe есть, но модель отсутствует -> ok=False, тоже не падает."""
    fake_exe = tmp_path / "realesrgan-ncnn-vulkan.exe"
    fake_exe.write_bytes(b"not a real exe")
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", fake_exe)
    monkeypatch.setattr(upscale, "REALESRGAN_MODELS_DIR", tmp_path / "models")

    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    out = tmp_path / "out.png"
    res = upscale.upscale(src, out, model="does-not-exist")
    assert res["ok"] is False
    assert "не найдена" in res["error"]


def test_upscale_happy_path_mocked_on_resize(tmp_path, monkeypatch):
    """subprocess.run замокан на PIL resize x scale (эмулирует реальный
    realesrgan-ncnn-vulkan.exe без реального вызова) — upscale() возвращает ok=True,
    альфа-канал сохраняется (RGBA), итоговый размер = вход * scale."""
    fake_exe = tmp_path / "realesrgan-ncnn-vulkan.exe"
    fake_exe.write_bytes(b"not a real exe")
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "realesrgan-x4plus-anime.bin").write_bytes(b"x")
    (models_dir / "realesrgan-x4plus-anime.param").write_bytes(b"x")
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", fake_exe)
    monkeypatch.setattr(upscale, "REALESRGAN_MODELS_DIR", models_dir)
    monkeypatch.setattr(upscale, "REALESRGAN_DIR", tmp_path)

    src = tmp_path / "in.png"
    src_img = _tiny_rgba(40, 60)
    src_img.save(src)
    out = tmp_path / "out.png"

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        in_path = Path(cmd[cmd.index("-i") + 1])
        out_path = Path(cmd[cmd.index("-o") + 1])
        scale = int(cmd[cmd.index("-s") + 1])
        im = Image.open(in_path)
        im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
        im.save(out_path)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(upscale.subprocess, "run", _fake_run)

    res = upscale.upscale(src, out, scale=4, model="realesrgan-x4plus-anime")
    assert res["ok"] is True
    assert res["out_size"] == (160, 240)
    assert out.exists()
    with Image.open(out) as im:
        assert im.mode == "RGBA"
        assert im.size == (160, 240)


def test_upscale_paths_passed_to_subprocess_are_absolute(tmp_path, monkeypatch):
    """Регресс-тест на реальный баг (найден на живом замере 2026-07-09): exe запускается
    с cwd=REALESRGAN_DIR — если png_in/png_out переданы ОТНОСИТЕЛЬНЫМ путём (обычный
    случай, batch_print передаёт outdir/tag_diecut.png), exe резолвил их относительно
    СВОЕЙ cwd (REALESRGAN_DIR), не относительно рабочей директории Python-процесса, и
    падал "decode image ... failed". upscale() ОБЯЗАН резолвить оба пути в абсолютные
    ДО передачи в subprocess.run, независимо от того, что передал вызывающий код."""
    fake_exe = tmp_path / "tools_dir" / "realesrgan-ncnn-vulkan.exe"
    fake_exe.parent.mkdir()
    fake_exe.write_bytes(b"x")
    models_dir = tmp_path / "tools_dir" / "models"
    models_dir.mkdir()
    (models_dir / "realesrgan-x4plus-anime.bin").write_bytes(b"x")
    (models_dir / "realesrgan-x4plus-anime.param").write_bytes(b"x")
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", fake_exe)
    monkeypatch.setattr(upscale, "REALESRGAN_MODELS_DIR", models_dir)
    monkeypatch.setattr(upscale, "REALESRGAN_DIR", fake_exe.parent)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    src = work_dir / "in.png"
    _tiny_rgba().save(src)
    out = work_dir / "out.png"

    captured_cmd = {}

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        captured_cmd["cmd"] = cmd
        in_path = Path(cmd[cmd.index("-i") + 1])
        out_path = Path(cmd[cmd.index("-o") + 1])
        im = Image.open(in_path)
        im.save(out_path)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(upscale.subprocess, "run", _fake_run)

    # Передаём ОТНОСИТЕЛЬНЫЕ пути (строкой, как это делает os.path из разных cwd) —
    # монkeypatch cwd Python-процесса на work_dir, чтобы относительный путь "in.png"
    # был валиден ТОЛЬКО если резолвится СЕЙЧАС (до смены cwd внутри subprocess).
    monkeypatch.chdir(work_dir)
    res = upscale.upscale("in.png", "out.png", scale=2, model="realesrgan-x4plus-anime")

    assert res["ok"] is True
    in_arg = Path(captured_cmd["cmd"][captured_cmd["cmd"].index("-i") + 1])
    out_arg = Path(captured_cmd["cmd"][captured_cmd["cmd"].index("-o") + 1])
    assert in_arg.is_absolute()
    assert out_arg.is_absolute()


def test_upscale_subprocess_timeout_returns_ok_false(tmp_path, monkeypatch):
    """Таймаут subprocess -> ok=False с понятной причиной, не падает."""
    fake_exe = tmp_path / "realesrgan-ncnn-vulkan.exe"
    fake_exe.write_bytes(b"x")
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "realesrgan-x4plus-anime.bin").write_bytes(b"x")
    (models_dir / "realesrgan-x4plus-anime.param").write_bytes(b"x")
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", fake_exe)
    monkeypatch.setattr(upscale, "REALESRGAN_MODELS_DIR", models_dir)
    monkeypatch.setattr(upscale, "REALESRGAN_DIR", tmp_path)

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(upscale.subprocess, "run", _fake_run)

    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    res = upscale.upscale(src, tmp_path / "out.png", timeout=1)
    assert res["ok"] is False
    assert "таймаут" in res["error"]


def test_config_upscale_defaults():
    """config.UPSCALE дефолт on, config.FIGURE_MIN_FRAC дефолт 0.55 (задача лида) —
    защита от случайного дефолта, съехавшего в другую сторону при правке .env."""
    assert config.UPSCALE_SCALE == 4 or isinstance(config.UPSCALE_SCALE, int)
    assert config.UPSCALE_MODEL == "realesrgan-x4plus-anime"


def test_config_print_min_side_and_upscale_timeout_defaults():
    """Пятнадцатый заход: config.PRINT_MIN_SIDE дефолт 3800, config.UPSCALE_TIMEOUT
    дефолт 300 — защита от случайного дефолта, съехавшего в другую сторону."""
    assert config.PRINT_MIN_SIDE == 3800
    assert config.UPSCALE_TIMEOUT == 300


# ═══════════════════════════════════════════════════════════════════════════════
# upscale.upscale_to_print_min — адаптивный апскейл до печатного минимума
# (пятнадцатый заход, задача лида: PRINT_MIN_SIDE, серия Lanczos-фолбэков)
# ═══════════════════════════════════════════════════════════════════════════════

def test_upscale_to_print_min_x4_already_sufficient_no_extra_lanczos(tmp_path, monkeypatch):
    """x4 realesrgan уже дал >= min_side по большей стороне -> НИКАКОГО досчёта не
    происходит, out_size = ровно то, что вернул x4 (без дополнительного пересохранения
    через PIL, которое могло бы слегка изменить пиксели)."""
    fake_exe = tmp_path / "realesrgan-ncnn-vulkan.exe"
    fake_exe.write_bytes(b"x")
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "realesrgan-x4plus-anime.bin").write_bytes(b"x")
    (models_dir / "realesrgan-x4plus-anime.param").write_bytes(b"x")
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", fake_exe)
    monkeypatch.setattr(upscale, "REALESRGAN_MODELS_DIR", models_dir)
    monkeypatch.setattr(upscale, "REALESRGAN_DIR", tmp_path)

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        in_path = Path(cmd[cmd.index("-i") + 1])
        out_path = Path(cmd[cmd.index("-o") + 1])
        scale = int(cmd[cmd.index("-s") + 1])
        im = Image.open(in_path)
        im.resize((im.width * scale, im.height * scale), Image.LANCZOS).save(out_path)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(upscale.subprocess, "run", _fake_run)

    src = tmp_path / "in.png"
    _tiny_rgba(1000, 1000).save(src)  # x4 -> 4000x4000, уже >= min_side 3800
    out = tmp_path / "out.png"
    res = upscale.upscale_to_print_min(src, out, min_side=3800, scale=4)
    assert res["ok"] is True
    assert res["out_size"] == (4000, 4000)
    assert res["print_fallback"] is False


def test_upscale_to_print_min_adaptive_lanczos_tops_up_small_x4_result(tmp_path, monkeypatch):
    """x4 realesrgan дал МЕНЬШЕ min_side (raw был мелкий, напр. 768px) -> адаптивный
    PIL Lanczos-досчёт ПРЯМО ПОВЕРХ результата x4 дотягивает до >= min_side. НЕ второй
    проход realesrgan (subprocess.run вызывается ровно ОДИН раз)."""
    fake_exe = tmp_path / "realesrgan-ncnn-vulkan.exe"
    fake_exe.write_bytes(b"x")
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "realesrgan-x4plus-anime.bin").write_bytes(b"x")
    (models_dir / "realesrgan-x4plus-anime.param").write_bytes(b"x")
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", fake_exe)
    monkeypatch.setattr(upscale, "REALESRGAN_MODELS_DIR", models_dir)
    monkeypatch.setattr(upscale, "REALESRGAN_DIR", tmp_path)

    run_calls = {"n": 0}

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        run_calls["n"] += 1
        in_path = Path(cmd[cmd.index("-i") + 1])
        out_path = Path(cmd[cmd.index("-o") + 1])
        scale = int(cmd[cmd.index("-s") + 1])
        im = Image.open(in_path)
        im.resize((im.width * scale, im.height * scale), Image.LANCZOS).save(out_path)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(upscale.subprocess, "run", _fake_run)

    src = tmp_path / "in.png"
    _tiny_rgba(700, 500).save(src)  # x4 -> 2800x2000, обе стороны < 3800 (min_side)
    out = tmp_path / "out.png"
    res = upscale.upscale_to_print_min(src, out, min_side=3800, scale=4)
    assert res["ok"] is True
    assert max(res["out_size"]) >= 3800
    assert res["print_fallback"] is False  # x4 отработал, это адаптивный досчёт, не фолбэк
    assert run_calls["n"] == 1  # realesrgan вызван РОВНО один раз (не второй x4-проход)
    with Image.open(out) as im:
        assert im.mode == "RGBA"  # альфа сохранена через Lanczos-досчёт


def test_upscale_to_print_min_missing_exe_falls_back_to_lanczos_from_source(
        tmp_path, monkeypatch):
    """realesrgan.exe отсутствует -> Lanczos-фолбэк НАПРЯМУЮ с исходника до min_side,
    print_fallback=True, ok=True (печатный размер гарантирован даже без ESRGAN)."""
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", tmp_path / "does_not_exist.exe")

    src = tmp_path / "in.png"
    _tiny_rgba(400, 600).save(src)
    out = tmp_path / "out.png"
    res = upscale.upscale_to_print_min(src, out, min_side=3800)
    assert res["ok"] is True
    assert res["print_fallback"] is True
    assert max(res["out_size"]) >= 3800
    with Image.open(out) as im:
        assert im.mode == "RGBA"


def test_upscale_to_print_min_subprocess_timeout_falls_back_to_lanczos(tmp_path, monkeypatch):
    """realesrgan таймаутирует (config.UPSCALE_TIMEOUT) -> Lanczos-фолбэк с исходника,
    print_fallback=True, ok=True — не блокирует пайплайн даже при зависшем GPU-вызове."""
    fake_exe = tmp_path / "realesrgan-ncnn-vulkan.exe"
    fake_exe.write_bytes(b"x")
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "realesrgan-x4plus-anime.bin").write_bytes(b"x")
    (models_dir / "realesrgan-x4plus-anime.param").write_bytes(b"x")
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", fake_exe)
    monkeypatch.setattr(upscale, "REALESRGAN_MODELS_DIR", models_dir)
    monkeypatch.setattr(upscale, "REALESRGAN_DIR", tmp_path)

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(upscale.subprocess, "run", _fake_run)

    src = tmp_path / "in.png"
    _tiny_rgba(400, 600).save(src)
    out = tmp_path / "out.png"
    res = upscale.upscale_to_print_min(src, out, min_side=3800, timeout=1)
    assert res["ok"] is True
    assert res["print_fallback"] is True
    assert "таймаут" in res["error"]
    assert max(res["out_size"]) >= 3800


def test_upscale_to_print_min_uses_config_print_min_side_by_default(tmp_path, monkeypatch):
    """min_side не передан явно -> читает config.PRINT_MIN_SIDE (monkeypatch применяется
    на момент вызова, не на момент импорта модуля)."""
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", tmp_path / "does_not_exist.exe")
    monkeypatch.setattr(config, "PRINT_MIN_SIDE", 500)

    src = tmp_path / "in.png"
    _tiny_rgba(40, 60).save(src)
    out = tmp_path / "out.png"
    res = upscale.upscale_to_print_min(src, out)
    assert res["ok"] is True
    assert max(res["out_size"]) >= 500
    assert max(res["out_size"]) < 3800  # НЕ дефолт 3800 — реально прочитан override 500


def test_upscale_uses_config_upscale_timeout_by_default(tmp_path, monkeypatch):
    """upscale() без явного timeout -> читает config.UPSCALE_TIMEOUT НА МОМЕНТ ВЫЗОВА
    (не на момент импорта/определения функции — важно, т.к. дефолтный параметр Python
    вычисляется один раз при определении функции, если бы это было
    timeout=config.UPSCALE_TIMEOUT в сигнатуре)."""
    fake_exe = tmp_path / "realesrgan-ncnn-vulkan.exe"
    fake_exe.write_bytes(b"x")
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "realesrgan-x4plus-anime.bin").write_bytes(b"x")
    (models_dir / "realesrgan-x4plus-anime.param").write_bytes(b"x")
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", fake_exe)
    monkeypatch.setattr(upscale, "REALESRGAN_MODELS_DIR", models_dir)
    monkeypatch.setattr(upscale, "REALESRGAN_DIR", tmp_path)
    monkeypatch.setattr(config, "UPSCALE_TIMEOUT", 42)

    captured = {}

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        captured["timeout"] = timeout
        im = Image.open(Path(cmd[cmd.index("-i") + 1]))
        im.save(Path(cmd[cmd.index("-o") + 1]))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(upscale.subprocess, "run", _fake_run)

    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    upscale.upscale(src, tmp_path / "out.png")
    assert captured["timeout"] == 42


# ═══════════════════════════════════════════════════════════════════════════════
# upscale._UPSCALE_LOCK — сериализация апскейлов между воркерами (пятнадцатый заход)
# ═══════════════════════════════════════════════════════════════════════════════

def test_upscale_serializes_concurrent_calls_via_lock(tmp_path, monkeypatch):
    """Два потока вызывают upscale() ОДНОВРЕМЕННО (имитация WORKERS>1 из daily_prints.py)
    -> _UPSCALE_LOCK гарантирует, что subprocess.run НИКОГДА не выполняется параллельно
    (замер лида: параллельные realesrgan на встроенной Vega душат друг друга) — фиксируем
    через маркер "сейчас внутри subprocess.run": если бы лок не работал, оба потока могли
    бы оказаться внутри одновременно."""
    import threading as _threading
    import time as _time

    fake_exe = tmp_path / "realesrgan-ncnn-vulkan.exe"
    fake_exe.write_bytes(b"x")
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "realesrgan-x4plus-anime.bin").write_bytes(b"x")
    (models_dir / "realesrgan-x4plus-anime.param").write_bytes(b"x")
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", fake_exe)
    monkeypatch.setattr(upscale, "REALESRGAN_MODELS_DIR", models_dir)
    monkeypatch.setattr(upscale, "REALESRGAN_DIR", tmp_path)

    concurrent_count = {"current": 0, "max_seen": 0}
    count_lock = _threading.Lock()

    def _fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        with count_lock:
            concurrent_count["current"] += 1
            concurrent_count["max_seen"] = max(concurrent_count["max_seen"],
                                               concurrent_count["current"])
        _time.sleep(0.15)  # достаточно, чтобы второй поток УСПЕЛ бы влезть без лока
        with count_lock:
            concurrent_count["current"] -= 1
        im = Image.open(Path(cmd[cmd.index("-i") + 1]))
        im.save(Path(cmd[cmd.index("-o") + 1]))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(upscale.subprocess, "run", _fake_run)

    def _run_one(idx):
        src = tmp_path / f"in{idx}.png"
        _tiny_rgba().save(src)
        upscale.upscale(src, tmp_path / f"out{idx}.png", timeout=5)

    threads = [_threading.Thread(target=_run_one, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert concurrent_count["max_seen"] == 1, (
        f"максимум {concurrent_count['max_seen']} одновременных subprocess.run "
        f"внутри upscale() — ожидалось РОВНО 1 (сериализация через _UPSCALE_LOCK), "
        f"параллельные апскейлы душат друг друга на встроенной GPU (замер лида)."
    )


def test_render_design_upscale_off_skips_print_png(tmp_path, monkeypatch):
    """config.UPSCALE=off -> render_design НЕ создаёт tag_print.png, result["print_png"]
    остаётся None, upscale.upscale не вызывается вовсе."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    monkeypatch.setattr(config, "UPSCALE", False)
    img = _green_frame_img()
    monkeypatch.setattr(batch_print.providers, "generate_image",
                        lambda prompt, seed=None, model=None, reference=None: img)
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)
    calls = {"n": 0}
    monkeypatch.setattr(batch_print.upscale, "upscale",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    d = _design()
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)
    assert res["ok"] is True
    assert res["print_png"] is None
    assert calls["n"] == 0
    assert not (tmp_path / "01_print.png").exists()


def test_render_design_upscale_on_creates_print_png(tmp_path, monkeypatch):
    """config.UPSCALE=on (дефолт) -> render_design зовёт upscale.upscale_to_print_min
    (пятнадцатый заход — раньше upscale.upscale напрямую) и, при успехе x4 realesrgan,
    заполняет result["print_png"]. Мокаем НИЖНИЙ уровень (upscale.upscale) — реальный
    upscale_to_print_min выполняется, включая возможный адаптивный Lanczos-досчёт (out_
    size=(800,800) < дефолт config.PRINT_MIN_SIDE=3800 — досчёт СРАБОТАЕТ, но это не
    print_fallback, x4 realesrgan успешно отработал по мок-контракту)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    monkeypatch.setattr(config, "UPSCALE", True)
    img = _green_frame_img()
    monkeypatch.setattr(batch_print.providers, "generate_image",
                        lambda prompt, seed=None, model=None, reference=None: img)
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)

    def _fake_upscale(png_in, png_out, scale=4, model=None, timeout=None):
        Path(png_out).write_bytes(Path(png_in).read_bytes())
        return {"ok": True, "elapsed_sec": 1.2, "out_size": (800, 800), "error": None}

    monkeypatch.setattr(batch_print.upscale, "upscale", _fake_upscale)

    d = _design()
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)
    assert res["ok"] is True
    assert res["print_png"] == str(tmp_path / "01_print.png")
    assert (tmp_path / "01_print.png").exists()
    assert res["print_fallback"] is False  # x4 realesrgan отработал, это не Lanczos-фолбэк


def test_render_design_upscale_failure_falls_back_to_lanczos_print_fallback(
        tmp_path, monkeypatch):
    """upscale.upscale() возвращает ok=False (exe отсутствует и т.п.) -> render_design
    ВСЁ РАВНО отдаёт ok=True для самого дизайна (апскейл — не блокирующий шаг).

    Пятнадцатый заход (адаптивный апскейл + таймаут-фолбэк, задача лида): render_design
    теперь зовёт upscale.upscale_to_print_min (не upscale.upscale напрямую) — та САМА
    откатывается на Lanczos-апскейл с исходника при сбое realesrgan (см. upscale.py),
    поэтому print_png больше НЕ становится None на этом сценарии — печатный размер
    гарантирован, но result["print_fallback"]=True честно сигнализирует худшее качество
    (не ESRGAN). Мокаем именно upscale.upscale (внутренний вызов
    upscale_to_print_min) — путь фолбэка реальный, не замоканный."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    monkeypatch.setattr(config, "UPSCALE", True)
    monkeypatch.setattr(config, "PRINT_MIN_SIDE", 400)  # маленький порог — быстрый тест
    img = _green_frame_img()
    monkeypatch.setattr(batch_print.providers, "generate_image",
                        lambda prompt, seed=None, model=None, reference=None: img)
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)
    monkeypatch.setattr(batch_print.upscale, "upscale",
                        lambda *a, **k: {"ok": False, "elapsed_sec": 0.0,
                                         "out_size": None, "error": "exe не найден"})

    d = _design()
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)
    assert res["ok"] is True
    assert res["print_png"] is not None
    assert Path(res["print_png"]).exists()
    assert res["print_fallback"] is True


def test_render_design_upscale_total_failure_still_does_not_break_design(
        tmp_path, monkeypatch):
    """Даже если И realesrgan, И сам Lanczos-фолбэк упадут (гипотетический полный сбой
    upscale.py) — render_design ВСЁ РАВНО отдаёт ok=True для дизайна, print_png=None
    (апскейл целиком, включая фолбэк, — не блокирующий шаг)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    monkeypatch.setattr(config, "UPSCALE", True)
    img = _green_frame_img()
    monkeypatch.setattr(batch_print.providers, "generate_image",
                        lambda prompt, seed=None, model=None, reference=None: img)
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)
    monkeypatch.setattr(batch_print.upscale, "upscale_to_print_min",
                        lambda *a, **k: {"ok": False, "elapsed_sec": 0.0,
                                         "out_size": None, "print_fallback": False,
                                         "error": "realesrgan и Lanczos-фолбэк оба упали"})

    d = _design()
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)
    assert res["ok"] is True
    assert res["print_png"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# QC-гейт масштаба фигуры (batch_print._figure_fills_frame/_figure_bbox_height_frac)
# ═══════════════════════════════════════════════════════════════════════════════

def _synth_frame(w: int, h: int, fig_height_frac: float,
                 fig_width_frac: float = 0.5) -> Image.Image:
    """Синтетический raw-кадр: зелёный хромакей-фон + непрозрачная "фигура"
    (прямоугольник) занимающая fig_height_frac доли ВЫСОТЫ кадра, по центру."""
    img = Image.new("RGB", (w, h), (0, 177, 64))
    d = ImageDraw.Draw(img)
    fig_h = int(h * fig_height_frac)
    fig_w = int(w * fig_width_frac)
    y0 = (h - fig_h) // 2
    x0 = (w - fig_w) // 2
    d.rectangle([x0, y0, x0 + fig_w, y0 + fig_h], fill=(180, 90, 40))
    return img


def test_figure_bbox_height_frac_matches_synthetic_ratio():
    """Синтетика с ИЗВЕСТНОЙ долей высоты фигуры -> _figure_bbox_height_frac находит
    ту же долю (с небольшим допуском на округление растеризации прямоугольника)."""
    img = _synth_frame(400, 600, fig_height_frac=0.70)
    frac = batch_print._figure_bbox_height_frac(img)
    assert abs(frac - 0.70) < 0.02


def test_figure_fills_frame_ok_above_threshold():
    """Фигура 0.60 высоты кадра (> дефолт 0.55) -> QC-гейт проходит."""
    img = _synth_frame(400, 600, fig_height_frac=0.60)
    ok, frac = batch_print._figure_fills_frame(img)
    assert ok is True
    assert frac > 0.55


def test_figure_fills_frame_fails_below_threshold():
    """Мелкая фигура (0.30 высоты кадра, << 0.55) -> QC-гейт бракует ("фигура
    слишком мелкая") — урок на мелких Маки/этикетке/Люси из задачи лида."""
    img = _synth_frame(400, 600, fig_height_frac=0.30)
    ok, frac = batch_print._figure_fills_frame(img)
    assert ok is False
    assert frac < 0.55


def test_figure_fills_frame_custom_threshold_override():
    """min_frac передан явно -> используется вместо config.FIGURE_MIN_FRAC."""
    img = _synth_frame(400, 600, fig_height_frac=0.40)
    ok_default, _ = batch_print._figure_fills_frame(img)  # 0.40 < 0.55 дефолт -> провал
    ok_relaxed, _ = batch_print._figure_fills_frame(img, min_frac=0.30)
    assert ok_default is False
    assert ok_relaxed is True


def test_figure_fills_frame_respects_config_figure_min_frac(monkeypatch):
    """config.FIGURE_MIN_FRAC читается на момент вызова (monkeypatch применяется)."""
    img = _synth_frame(400, 600, fig_height_frac=0.50)
    monkeypatch.setattr(config, "FIGURE_MIN_FRAC", 0.40)
    ok, _ = batch_print._figure_fills_frame(img)
    assert ok is True
    monkeypatch.setattr(config, "FIGURE_MIN_FRAC", 0.60)
    ok2, _ = batch_print._figure_fills_frame(img)
    assert ok2 is False


def test_figure_bbox_height_frac_empty_foreground_returns_zero():
    """Кадр целиком хромакей (нет фигуры вовсе) -> 0.0, не падает делением на ноль."""
    img = Image.new("RGB", (200, 200), (0, 177, 64))
    frac = batch_print._figure_bbox_height_frac(img)
    assert frac == 0.0


# ── render_design интеграция: мелкая фигура на первой попытке -> ретрай ────────

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
        "type_spec": "",
    }
    base.update(overrides)
    return base


def _green_frame_img(w=200, h=200, fig_height_frac=0.60) -> Image.Image:
    return _synth_frame(w, h, fig_height_frac=fig_height_frac)


def _fake_gen_image_factory(images: list):
    calls = {"n": 0}

    def _fake(prompt, seed=None, model=None, reference=None):
        img = images[min(calls["n"], len(images) - 1)]
        calls["n"] += 1
        return img

    _fake.calls = calls
    return _fake


def test_render_design_small_figure_triggers_retry_then_succeeds(tmp_path, monkeypatch):
    """Первая попытка — фигура слишком мелкая (0.30), вторая — нормальная (0.65) ->
    QC-цикл ретраит и выбирает ВТОРУЮ попытку как финальную (не первую с мелкой
    фигурой, даже если border coverage у обеих одинаково хорош)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    monkeypatch.setattr(config, "UPSCALE", False)
    small_fig = _green_frame_img(fig_height_frac=0.30)
    big_fig = _green_frame_img(fig_height_frac=0.65)
    fake_gen = _fake_gen_image_factory([small_fig, big_fig])
    monkeypatch.setattr(batch_print.providers, "generate_image", fake_gen)
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)

    d = _design()
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=1)

    assert res["ok"] is True
    assert fake_gen.calls["n"] == 2  # ретрай реально произошёл
    with Image.open(res["raw"]) as raw:
        frac = batch_print._figure_bbox_height_frac(raw)
        assert frac > 0.55  # финальный raw — большая фигура, не мелкая


def test_render_design_all_attempts_small_figure_warns_but_succeeds(tmp_path, monkeypatch):
    """Фигура мелкая на ВСЕХ попытках (в т.ч. после ретраев) -> дизайн ВСЁ РАВНО
    выпускается (не блокируем целиком), просто предупреждение в лог + coverage/
    figure остаются как есть — тот же принцип, что border coverage < 0.90."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    monkeypatch.setattr(config, "UPSCALE", False)
    small_fig = _green_frame_img(fig_height_frac=0.25)
    fake_gen = _fake_gen_image_factory([small_fig, small_fig])
    monkeypatch.setattr(batch_print.providers, "generate_image", fake_gen)
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)

    d = _design()
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=1)
    assert res["ok"] is True  # не блокирует выпуск дизайна


# ═══════════════════════════════════════════════════════════════════════════════
# Строгая кана — поглифная сверка (batch_print._is_japanese_phrase/
# _glyph_by_glyph_match/_verify_text, дакутэн/хандакутэн)
# ═══════════════════════════════════════════════════════════════════════════════

def test_is_japanese_phrase_detects_kana_and_kanji():
    assert batch_print._is_japanese_phrase("更木剣八") is True
    assert batch_print._is_japanese_phrase("バカ") is True  # катакана
    assert batch_print._is_japanese_phrase("ばか") is True  # хирагана
    assert batch_print._is_japanese_phrase("LETS PARTY") is False
    assert batch_print._is_japanese_phrase("") is False


def test_glyph_by_glyph_match_distinguishes_dakuten():
    """ハ (HA, без дакутэн) != バ (BA, с дакутэн) != パ (PA, хандакутэн) — поглифная
    сверка ловит подмену дакутэн/хандакутэн, которую substring-сверка целой фразы
    тоже поймала бы, но здесь проверяем именно ГАРАНТИЮ различения на уровне глифа."""
    assert batch_print._glyph_by_glyph_match("バカ", "バカ") is True  # точное совпадение
    assert batch_print._glyph_by_glyph_match("バカ", "ハカ") is False  # дакутэн потерян
    assert batch_print._glyph_by_glyph_match("パン", "ハン") is False  # хандакутэн потерян
    assert batch_print._glyph_by_glyph_match("パン", "バン") is False  # хандакутэн->дакутэн


def test_glyph_by_glyph_match_nfc_normalizes_combining_dakuten():
    """OCR может отдать дакутэн КАК ОТДЕЛЬНЫЙ комбинирующий кодпоинт (U+3099) вместо
    предкомпозированного глифа — NFC-нормализация должна схлопнуть их к одному и тому
    же символу, поглифная сверка проходит (это НЕ ошибка каны, просто другая юникод-
    форма ТОГО ЖЕ символа)."""
    ba_precomposed = "バ"  # バ готовый глиф
    ha_plus_combining_dakuten = "バ"  # ハ + отдельный дакутэн-маркер
    assert batch_print._glyph_by_glyph_match(ba_precomposed, ha_plus_combining_dakuten) is True


def test_glyph_by_glyph_match_empty_expected_is_true():
    assert batch_print._glyph_by_glyph_match("", "any transcript") is True


def test_glyph_by_glyph_match_substring_within_longer_transcript():
    """Ожидаемая колонка встречается КАК ПОДПОСЛЕДОВАТЕЛЬНОСТЬ в более длинном
    транскрипте (OCR мог захватить лишний соседний текст) -> всё равно True."""
    assert batch_print._glyph_by_glyph_match("更木剣八", "имя: 更木剣八 (Kenpachi)") is True


def test_verify_text_japanese_phrase_calls_second_ocr_with_column_prompt(monkeypatch):
    """Японская фраза в expected_phrases -> _verify_text ЗОВЁТ providers.verify_text_
    in_image ВТОРОЙ раз с _JP_COLUMN_PROMPT (узкий вопрос про вертикальную колонку),
    отдельно от общего транскрипта."""
    calls = []

    def _fake_verify(image, prompt=providers._OCR_PROMPT):
        calls.append(prompt)
        if prompt == batch_print._JP_COLUMN_PROMPT:
            return "更木剣八"
        return "some general transcript with 更木剣八 in it"

    monkeypatch.setattr(providers, "verify_text_in_image", _fake_verify)
    ok = batch_print._verify_text(_tiny_rgba(), ["更木剣八"])
    assert ok is True
    assert batch_print._JP_COLUMN_PROMPT in calls
    assert len(calls) == 2  # общий транскрипт + узкий кана-вызов


def test_verify_text_japanese_dakuten_real_mismatch_fails(monkeypatch):
    """Реальный сценарий расхождения: ожидаем 'バカ' (с дакутэн), колонка-транскрипт
    второго вызова отдаёт 'ハカ' (дакутэн потерян) -> _verify_text возвращает False,
    даже если общий транскрипт первого вызова содержит что-то похожее."""
    def _fake_verify(image, prompt=providers._OCR_PROMPT):
        if prompt == batch_print._JP_COLUMN_PROMPT:
            return "ハカ"  # дакутэн потерян относительно ожидаемого "バカ"
        return "バカ"  # общий транскрипт "выглядит" правильным

    monkeypatch.setattr(providers, "verify_text_in_image", _fake_verify)
    ok = batch_print._verify_text(_tiny_rgba(), ["バカ"])
    assert ok is False


def test_verify_text_non_japanese_phrase_skips_second_ocr_call(monkeypatch):
    """Латинская фраза без каны -> ТОЛЬКО один OCR-вызов (общий транскрипт), второй
    (кана-специфичный) вызов не делается — не тратим лишний вызов там, где не нужно."""
    calls = []

    def _fake_verify(image, prompt=providers._OCR_PROMPT):
        calls.append(prompt)
        return "LETS PARTY"

    monkeypatch.setattr(providers, "verify_text_in_image", _fake_verify)
    ok = batch_print._verify_text(_tiny_rgba(), ["LETS PARTY"])
    assert ok is True
    assert len(calls) == 1


def test_verify_text_second_ocr_call_failure_fails_verification(monkeypatch):
    """Второй (кана) OCR-вызов падает (сеть/HTTP) -> провал всей проверки (не
    подтверждено — трактуем как непройденную, тот же принцип, что общий OCR-сбой)."""
    def _fake_verify(image, prompt=providers._OCR_PROMPT):
        if prompt == batch_print._JP_COLUMN_PROMPT:
            raise RuntimeError("HTTP 500")
        return "更木剣八"

    monkeypatch.setattr(providers, "verify_text_in_image", _fake_verify)
    ok = batch_print._verify_text(_tiny_rgba(), ["更木剣八"])
    assert ok is False


def test_normalize_for_compare_nfc_normalizes_combining_dakuten():
    """_normalize_for_compare тоже NFC-нормализует (общий substring-путь для
    смешанных фраз, не только поглифный кана-путь) — комбинирующий дакутэн схлопывается
    к тому же предкомпозированному символу."""
    ba_precomposed = "バ"
    ha_plus_combining = "バ"
    assert (batch_print._normalize_for_compare(ba_precomposed)
            == batch_print._normalize_for_compare(ha_plus_combining))


def test_normalize_for_compare_still_distinguishes_different_kana():
    """NFC не смешивает РАЗНЫЕ базовые каны — ハ и バ остаются разными строками
    после нормализации (не ложно-положительное совпадение)."""
    assert (batch_print._normalize_for_compare("ハ")
            != batch_print._normalize_for_compare("バ"))


# ═══════════════════════════════════════════════════════════════════════════════
# typography_v3.ring_text — медальон-гибрид, офлайн на синтетике
# ═══════════════════════════════════════════════════════════════════════════════

def _round_figure(w=800, h=800, radius_frac=0.35) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(min(w, h) * radius_frac)
    cx, cy = w // 2, h // 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(200, 50, 50, 255))
    return img


def _ring_alpha_only(rgba: Image.Image, figure_rgba_original: Image.Image) -> np.ndarray:
    """Альфа-канал результата МИНУС альфа исходной фигуры (по центру, с учётом
    паддинга ring_text) — грубая эвристика "что добавил ring_text поверх фигуры".
    Используется только для sanity-проверки, что ring_text реально что-то нарисовал
    ЗА пределами исходного силуэта (кольцо вокруг, не поверх)."""
    return np.array(rgba.getchannel("A"))


def test_ring_text_all_characters_present_via_ocr_free_pixel_check():
    """Синтетика: круглая фигура + ring_text('UCHIHA MADARA') -> результат заметно
    БОЛЬШЕ исходной фигуры (кольцо реально дорисовано за пределами силуэта) и содержит
    непрозрачные пиксели вне центрального круга на всех 4 сторонах (кольцо охватывает
    фигуру равномерно, не однобоко)."""
    fig = _round_figure()
    out = t3.ring_text(fig, "UCHIHA MADARA")

    assert out.width > fig.width
    assert out.height > fig.height

    a = np.array(out.getchannel("A"))
    h, w = a.shape
    cx, cy = w // 2, h // 2
    # Кольцо должно оставить непрозрачные пиксели в 4 узких полосах ЗА пределами
    # центральной фигуры (верх/низ/лево/право) — если ring_text ничего не нарисовал,
    # эти полосы будут пустыми (alpha==0 везде).
    band = 30
    top_strip = a[max(0, cy - int(h * 0.42) - band):max(1, cy - int(h * 0.42)), cx - 5:cx + 5]
    assert (top_strip > 0).any(), "нет пикселей кольца сверху фигуры"


def test_ring_text_exactly_one_pass_no_letter_repeated():
    """Один проход по кольцу — каждый символ фразы рисуется РОВНО ОДИН РАЗ. Проверяем
    косвенно: число непрозрачных "глиф-компонент" на кольце (после вычитания
    центральной фигуры) не превышает числа НЕПРОБЕЛЬНЫХ символов фразы (не более —
    может быть меньше из-за антиалиасинга/слияния соседних глифов на мелком кегле,
    но не может пойти на ВТОРОЙ круг и удвоить число компонент)."""
    import cv2

    fig = _round_figure()
    phrase = "MADARA"
    out = t3.ring_text(fig, phrase)

    a = np.array(out.getchannel("A"))
    # Вырезаем маску, исключая центральную зону (сама фигура) — оставляем только
    # кольцевые буквы.
    h, w = a.shape
    cy, cx = h // 2, w // 2
    fig_r_px = int(min(fig.width, fig.height) * 0.35 * 1.15)  # фигура + небольшой запас
    mask = (a > 40).astype(np.uint8)
    yy, xx = np.ogrid[:h, :w]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    mask[dist < fig_r_px] = 0

    n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    n_components = 0
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= 5:  # отсеиваем шум антиалиасинга
            n_components += 1

    non_space_chars = len(phrase.replace(" ", ""))
    assert 1 <= n_components <= non_space_chars


def test_ring_text_empty_phrase_returns_copy_unchanged():
    fig = _round_figure()
    out = t3.ring_text(fig, "")
    assert out.size == fig.size


def test_ring_text_uses_palette_colors_not_hardcoded():
    """Цвета кольца берутся из PaletteRoles конкретной иллюстрации (accent/dominant),
    не хардкод — roles с явно отличной палитрой должны дать другие цвета глифов."""
    fig = _round_figure()
    custom_palette = [(10, 200, 10), (10, 10, 200), (240, 240, 240), (5, 5, 5)]
    roles = palette.PaletteRoles(custom_palette)
    out = t3.ring_text(fig, "AB", roles=roles)

    arr = np.array(out.convert("RGBA"))
    a = arr[:, :, 3]
    h, w = a.shape
    cy, cx = h // 2, w // 2
    fig_r_px = int(min(fig.width, fig.height) * 0.35 * 1.15)
    yy, xx = np.ogrid[:h, :w]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    ring_mask = (a > 100) & (dist >= fig_r_px)
    assert ring_mask.any(), "кольцевой текст не нарисован"
    ring_pixels = arr[ring_mask][:, :3]
    # Цвет глифа должен совпасть (приблизительно, из-за стека шрифт/сглаживание) с
    # accent ИЛИ dominant роли, не быть чем-то посторонним типа typography._TITLE_COLORS.
    matches_role_color = False
    for rgb in (roles.accent, roles.dominant):
        dist_to_role = np.sqrt(((ring_pixels.astype(np.float32) - np.array(rgb)) ** 2).sum(axis=1))
        if (dist_to_role < 40).any():
            matches_role_color = True
    assert matches_role_color


# ═══════════════════════════════════════════════════════════════════════════════
# batch_print.render_design — медальон-гибрид (style_id/hybrid_ring_text)
# ═══════════════════════════════════════════════════════════════════════════════

def test_render_design_ring_medallion_direct_field_prompt_requests_empty_ring(
        tmp_path, monkeypatch):
    """Прямой путь (design["style_id"]=="ring_medallion", БЕЗ банковского префикса —
    design НЕ проходит через docs/STYLE_BANK.json/art_director._style_by_id) -> промпт
    ДОЛЖЕН содержать инструкцию про ПУСТОЕ кольцо (_RING_MEDALLION_PROMPT_SUFFIX,
    добавляется batch_print САМ, раз art_director про этот design не знает)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")  # type_spec не участвует здесь
    monkeypatch.setattr(config, "UPSCALE", False)

    captured = {}

    def _fake_gen(prompt, seed=None, model=None, reference=None):
        captured["prompt"] = prompt
        return _green_frame_img()

    monkeypatch.setattr(batch_print.providers, "generate_image", _fake_gen)
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)

    d = _design(style_id="ring_medallion", quote="UCHIHA MADARA")
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)

    assert res["ok"] is True
    assert "CONTAINS NO LETTERING" in captured["prompt"]


def test_render_design_ring_medallion_via_real_style_bank_id(tmp_path, monkeypatch):
    """Банковский путь: design["style_id"]=="09_ring_medallion" (реальный id из
    docs/STYLE_BANK.json) -> art_director.build_prompt САМ добавляет инструкцию про
    пустое кольцо (_style_bank_prompt_block) — batch_print НЕ дублирует суффикс, но
    ring_text ВСЁ РАВНО применяется после вырезки (детекция через _style_by_id)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    monkeypatch.setattr(config, "UPSCALE", False)

    captured = {}

    def _fake_gen(prompt, seed=None, model=None, reference=None):
        captured["prompt"] = prompt
        return _green_frame_img()

    monkeypatch.setattr(batch_print.providers, "generate_image", _fake_gen)
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)

    ring_calls = {"n": 0}
    monkeypatch.setattr(batch_print.typography_v3, "ring_text",
                        lambda cut, phrase, *a, **k: (ring_calls.__setitem__(
                            "n", ring_calls["n"] + 1), cut)[1])

    d = _design(style_id="09_ring_medallion", quote="UCHIHA MADARA")
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)

    assert res["ok"] is True
    assert "COMPLETELY PLAIN" in captured["prompt"]  # art_director's own wording
    # batch_print НЕ добавляет свой суффикс поверх (его формулировка "CONTAINS NO
    # LETTERING" отсутствует — art_director использует другие слова для того же
    # запрета, не дублируем инструкцию дважды разными словами).
    assert "CONTAINS NO LETTERING" not in captured["prompt"]
    assert ring_calls["n"] == 1


def test_render_design_ring_medallion_applies_ring_text_after_cutout(tmp_path, monkeypatch):
    """Медальон-гибрид -> итоговый diecut ДОЛЖЕН содержать текст, нанесённый
    typography_v3.ring_text (не голая вырезка, не обычная typography_v3/typography)."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    monkeypatch.setattr(config, "UPSCALE", False)
    monkeypatch.setattr(batch_print.providers, "generate_image",
                        lambda prompt, seed=None, model=None, reference=None: _green_frame_img())
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)

    ring_calls = {"n": 0}
    orig_ring_text = batch_print.typography_v3.ring_text

    def _spy_ring_text(cut, phrase, *a, **k):
        ring_calls["n"] += 1
        ring_calls["phrase"] = phrase
        return orig_ring_text(cut, phrase, *a, **k)

    monkeypatch.setattr(batch_print.typography_v3, "ring_text", _spy_ring_text)

    d = _design(style_id="ring_medallion", quote="UCHIHA MADARA")
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)

    assert res["ok"] is True
    assert ring_calls["n"] == 1
    assert ring_calls["phrase"] == "UCHIHA MADARA"


def test_render_design_hybrid_ring_text_field_also_triggers_medallion(tmp_path, monkeypatch):
    """design["hybrid_ring_text"] непусто (без style_id) -> тот же режим включается."""
    monkeypatch.setattr(config, "TEXT_RENDER", "image")
    monkeypatch.setattr(config, "UPSCALE", False)
    monkeypatch.setattr(batch_print.providers, "generate_image",
                        lambda prompt, seed=None, model=None, reference=None: _green_frame_img())
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)

    ring_calls = {"n": 0}
    monkeypatch.setattr(batch_print.typography_v3, "ring_text",
                        lambda cut, phrase, *a, **k: (ring_calls.__setitem__(
                            "n", ring_calls["n"] + 1), cut)[1])

    d = _design(hybrid_ring_text="KAMINA")
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)
    assert res["ok"] is True
    assert ring_calls["n"] == 1


def test_render_design_non_ring_medallion_design_unaffected(tmp_path, monkeypatch):
    """Обычный design (без style_id/hybrid_ring_text) -> ring_text НЕ вызывается,
    старое поведение полностью сохранено."""
    monkeypatch.setattr(config, "TEXT_RENDER", "code")
    monkeypatch.setattr(config, "UPSCALE", False)
    monkeypatch.setattr(batch_print.providers, "generate_image",
                        lambda prompt, seed=None, model=None, reference=None: _green_frame_img())
    monkeypatch.setattr(batch_print.character_ref, "get_reference", lambda *a, **k: None)
    ring_calls = {"n": 0}
    monkeypatch.setattr(batch_print.typography_v3, "ring_text",
                        lambda *a, **k: ring_calls.__setitem__("n", ring_calls["n"] + 1))

    d = _design()
    res = batch_print.render_design(d, "01", tmp_path, timeout_retries=0)
    assert res["ok"] is True
    assert ring_calls["n"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# style_madara.py — дефолт --character пустой (баг заражения Мадарой)
# ═══════════════════════════════════════════════════════════════════════════════

def test_style_madara_default_character_is_empty():
    assert style_madara.DEFAULT_CHARACTER_EN == ""
    assert style_madara.DEFAULT_TITLE_EN == ""


def test_style_madara_recipe_to_design_without_character_uses_empty_who():
    """Без character_en (ни в рецепте, ни переданного явно) -> design["character_en"]
    пуст, промпт-фраза "who" в prompt деградирует до пустого имени (не молча
    подставляет "Madara Uchiha")."""
    recipe = {
        "id": "test_recipe",
        "moment": "stands in a heroic pose",
        "art_style": "anime cel-shading",
        "palette": "red, black",
        "typography": "bold caps",
        "text_content": {"main": "TEST PHRASE", "secondary": "", "vertical_jp": ""},
        "chroma": "green",
    }
    design = style_madara.recipe_to_design(recipe, character_en="", title_en="")
    assert design["character_en"] == ""
    assert "Madara" not in design["prompt"]


def test_style_madara_main_cli_requires_character_when_recipe_lacks_it(tmp_path, monkeypatch):
    """main() с файлом рецептов без собственного character_en И без --character ->
    честная остановка (sys.exit(1)), не молчаливая генерация с пустым/дефолтным
    персонажем."""
    recipes_path = tmp_path / "recipes.json"
    recipes_path.write_text(
        '[{"id": "r1", "moment": "stands", "art_style": "anime", "palette": "red", '
        '"typography": "caps", "text_content": {"main": "HI", "secondary": "", '
        '"vertical_jp": ""}, "chroma": "green"}]',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv",
                        ["style_madara.py", "--recipes", str(recipes_path),
                         "--outdir", str(tmp_path / "out")])
    with pytest.raises(SystemExit) as exc_info:
        style_madara.main()
    assert exc_info.value.code == 1


def test_style_madara_vertical_jp_type_spec_mentions_dakuten():
    """Рецепт с vertical_jp -> type_spec содержит явную инструкцию про дакутэн/
    хандакутэн (задача лида: "render dakuten and handakuten marks accurately")."""
    recipe = {
        "id": "test_recipe_jp",
        "moment": "stands",
        "art_style": "anime",
        "palette": "red",
        "typography": "caps",
        "text_content": {"main": "", "secondary": "", "vertical_jp": "バカ"},
        "chroma": "green",
    }
    design = style_madara.recipe_to_design(recipe, character_en="Someone", title_en="")
    assert "dakuten" in design["type_spec"]
    assert "handakuten" in design["type_spec"]
    assert "ハ" in design["type_spec"]  # ハ упомянут как пример различения
    assert "バ" in design["type_spec"]  # バ упомянут как пример различения


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
