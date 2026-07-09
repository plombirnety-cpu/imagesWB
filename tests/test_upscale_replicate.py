# -*- coding: utf-8 -*-
"""Тесты шестнадцатого захода: бэкпорт Replicate-апскейла (upscale.py) из
content-factory-saas/engine/print_factory/upscale.py, встроен в upscale_to_print_min
как ПЕРВЫЙ путь при наличии REPLICATE_API_TOKEN (локальный realesrgan — второй,
Lanczos — финальный фолбэк). Полностью офлайн — requests.post/requests.get мокаются
через monkeypatch, никакой реальной сети (см. модульные докстринги других test_*.py —
тот же принцип). Живой единичный вызов Replicate (задача лида) прогнан ОТДЕЛЬНО,
руками, вне автоматического набора (не хотим бить реальный оплачиваемый API на каждый
прогон pytest) — см. передаточную записку разработчика.

Запуск:
    cd print-factory-nb && python -m pytest tests/test_upscale_replicate.py -v
"""
from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image, ImageDraw  # noqa: E402

import config  # noqa: E402
import upscale  # noqa: E402


def _tiny_rgba(w=40, h=60) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, w - 2, h - 2], fill=(200, 60, 60, 255))
    return img


def _png_bytes(w=160, h=240) -> bytes:
    buf = io.BytesIO()
    _tiny_rgba(w, h).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.content = content
        self.text = text or str(json_data)

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# replicate_available() / токен
# ═══════════════════════════════════════════════════════════════════════════════

def test_replicate_available_true_when_token_set(monkeypatch):
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")
    assert upscale.replicate_available() is True


def test_replicate_available_false_when_token_empty(monkeypatch):
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "")
    assert upscale.replicate_available() is False


