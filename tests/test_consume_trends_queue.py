# -*- coding: utf-8 -*-
"""Тесты consume_trends_queue.py — least-invasive мост «очередь Радара → план».

Радар (trend-radar/make_prints.py) дописывает строки в trends_queue.jsonl; этот
конвертер присваивает seq, срезает _radar, дедуплицирует по filename_base и
дописывает записи в trends_plan.json (который mega_batch_run уже читает). Платный
core (mega_batch_run/batch_print) НЕ вызывается — генерации здесь нет (офлайн,
никакой сети/Gemini). Проверяем ровно конвертацию: формат записей плана, seq,
срез _radar, дедуп против плана и журнала, перенос обработанного в .done.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_consume_trends_queue.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import consume_trends_queue as ctq  # noqa: E402


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                    encoding="utf-8")


def _radar_rec(fb: str, cid: int = 9, style="29_original_trend", meme_ref="sova_na_skakalke",
               theme="Оригинальный вирусный мем Сова на скакалке, точно как в оригинале") -> dict:
    """Строка очереди в формате, который пишет trend-radar/make_prints.enqueue_prints."""
    return {
        "theme": theme, "style_pref": style, "meme_ref": meme_ref,
        "filename_base": fb, "category": "trends/sova_na_skakalke", "format": "diecut",
        "_radar": {"candidate_id": cid, "candidate_name": "Сова на скакалке",
                   "source": "radar", "enqueued_at": "2026-07-12T09:00:00",
                   "verdict": "window", "type": "trend"},
    }


def _paths(tmp_path):
    return (tmp_path / "trends_queue.jsonl",
            tmp_path / "trends_plan.json",
            tmp_path / "trends_queue.done.jsonl",
            tmp_path / "outroot")


def test_consume_appends_and_assigns_seq_and_strips_radar(tmp_path):
    q, plan, done, outroot = _paths(tmp_path)
    _write_jsonl(q, [_radar_rec("sova_na_skakalke_01"), _radar_rec("sova_na_skakalke_02", style="19_tarot")])

    summary = ctq.consume(q, plan, done, outroot)
    assert summary["appended"] == 2
    assert summary["skipped"] == 0

    recs = json.loads(plan.read_text(encoding="utf-8"))
    assert [r["seq"] for r in recs] == [1, 2]
    for r in recs:
        assert "_radar" not in r                       # служебный ключ срезан
        assert r["category"] == "trends/sova_na_skakalke"
        assert r["theme"] and r["filename_base"]
    assert recs[0]["style_pref"] == "29_original_trend"
    assert recs[0]["meme_ref"] == "sova_na_skakalke"
    assert recs[0]["format"] == "diecut"


def test_consume_continues_seq_from_existing_plan(tmp_path):
    q, plan, done, outroot = _paths(tmp_path)
    plan.write_text(json.dumps([
        {"seq": 40, "category": "anime/one_piece", "theme": "t", "filename_base": "op_40"},
        {"seq": 41, "category": "anime/one_piece", "theme": "t", "filename_base": "op_41"},
    ], ensure_ascii=False), encoding="utf-8")
    _write_jsonl(q, [_radar_rec("sova_na_skakalke_01")])

    ctq.consume(q, plan, done, outroot)
    recs = json.loads(plan.read_text(encoding="utf-8"))
    assert len(recs) == 3
    assert recs[-1]["seq"] == 42                        # продолжил нумерацию, не затёр
    assert recs[-1]["filename_base"] == "sova_na_skakalke_01"


def test_consume_dedups_against_plan(tmp_path):
    q, plan, done, outroot = _paths(tmp_path)
    plan.write_text(json.dumps([
        {"seq": 1, "category": "trends/sova_na_skakalke", "theme": "t",
         "filename_base": "sova_na_skakalke_01"},
    ], ensure_ascii=False), encoding="utf-8")
    _write_jsonl(q, [_radar_rec("sova_na_skakalke_01"),   # дубль -> пропуск
                     _radar_rec("sova_na_skakalke_02")])  # новый -> добавить

    summary = ctq.consume(q, plan, done, outroot)
    assert summary["appended"] == 1
    assert summary["skipped"] == 1
    assert "sova_na_skakalke_01" in summary["skipped_bases"]
    bases = [r["filename_base"] for r in json.loads(plan.read_text(encoding="utf-8"))]
    assert bases.count("sova_na_skakalke_01") == 1       # дубль не задвоился


def test_consume_dedups_against_journal(tmp_path):
    q, plan, done, outroot = _paths(tmp_path)
    outroot.mkdir()
    (outroot / "_journal.jsonl").write_text(
        json.dumps({"filename_base": "sova_na_skakalke_01", "status": "green_ok"}) + "\n",
        encoding="utf-8")
    _write_jsonl(q, [_radar_rec("sova_na_skakalke_01"), _radar_rec("sova_na_skakalke_02")])

    summary = ctq.consume(q, plan, done, outroot)
    assert summary["appended"] == 1                      # уже в журнале -> пропущен
    assert "sova_na_skakalke_01" in summary["skipped_bases"]


def test_consume_moves_processed_to_done_and_clears_queue(tmp_path):
    q, plan, done, outroot = _paths(tmp_path)
    _write_jsonl(q, [_radar_rec("sova_na_skakalke_01")])
    ctq.consume(q, plan, done, outroot)

    assert q.read_text(encoding="utf-8").strip() == ""   # очередь очищена
    done_rows = [json.loads(ln) for ln in done.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(done_rows) == 1
    assert done_rows[0]["filename_base"] == "sova_na_skakalke_01"
    assert "_consumed_at" in done_rows[0]                # помечено обработанным


def test_consume_skips_records_missing_required_fields(tmp_path):
    q, plan, done, outroot = _paths(tmp_path)
    bad = {"style_pref": "19_tarot", "_radar": {"source": "radar"}}  # нет theme/filename_base/category
    _write_jsonl(q, [bad, _radar_rec("sova_na_skakalke_01")])

    summary = ctq.consume(q, plan, done, outroot)
    assert summary["appended"] == 1
    assert summary["skipped"] == 1


def test_consume_drops_empty_optional_fields(tmp_path):
    q, plan, done, outroot = _paths(tmp_path)
    # ниша: meme_ref пуст, style None -> в план не попадают (mega_batch_run возьмёт дефолты)
    _write_jsonl(q, [{"theme": "Тактические тапочки: принт по теме", "style_pref": None,
                      "meme_ref": "", "filename_base": "takticheskie_tapochki_01",
                      "category": "trends/takticheskie_tapochki", "format": "diecut",
                      "_radar": {"type": "niche"}}])
    ctq.consume(q, plan, done, outroot)
    rec = json.loads(plan.read_text(encoding="utf-8"))[0]
    assert "meme_ref" not in rec
    assert "style_pref" not in rec
    assert rec["format"] == "diecut"                     # непустое опциональное — сохранили


def test_consume_empty_queue_is_noop(tmp_path):
    q, plan, done, outroot = _paths(tmp_path)
    summary = ctq.consume(q, plan, done, outroot)
    assert summary["appended"] == 0
    assert not plan.exists()                             # план не создаём на пустой очереди


def test_consume_bad_line_does_not_crash(tmp_path):
    q, plan, done, outroot = _paths(tmp_path)
    q.write_text('{"broken": \n' + json.dumps(_radar_rec("sova_na_skakalke_01")) + "\n",
                 encoding="utf-8")
    summary = ctq.consume(q, plan, done, outroot)
    assert summary["appended"] == 1                      # битую строку пропустили, валидную взяли


def test_consume_ignores_already_consumed_marker(tmp_path):
    q, plan, done, outroot = _paths(tmp_path)
    rec = _radar_rec("sova_na_skakalke_01")
    rec["_consumed"] = True
    _write_jsonl(q, [rec, _radar_rec("sova_na_skakalke_02")])
    summary = ctq.consume(q, plan, done, outroot)
    assert summary["appended"] == 1                      # помеченную _consumed пропустили
