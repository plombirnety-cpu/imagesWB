# -*- coding: utf-8 -*-
"""Тесты vision-QC-гейта анатомии рук (жалоба владельца 2026-07-11: у персонажей
иногда 3 руки / одна рука / нет руки). Двухуровневая защита:
  1) промпт-усиление art_director._ANATOMY_BLOCK (ВСЕГДА, бесплатно);
  2) vision-QC-гейт batch_print._verify_anatomy + providers.verify_anatomy_in_image
     (за флагом config.ANATOMY_QC), тот же ретрай/best-effort принцип, что OCR-контроль
     спеллинга.

Полностью офлайн: providers.verify_anatomy_in_image и generate_image замоканы, сеть
не трогается. conftest.py форсит config.ANATOMY_QC=False по умолчанию (чтобы старые
тесты не стучались в сеть); тесты ниже включают гейт явно через
monkeypatch.setattr(config, "ANATOMY_QC", True) поверх фикстуры.

Запуск: cd print-factory-nb && python -m pytest tests/test_anatomy_qc.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image, ImageDraw  # noqa: E402

import art_director  # noqa: E402
import batch_print  # noqa: E402
import config  # noqa: E402

REF_GREEN = (0, 177, 64)


def _fig_design(**ov) -> dict:
    """Фигуративный дизайн (has_human_figure=True) без текста (type_spec/slogan пусты
    -> OCR-контроль не участвует, чистый офлайн-тест только гейта анатомии)."""
    base = {
        "prompt": "A warrior swinging a katana in a dynamic pose.",
        "chroma": "green", "slogan": "", "slogan_color": "orange", "kana": "",
        "character_en": "", "title_en": "", "signature_props": "", "text_mode": "none",
        "text_modes_v3": [], "quote": "", "name_jp": "", "mood": "", "type_spec": "",
        "has_human_figure": True,
    }
    base.update(ov)
    return base


def _gen_img(bg=REF_GREEN, w=220, h=300) -> Image.Image:
    """Кадр с правильным зелёным хромакеем и высокой фигурой (bbox >= 0.55 высоты —
    проходит QC-гейт масштаба фигуры; рамка кадра = зелёный эталон -> border QC ок)."""
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([w * 0.3, h * 0.08, w * 0.7, h * 0.94], fill=(180, 60, 50))
    return img


# ═══════════════════════ unit: _verify_anatomy — контракт гейта ═══════════════════════

def test_verify_anatomy_skips_non_figurative(monkeypatch):
    """Не-фигуративная тема (животное-мем/машина/знак, has_human_figure=False):
    гейт пропускается БЕЗ vision-вызова (экономия RPD-квоты), даже если QC включён."""
    monkeypatch.setattr(config, "ANATOMY_QC", True)
    called = []
    monkeypatch.setattr(batch_print.providers, "verify_anatomy_in_image",
                        lambda *a, **k: called.append(1) or {"anomaly": True})
    ok, info = batch_print._verify_anatomy(_fig_design(has_human_figure=False), _gen_img())
    assert ok is True and info == {}
    assert not called, "verify_anatomy_in_image НЕ должен вызываться для не-фигуративных тем"


def test_verify_anatomy_skips_when_qc_off(monkeypatch):
    """config.ANATOMY_QC=off (владелец выключил на исходе квоты): гейт пропускается
    без vision-вызова — фигуративная тема принимается как есть."""
    monkeypatch.setattr(config, "ANATOMY_QC", False)
    called = []
    monkeypatch.setattr(batch_print.providers, "verify_anatomy_in_image",
                        lambda *a, **k: called.append(1) or {"anomaly": True})
    ok, info = batch_print._verify_anatomy(_fig_design(), _gen_img())
    assert ok is True and info == {}
    assert not called


def test_verify_anatomy_passes_clean(monkeypatch):
    """Чистая анатомия (2 руки, anomaly=false) — гейт пройден."""
    monkeypatch.setattr(config, "ANATOMY_QC", True)
    monkeypatch.setattr(batch_print.providers, "verify_anatomy_in_image",
                        lambda *a, **k: {"arms_visible": 2, "anomaly": False, "reason": ""})
    ok, info = batch_print._verify_anatomy(_fig_design(), _gen_img())
    assert ok is True and info.get("arms_visible") == 2


def test_verify_anatomy_flags_anomaly(monkeypatch):
    """Третья рука (anomaly=true) — гейт возвращает ok=False (пойдёт ретрай)."""
    monkeypatch.setattr(config, "ANATOMY_QC", True)
    monkeypatch.setattr(batch_print.providers, "verify_anatomy_in_image",
                        lambda *a, **k: {"arms_visible": 3, "anomaly": True, "reason": "third arm"})
    ok, info = batch_print._verify_anatomy(_fig_design(), _gen_img())
    assert ok is False and info.get("anomaly") is True


def test_verify_anatomy_vision_error_is_conservative(monkeypatch):
    """Сбой самого vision-вызова -> ok=False (консервативно, как _verify_text:
    лучше лишний ретрай, чем молча пропустить возможную аномалию)."""
    monkeypatch.setattr(config, "ANATOMY_QC", True)

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(batch_print.providers, "verify_anatomy_in_image", _boom)
    ok, info = batch_print._verify_anatomy(_fig_design(), _gen_img())
    assert ok is False and "error" in info


# ═══════════ integration render_design: ретрай при аномалии -> best-effort ═══════════

def test_render_design_anatomy_retry_then_best_effort(tmp_path, monkeypatch):
    """Анатомия аномальна на КАЖДОЙ попытке: гейт вызывает ретраи, но дизайн ВСЁ РАВНО
    выпускается (best-effort, не блокируем) с честным anatomy_warning=True."""
    monkeypatch.setattr(config, "ANATOMY_QC", True)
    gen_calls = []

    def _gen(*a, **k):
        gen_calls.append(1)
        return _gen_img()

    monkeypatch.setattr(batch_print.providers, "generate_image", _gen)
    monkeypatch.setattr(batch_print.providers, "verify_anatomy_in_image",
                        lambda *a, **k: {"arms_visible": 3, "anomaly": True, "reason": "extra arm"})

    outdir = tmp_path / "anime" / "x"
    outdir.mkdir(parents=True)
    meta = tmp_path / "_meta" / "x01_design.json"
    res = batch_print.render_design(_fig_design(), "x01", outdir, green_only=True,
                                    design_json_path=meta)

    assert res["ok"] is True, "best-effort: дизайн выпускается даже при неисправимой анатомии"
    assert res["anatomy_warning"] is True, "нужно честное предупреждение об анатомии"
    assert len(gen_calls) >= 2, "аномалия должна вызвать хотя бы один ретрай генерации"
    assert (outdir / "x01.png").exists()


def test_render_design_clean_anatomy_no_warning(tmp_path, monkeypatch):
    """Чистая анатомия — дизайн ок, anatomy_warning=False."""
    monkeypatch.setattr(config, "ANATOMY_QC", True)
    monkeypatch.setattr(batch_print.providers, "generate_image", lambda *a, **k: _gen_img())
    monkeypatch.setattr(batch_print.providers, "verify_anatomy_in_image",
                        lambda *a, **k: {"arms_visible": 2, "anomaly": False, "reason": ""})

    outdir = tmp_path / "anime" / "y"
    outdir.mkdir(parents=True)
    meta = tmp_path / "_meta" / "y01_design.json"
    res = batch_print.render_design(_fig_design(), "y01", outdir, green_only=True,
                                    design_json_path=meta)

    assert res["ok"] is True and res["anatomy_warning"] is False


# ═══════════════════════ промпт-усиление (всегда, бесплатно) ═══════════════════════

def test_anatomy_block_demands_exactly_two_arms():
    """Промпт-блок анатомии существует и жёстко требует ровно две руки — это
    защита на уровне промпта, работает независимо от config.ANATOMY_QC."""
    block = art_director._ANATOMY_BLOCK
    assert block, "_ANATOMY_BLOCK не должен быть пустым"
    up = block.upper()
    assert "TWO" in up and "ARM" in up, "промпт должен требовать ровно две руки"