def test_upscale_via_replicate_no_token_returns_ok_false_without_network(
        tmp_path, monkeypatch):
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "")
    calls = {"n": 0}
    monkeypatch.setattr(upscale.requests, "post",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    res = upscale.upscale_via_replicate(src, tmp_path / "out.png")
    assert res["ok"] is False
    assert "REPLICATE_API_TOKEN" in res["error"]
    assert calls["n"] == 0  # без токена сеть НЕ трогаем вовсе


# ═══════════════════════════════════════════════════════════════════════════════
# upscale_via_replicate — happy path + все сбойные ветки, requests замокан
# ═══════════════════════════════════════════════════════════════════════════════

def test_upscale_via_replicate_happy_path_mocked(tmp_path, monkeypatch):
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")

    def _fake_post(url, headers=None, json=None, timeout=None):
        assert json["input"]["scale"] == 4
        assert json["input"]["image"].startswith("data:image/png;base64,")
        return _FakeResponse(201, {"urls": {"get": "http://fake/predictions/123"}})

    def _fake_get(url, headers=None, timeout=None):
        if url == "http://fake/predictions/123":
            return _FakeResponse(200, {"status": "succeeded",
                                       "output": "http://fake/output.png"})
        if url == "http://fake/output.png":
            return _FakeResponse(200, content=_png_bytes(160, 240))
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(upscale.requests, "post", _fake_post)
    monkeypatch.setattr(upscale.requests, "get", _fake_get)

    src = tmp_path / "in.png"
    _tiny_rgba(40, 60).save(src)
    out = tmp_path / "out.png"
    res = upscale.upscale_via_replicate(src, out, scale=4)
    assert res["ok"] is True
    assert res["out_size"] == (160, 240)
    assert out.exists()
    with Image.open(out) as im:
        assert im.mode == "RGBA"


def test_upscale_via_replicate_create_network_error(tmp_path, monkeypatch):
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")

    def _fake_post(url, headers=None, json=None, timeout=None):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(upscale.requests, "post", _fake_post)
    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    res = upscale.upscale_via_replicate(src, tmp_path / "out.png")
    assert res["ok"] is False
    assert "сеть" in res["error"]


def test_upscale_via_replicate_create_non_2xx(tmp_path, monkeypatch):
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")
    monkeypatch.setattr(upscale.requests, "post",
                        lambda *a, **k: _FakeResponse(402, text="insufficient credit"))
    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    res = upscale.upscale_via_replicate(src, tmp_path / "out.png")
    assert res["ok"] is False
    assert "402" in res["error"]


def test_upscale_via_replicate_missing_urls_get(tmp_path, monkeypatch):
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")
    monkeypatch.setattr(upscale.requests, "post", lambda *a, **k: _FakeResponse(201, {}))
    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    res = upscale.upscale_via_replicate(src, tmp_path / "out.png")
    assert res["ok"] is False
    assert "urls.get" in res["error"]


def test_upscale_via_replicate_prediction_failed_status(tmp_path, monkeypatch):
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")
    monkeypatch.setattr(upscale.requests, "post",
                        lambda *a, **k: _FakeResponse(201, {"urls": {"get": "http://fake/p"}}))
    monkeypatch.setattr(upscale.requests, "get",
                        lambda *a, **k: _FakeResponse(200, {"status": "failed",
                                                            "error": "CUDA OOM"}))
    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    res = upscale.upscale_via_replicate(src, tmp_path / "out.png")
    assert res["ok"] is False
    assert "failed" in res["error"]
    assert "CUDA OOM" in res["error"]


def test_upscale_via_replicate_poll_timeout(tmp_path, monkeypatch):
    """Статус вечно 'processing' -> опрос упирается в потолок (_REPLICATE_MAX_POLL_SEC,
    урезанный до config.UPSCALE_TIMEOUT) и честно сдаётся, не виснет навсегда."""
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")
    monkeypatch.setattr(upscale, "_REPLICATE_MAX_POLL_SEC", 1)
    monkeypatch.setattr(upscale, "_REPLICATE_POLL_INTERVAL_SEC", 0.2)
    monkeypatch.setattr(upscale.requests, "post",
                        lambda *a, **k: _FakeResponse(201, {"urls": {"get": "http://fake/p"}}))
    monkeypatch.setattr(upscale.requests, "get",
                        lambda *a, **k: _FakeResponse(200, {"status": "processing"}))
    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    res = upscale.upscale_via_replicate(src, tmp_path / "out.png", timeout=5)
    assert res["ok"] is False
    assert "таймаут поллинга" in res["error"]


def test_upscale_via_replicate_download_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")
    monkeypatch.setattr(upscale.requests, "post",
                        lambda *a, **k: _FakeResponse(201, {"urls": {"get": "http://fake/p"}}))

    def _fake_get(url, headers=None, timeout=None):
        if url == "http://fake/p":
            return _FakeResponse(200, {"status": "succeeded", "output": "http://fake/out.png"})
        return _FakeResponse(500)  # скачивание результата падает

    monkeypatch.setattr(upscale.requests, "get", _fake_get)
    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    res = upscale.upscale_via_replicate(src, tmp_path / "out.png")
    assert res["ok"] is False
    assert "скачать" in res["error"]


def test_upscale_via_replicate_output_as_list_uses_first_element(tmp_path, monkeypatch):
    """Некоторые версии модели отдают output списком URL — берём первый элемент."""
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")
    monkeypatch.setattr(upscale.requests, "post",
                        lambda *a, **k: _FakeResponse(201, {"urls": {"get": "http://fake/p"}}))

    def _fake_get(url, headers=None, timeout=None):
        if url == "http://fake/p":
            return _FakeResponse(200, {"status": "succeeded",
                                       "output": ["http://fake/out.png"]})
        if url == "http://fake/out.png":
            return _FakeResponse(200, content=_png_bytes())
        raise AssertionError(url)

    monkeypatch.setattr(upscale.requests, "get", _fake_get)
    src = tmp_path / "in.png"
    _tiny_rgba().save(src)
    res = upscale.upscale_via_replicate(src, tmp_path / "out.png")
    assert res["ok"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# Семафор параллельности (задача лида: "сеть, не GPU — параллельность 4 ок") —
# ОТЛИЧАЕТСЯ от _UPSCALE_LOCK локального realesrgan (полный Lock, максимум 1
# одновременно): здесь несколько облачных вызовов реально идут ОДНОВРЕМЕННО, до
# потолка REPLICATE_MAX_CONCURRENT.
# ═══════════════════════════════════════════════════════════════════════════════

def test_replicate_semaphore_allows_real_parallelism_up_to_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")
    monkeypatch.setattr(upscale, "_REPLICATE_SEMAPHORE", threading.Semaphore(2))

    concurrent_count = {"current": 0, "max_seen": 0}
    count_lock = threading.Lock()

    def _fake_post(url, headers=None, json=None, timeout=None):
        with count_lock:
            concurrent_count["current"] += 1
            concurrent_count["max_seen"] = max(concurrent_count["max_seen"],
                                               concurrent_count["current"])
        time.sleep(0.15)
        with count_lock:
            concurrent_count["current"] -= 1
        return _FakeResponse(201, {"urls": {"get": f"http://fake/p/{threading.get_ident()}"}})

    def _fake_get(url, headers=None, timeout=None):
        if url.startswith("http://fake/p/"):
            return _FakeResponse(200, {"status": "succeeded", "output": "http://fake/out.png"})
        return _FakeResponse(200, content=_png_bytes())

    monkeypatch.setattr(upscale.requests, "post", _fake_post)
    monkeypatch.setattr(upscale.requests, "get", _fake_get)

    def _run_one(idx):
        src = tmp_path / f"in{idx}.png"
        _tiny_rgba().save(src)
        upscale.upscale_via_replicate(src, tmp_path / f"out{idx}.png")

    threads = [threading.Thread(target=_run_one, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert concurrent_count["max_seen"] >= 2, (
        "семафор должен пропускать РЕАЛЬНУЮ параллельность (не полный Lock, как у "
        "локального GPU-пути) — ожидался хотя бы 2 одновременных сетевых вызова"
    )
    assert concurrent_count["max_seen"] <= 2, (
        f"потолок REPLICATE_MAX_CONCURRENT=2 нарушен, видели {concurrent_count['max_seen']} "
        f"одновременных вызовов"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# upscale_to_print_min — порядок путей: Replicate ПЕРВЫЙ, локальный realesrgan
# ВТОРОЙ, Lanczos — финальный фолбэк (задача лида, шестнадцатый заход)
# ═══════════════════════════════════════════════════════════════════════════════

def test_upscale_to_print_min_tries_replicate_first_and_skips_local(tmp_path, monkeypatch):
    """Токен задан, Replicate отвечает успехом с достаточным размером -> локальный
    realesrgan (upscale.upscale) НЕ вызывается вовсе."""
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")

    def _fake_replicate(png_in, png_out, scale=4, timeout=None):
        Path(png_out).write_bytes(Path(png_in).read_bytes())
        return {"ok": True, "elapsed_sec": 3.0, "out_size": (4000, 4000), "error": None}

    monkeypatch.setattr(upscale, "upscale_via_replicate", _fake_replicate)

    local_calls = {"n": 0}
    monkeypatch.setattr(upscale, "upscale",
                        lambda *a, **k: local_calls.__setitem__("n", local_calls["n"] + 1))

    src = tmp_path / "in.png"
    _tiny_rgba(1000, 1000).save(src)
    out = tmp_path / "out.png"
    res = upscale.upscale_to_print_min(src, out, min_side=3800)
    assert res["ok"] is True
    assert res["out_size"] == (4000, 4000)
    assert res["print_fallback"] is False
    assert local_calls["n"] == 0, "локальный realesrgan НЕ должен звонить, Replicate уже успешен"


def test_upscale_to_print_min_no_token_skips_replicate_uses_local(tmp_path, monkeypatch):
    """Токена нет -> replicate_available()==False -> Replicate вообще не пробуем
    (upscale_via_replicate не вызывается), сразу локальный путь — старое поведение
    пятнадцатого захода не сломано."""
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "")

    replicate_calls = {"n": 0}
    monkeypatch.setattr(upscale, "upscale_via_replicate",
                        lambda *a, **k: replicate_calls.__setitem__(
                            "n", replicate_calls["n"] + 1))

    def _fake_local(png_in, png_out, scale=4, model=None, timeout=None):
        Path(png_out).write_bytes(Path(png_in).read_bytes())
        return {"ok": True, "elapsed_sec": 1.0, "out_size": (4000, 4000), "error": None}

    monkeypatch.setattr(upscale, "upscale", _fake_local)

    src = tmp_path / "in.png"
    _tiny_rgba(1000, 1000).save(src)
    out = tmp_path / "out.png"
    res = upscale.upscale_to_print_min(src, out, min_side=3800)
    assert res["ok"] is True
    assert res["print_fallback"] is False
    assert replicate_calls["n"] == 0


def test_upscale_to_print_min_falls_back_to_local_when_replicate_fails(tmp_path, monkeypatch):
    """Токен задан, но Replicate падает (сеть/API) -> локальный realesrgan пробуется
    ВТОРЫМ путём и успешно отрабатывает — итоговый результат ok=True, НЕ
    print_fallback (локальный апскейлер реально отработал)."""
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")
    monkeypatch.setattr(upscale, "upscale_via_replicate",
                        lambda *a, **k: {"ok": False, "elapsed_sec": 0.5, "out_size": None,
                                         "error": "Replicate: HTTP 500"})

    def _fake_local(png_in, png_out, scale=4, model=None, timeout=None):
        Path(png_out).write_bytes(Path(png_in).read_bytes())
        return {"ok": True, "elapsed_sec": 60.0, "out_size": (4000, 4000), "error": None}

    monkeypatch.setattr(upscale, "upscale", _fake_local)

    src = tmp_path / "in.png"
    _tiny_rgba(1000, 1000).save(src)
    out = tmp_path / "out.png"
    res = upscale.upscale_to_print_min(src, out, min_side=3800)
    assert res["ok"] is True
    assert res["print_fallback"] is False
    assert res["out_size"] == (4000, 4000)


def test_upscale_to_print_min_both_fail_falls_back_to_lanczos(tmp_path, monkeypatch):
    """И Replicate, И локальный realesrgan упали -> Lanczos-фолбэк с исходника,
    print_fallback=True, ok=True (печатный размер всё равно гарантирован), ошибка
    упоминает ОБЕ причины сбоя."""
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")
    monkeypatch.setattr(upscale, "upscale_via_replicate",
                        lambda *a, **k: {"ok": False, "elapsed_sec": 0.5, "out_size": None,
                                         "error": "Replicate: недоступен"})
    monkeypatch.setattr(upscale, "REALESRGAN_EXE", tmp_path / "does_not_exist.exe")

    src = tmp_path / "in.png"
    _tiny_rgba(400, 600).save(src)
    out = tmp_path / "out.png"
    res = upscale.upscale_to_print_min(src, out, min_side=3800)
    assert res["ok"] is True
    assert res["print_fallback"] is True
    assert max(res["out_size"]) >= 3800
    assert "Replicate" in res["error"]
    assert "realesrgan" in res["error"]


def test_upscale_to_print_min_replicate_small_result_gets_lanczos_boost(tmp_path, monkeypatch):
    """Replicate успешен, но исходник был мелкий (результат x4 < min_side) -> тот же
    адаптивный Lanczos-досчёт ПОВЕРХ результата Replicate, print_fallback остаётся
    False (Replicate реально отработал, это не полный фолбэк)."""
    monkeypatch.setattr(upscale, "REPLICATE_API_TOKEN", "r8_fake_token")

    def _fake_replicate(png_in, png_out, scale=4, timeout=None):
        # Эмулируем x4 на маленьком исходнике 700x500 -> 2800x2000, оба < 3800.
        small = Image.new("RGBA", (2800, 2000), (10, 20, 30, 255))
        small.save(png_out)
        return {"ok": True, "elapsed_sec": 4.0, "out_size": (2800, 2000), "error": None}

    monkeypatch.setattr(upscale, "upscale_via_replicate", _fake_replicate)
    local_calls = {"n": 0}
    monkeypatch.setattr(upscale, "upscale",
                        lambda *a, **k: local_calls.__setitem__("n", local_calls["n"] + 1))

    src = tmp_path / "in.png"
    _tiny_rgba(700, 500).save(src)
    out = tmp_path / "out.png"
    res = upscale.upscale_to_print_min(src, out, min_side=3800)
    assert res["ok"] is True
    assert res["print_fallback"] is False
    assert max(res["out_size"]) >= 3800
    assert local_calls["n"] == 0
    with Image.open(out) as im:
        assert im.mode == "RGBA"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
