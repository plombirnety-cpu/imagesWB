# -*- coding: utf-8 -*-
"""Тесты режима green_only (заказ владельца, мега-партия D:\\800, 2026-07-10):
на успешный принт — РОВНО ОДИН файл <base>.png, сохраняемый КАК ЕСТЬ, вообще БЕЗ
обработки пикселей — фон остаётся ТЕМ хромакеем, что реально сгенерился (зелёным
ИЛИ синим), без вырезки/апскейла/ongreen; паспорт design.json — в зеркальную
_meta; смета — по фактически полученным изображениям (429/500 без картинки
бесплатны); --migrate-legacy переводит старую (full-set) партию в новую
раскладку без генераций.

Правка владельца 2026-07-10 (дословно): "правило синего фона если зеленые
элементы на персонаже, сохраняем. Просто не вырезай фон и все, мне нужны файлы
с фоном Зеленый или синий, в зависимости от принта" — render_design(green_only=
True) БОЛЬШЕ НЕ вызывает chroma_remove.recolor_bg вообще (ни для blue, ни для
green); файл в тематической папке = raw бит-в-бит для ОБОИХ цветов хромакея.
Правило арт-директора "зелёные элементы у персонажа -> синий хромакей"
(art_director._chroma_bg, определяет, каким хромакеем ИДЁТ генерация) не
трогается и здесь не тестируется. chroma_remove.recolor_bg САМА функция и её
юнит-тесты ниже (перекраска синего в эталонный зелёный) ОСТАВЛЕНЫ как рабочая
утилита на будущее — просто больше не вызываются из green_only-пути
render_design.

Полностью офлайн — генерация мокается (как в остальных тестах проекта), сеть не
используется.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_green_only_mode.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import batch_print  # noqa: E402
import chroma_remove  # noqa: E402
import config  # noqa: E402
import mega_batch_run  # noqa: E402

REF_GREEN = (0, 177, 64)
REF_BLUE = (0, 71, 255)


# ═══════════════════════════════════════════════════════════════════════════════
# chroma_remove.recolor_bg — перекраска ТОЛЬКО фона, фигура бит-в-бит
# ═══════════════════════════════════════════════════════════════════════════════

def _blue_bg_figure_img(w=240, h=320, bg=REF_BLUE, fig=(200, 40, 40)) -> Image.Image:
    """Синтетика: сплошной синий хромакей + прямоугольная «фигура» контрастного
    цвета (далеко от синего в CrCb — зона a==1, не должна меняться ни на бит)."""
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([w * 0.25, h * 0.10, w * 0.75, h * 0.92], fill=fig)
    return img


def test_recolor_bg_blue_becomes_reference_green_and_figure_untouched():
    """Фон (чистый синий хромакей) обязан стать РОВНО эталонным зелёным
    RGB(0,177,64); пиксели фигуры — бит-в-бит исходные."""
    img = _blue_bg_figure_img()
    before = np.array(img)
    out = np.array(chroma_remove.recolor_bg(img, src_chroma="blue"))

    # углы кадра (чистый фон) — ровно эталонный зелёный
    for y, x in ((2, 2), (2, -3), (-3, 2), (-3, -3)):
        assert tuple(out[y, x]) == REF_GREEN, (
            f"угол ({y},{x}) = {tuple(out[y, x])}, ожидался эталон {REF_GREEN}")

    # центр фигуры — бит-в-бит (никакой обработки пикселей фигуры)
    h, w = before.shape[:2]
    cy, cx = h // 2, w // 2
    region_before = before[int(h * 0.3):int(h * 0.8), int(w * 0.35):int(w * 0.65)]
    region_after = out[int(h * 0.3):int(h * 0.8), int(w * 0.35):int(w * 0.65)]
    assert np.array_equal(region_before, region_after), (
        "пиксели фигуры изменились — recolor_bg обязан трогать ТОЛЬКО фон")
    assert tuple(out[cy, cx]) == (200, 40, 40)


def test_recolor_bg_leaves_no_blue_halo_on_edge():
    """Полутоновая кромка (АА-пиксель синий+фигура) должна уходить К ЗЕЛЁНОМУ:
    после перекраски в кадре не остаётся пикселей, чей цвет ближе к синему
    хромакею, чем к чему-либо ещё (нет синего ореола)."""
    img = _blue_bg_figure_img()
    # смоделировать АА-кромку: полупрозрачное смешение фигуры с фоном по границе
    a = np.array(img).astype(np.float32)
    h, w = a.shape[:2]
    x_edge = int(w * 0.25)
    blue = np.array(REF_BLUE, dtype=np.float32)
    a[int(h * 0.10):int(h * 0.92), x_edge - 1] = (a[int(h * 0.10):int(h * 0.92),
                                                    x_edge] * 0.5 + blue * 0.5)
    img = Image.fromarray(a.astype(np.uint8), "RGB")

    out = np.array(chroma_remove.recolor_bg(img, src_chroma="blue")).astype(np.float32)
    # ни один пиксель результата не должен остаться «по сути синим хромакеем»:
    # расстояние до эталонного синего у всех пикселей больше, чем было у чистого фона
    dist_blue = np.sqrt(((out - blue.reshape(1, 1, 3)) ** 2).sum(axis=2))
    assert float(dist_blue.min()) > 40.0, (
        f"остался почти-синий пиксель (min dist до эталонного синего = "
        f"{dist_blue.min():.1f}) — синий ореол не погашен")


def test_recolor_bg_robust_to_contaminated_border_metal_cover_incident():
    """РЕГРЕСС инцидента «Зоро» (28_metal_cover, GOTCHAS): декор у самого края
    кадра утаскивает голую медиану рамки (_border_key) в грязный серый — на
    реальном raw ключ уезжал на ~57 CrCb от настоящего синего фона, перекраска по
    такому ключу фон бы НЕ нашла. _robust_bg_key фильтрует пиксели рамки
    близостью к эталону запрошенного хромакея — серый декор отбрасывается ДО
    медианы. Синтетика: серая рамка-декор по периметру, внутри настоящий синий
    фон + фигура — фон обязан перекраситься, декор и фигура остаться."""
    w, h = 260, 340
    img = Image.new("RGB", (w, h), (110, 115, 120))  # серый «декор» всем кадром
    d = ImageDraw.Draw(img)
    border = 12
    d.rectangle([border, border, w - border - 1, h - border - 1], fill=REF_BLUE)
    d.rectangle([w * 0.3, h * 0.2, w * 0.7, h * 0.85], fill=(230, 210, 60))

    out = np.array(chroma_remove.recolor_bg(img, src_chroma="blue"))
    # синий фон внутри рамки стал эталонным зелёным
    assert tuple(out[border + 5, border + 5]) == REF_GREEN
    assert tuple(out[h - border - 6, w - border - 6]) == REF_GREEN
    # серый декор рамки не тронут (это «фигурный» материал, не фон)
    assert tuple(out[3, 3]) == (110, 115, 120)
    # фигура не тронута
    assert tuple(out[int(h * 0.5), int(w * 0.5)]) == (230, 210, 60)


# ═══════════════════════════════════════════════════════════════════════════════
# batch_print.render_design(green_only=True) — состав файлов и содержимое
# ═══════════════════════════════════════════════════════════════════════════════

def _design(**overrides) -> dict:
    base = {
        "prompt": "A swordsman in a dynamic pose, energy swirling around the blade.",
        "chroma": "green",
        "slogan": "",
        "slogan_color": "orange",
        "kana": "",
        "character_en": "",
        "title_en": "",
        "signature_props": "",
        "text_mode": "none",
        "text_modes_v3": [],
        "quote": "",
        "name_jp": "",
        "mood": "",
        "type_spec": "",  # пусто -> OCR-контроль не участвует (офлайн-тест)
    }
    base.update(overrides)
    return base


def _gen_img(bg, w=220, h=300) -> Image.Image:
    """Кадр с правильным хромакеем bg и высокой фигурой (bbox >= 0.55 высоты —
    проходит QC-гейт масштаба)."""
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([w * 0.3, h * 0.08, w * 0.7, h * 0.94], fill=(180, 60, 50))
    return img


def _forbid(name):
    def _boom(*a, **k):
        raise AssertionError(f"{name} НЕ должен вызываться в режиме green_only")
    return _boom


def test_render_design_green_only_blue_chroma_saved_bit_exact_no_recolor(
        tmp_path, monkeypatch):
    """chroma=blue: в outdir РОВНО ОДИН файл <tag>.png, сохранённый БИТ-В-БИТ
    (правка владельца 2026-07-10 — recolor_bg из green_only-пути убран, фон
    остаётся синим, каким сгенерился); паспорт design.json — по переданному
    зеркальному пути; вырезка/апскейл/ongreen НЕ вызываются и НЕ сохраняются."""
    gen = _gen_img(REF_BLUE)
    monkeypatch.setattr(batch_print.providers, "generate_image",
                        lambda *a, **k: gen)
    monkeypatch.setattr(batch_print.chroma_remove, "cutout_green",
                        _forbid("chroma_remove.cutout_green"))
    monkeypatch.setattr(batch_print.chroma_remove, "recolor_bg",
                        _forbid("chroma_remove.recolor_bg"))
    monkeypatch.setattr(batch_print.upscale, "upscale_to_print_min",
                        _forbid("upscale.upscale_to_print_min"))

    outdir = tmp_path / "anime" / "one_piece"
    outdir.mkdir(parents=True)
    meta_path = tmp_path / "_meta" / "anime" / "one_piece" / "z01_design.json"

    res = batch_print.render_design(_design(chroma="blue"), "z01", outdir,
                                    green_only=True, design_json_path=meta_path)

    assert res["ok"] is True
    assert res["green"] == str(outdir / "z01.png")
    assert res["images"] == 1
    # РОВНО один файл в тематической папке, без суффиксов
    files = sorted(p.name for p in outdir.iterdir())
    assert files == ["z01.png"], f"в тематической папке лишние файлы: {files}"
    # паспорт лежит в зеркальной _meta и воспроизводит design
    assert meta_path.exists()
    assert json.loads(meta_path.read_text(encoding="utf-8"))["chroma"] == "blue"
    # фон НЕ перекрашен — файл бит-в-бит равен сгенерированной картинке (синий
    # хромакей остаётся синим, как заказал владелец)
    saved = np.array(Image.open(outdir / "z01.png").convert("RGB"))
    assert np.array_equal(saved, np.array(gen)), (
        "blue-генерация обязана сохраняться бит-в-бит, без перекраски фона")
    out = np.array(Image.open(outdir / "z01.png").convert("RGB"))
    assert tuple(out[2, 2]) == REF_BLUE
    assert res["raw"] is None and res["diecut"] is None and res["ongreen"] is None \
        and res["print_png"] is None


def test_render_design_green_only_green_chroma_saved_bit_exact(tmp_path, monkeypatch):
    """chroma=green: raw сохраняется КАК ЕСТЬ — все пиксели файла бит-в-бит равны
    сгенерированной картинке (никакой обработки, включая juice/перекраску);
    фактический зелёный nano-banana не обязан быть эталонным."""
    actual_green = (11, 169, 58)  # реальный зелёный прошлых прогонов (не эталон)
    gen = _gen_img(actual_green)
    monkeypatch.setattr(batch_print.providers, "generate_image",
                        lambda *a, **k: gen)

    outdir = tmp_path / "cat"
    outdir.mkdir()
    res = batch_print.render_design(_design(chroma="green"), "g01", outdir,
                                    green_only=True,
                                    design_json_path=tmp_path / "_meta" / "g01_design.json")

    assert res["ok"] is True
    saved = np.array(Image.open(outdir / "g01.png").convert("RGB"))
    assert np.array_equal(saved, np.array(gen)), (
        "green-генерация обязана сохраняться бит-в-бит, без какой-либо обработки")
    assert sorted(p.name for p in outdir.iterdir()) == ["g01.png"]


def test_render_design_full_set_behaviour_unchanged_by_default(tmp_path, monkeypatch):
    """Обратная совместимость: БЕЗ green_only render_design работает по-старому —
    raw+diecut+ongreen+design.json рядом (апскейл выключаем конфигом)."""
    gen = _gen_img(REF_GREEN)
    monkeypatch.setattr(batch_print.providers, "generate_image",
                        lambda *a, **k: gen)
    monkeypatch.setattr(config, "UPSCALE", False)

    outdir = tmp_path / "old"
    outdir.mkdir()
    res = batch_print.render_design(_design(chroma="green"), "01", outdir)

    assert res["ok"] is True
    names = sorted(p.name for p in outdir.iterdir())
    assert names == ["01_design.json", "01_diecut.png", "01_ongreen.png", "01_raw.png"]
    assert res["images"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Смета по фактически полученным изображениям (429/500 без картинки — бесплатны)
# ═══════════════════════════════════════════════════════════════════════════════

def test_process_one_counts_cost_only_for_received_images(tmp_path, monkeypatch):
    """Первая попытка — HTTP 429 (картинки НЕТ, с баланса не списывается), вторая
    — успех. В журнал должно попасть attempts=2, images=1, est_cost = 1 * цена
    (НЕ 2 * цена, как считал старый код по attempts)."""
    calls = {"n": 0}

    def _fake_gen(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Gemini (nano-banana) не отдал картинку: HTTP 429")
        return _gen_img(REF_GREEN)

    monkeypatch.setattr(batch_print.providers, "generate_image", _fake_gen)
    monkeypatch.setattr(mega_batch_run.art_director, "make_ideas",
                        lambda *a, **k: [_design(chroma="green")])
    monkeypatch.setattr(config, "IMAGE_PROVIDER", "gemini")

    rec = {"seq": 7, "filename_base": "zoro_test_07", "category": "anime/one_piece",
           "theme": "Зоро", "format": "diecut", "style_pref": None}
    journal_rec = mega_batch_run._process_one(rec, tmp_path, None)

    assert journal_rec["status"] == "done"
    assert journal_rec["mode"] == "green_only"
    assert journal_rec["attempts"] == 2
    assert journal_rec["images"] == 1
    per = config.COST_PER_IMAGE_USD["gemini"]
    assert journal_rec["est_cost_usd"] == round(1 * per, 4), (
        f"смета {journal_rec['est_cost_usd']} — 429-попытка без картинки не должна "
        f"считаться (ожидалось {round(1 * per, 4)})")
    # раскладка green_only: одна картинка в тематической папке + паспорт в _meta
    cat_dir = tmp_path / "anime" / "one_piece"
    assert sorted(p.name for p in cat_dir.iterdir()) == ["zoro_test_07.png"]
    meta = tmp_path / "_meta" / "anime" / "one_piece" / "zoro_test_07_design.json"
    assert meta.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# Резюмируемость при смене режима + миграция старой партии
# ═══════════════════════════════════════════════════════════════════════════════

def test_resume_skips_old_format_done_records_and_retries_failed(tmp_path, monkeypatch):
    """Смена режима НЕ ломает резюм: done-запись СТАРОГО формата (без полей
    mode/images) остаётся done и пропускается, failed повторяется."""
    outroot = tmp_path / "800"
    outroot.mkdir()
    journal = outroot / "_journal.jsonl"
    old_done = {"seq": 1, "filename_base": "x1", "category": "c", "theme": "t",
                "format": "diecut", "style_pref": None, "status": "done",
                "attempts": 1, "error": None, "est_cost_usd": 0.04,
                "print_fallback": False, "ts": "2026-07-10 00:00:00"}
    old_failed = dict(old_done, seq=2, filename_base="x2", status="failed")
    journal.write_text(json.dumps(old_done) + "\n" + json.dumps(old_failed) + "\n",
                       encoding="utf-8")

    plan = [{"seq": 1, "filename_base": "x1", "category": "c", "theme": "t",
             "format": "diecut", "style_pref": None},
            {"seq": 2, "filename_base": "x2", "category": "c", "theme": "t",
             "format": "diecut", "style_pref": None}]
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    processed = []

    def _fake_process(rec, _outroot, _recent, _full_set=False):
        processed.append(rec["filename_base"])
        return mega_batch_run._empty_journal_record(rec, "done")

    monkeypatch.setattr(mega_batch_run, "_process_one", _fake_process)
    summary = mega_batch_run.run_mega_batch(plan_path, outroot, workers=1,
                                            budget_cap_usd=100.0)

    assert processed == ["x2"], (
        f"обработаны {processed} — done-запись старого формата должна пропускаться, "
        f"failed повторяться")
    assert summary["already_done_before_run"] == 1
    assert summary["mode"] == "green_only"


def _touch(p: Path, content: bytes = b"png") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


def test_migrate_legacy_layout_and_idempotency(tmp_path):
    """--migrate-legacy: _ongreen -> <base>.png (остаётся в тематической папке),
    оплаченные _diecut/_print -> _full/<категория>/, design.json -> _meta/...,
    _raw и файлы корня (журнал) не трогаются; повторный запуск — no-op."""
    outroot = tmp_path / "800"
    cat = outroot / "anime" / "one_piece"
    for base in ("a_01", "b_02"):
        _touch(cat / f"{base}_ongreen.png")
        _touch(cat / f"{base}_diecut.png")
        _touch(cat / f"{base}_print.png")
        _touch(cat / f"{base}_design.json", b"{}")
    # failed-остатки: design.json без картинок + raw для диагностики
    _touch(cat / "c_03_design.json", b"{}")
    _touch(cat / "c_03_raw.png")
    _touch(outroot / "_journal.jsonl", b"{}\n")

    stats = mega_batch_run.migrate_legacy(outroot)
    assert stats == {"renamed_ongreen": 2, "moved_full": 4, "moved_meta": 3,
                     "skipped_exists": 0}

    assert sorted(p.name for p in cat.iterdir()) == [
        "a_01.png", "b_02.png", "c_03_raw.png"]
    full = outroot / "_full" / "anime" / "one_piece"
    assert sorted(p.name for p in full.iterdir()) == [
        "a_01_diecut.png", "a_01_print.png", "b_02_diecut.png", "b_02_print.png"]
    meta = outroot / "_meta" / "anime" / "one_piece"
    assert sorted(p.name for p in meta.iterdir()) == [
        "a_01_design.json", "b_02_design.json", "c_03_design.json"]
    assert (outroot / "_journal.jsonl").exists()

    # идемпотентность: второй прогон ничего не меняет и не падает
    stats2 = mega_batch_run.migrate_legacy(outroot)
    assert stats2 == {"renamed_ongreen": 0, "moved_full": 0, "moved_meta": 0,
                      "skipped_exists": 0}
    assert sorted(p.name for p in cat.iterdir()) == [
        "a_01.png", "b_02.png", "c_03_raw.png"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
