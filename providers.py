# -*- coding: utf-8 -*-
"""providers.py — тонкая абстракция генерации картинки поверх nano-banana (Google Gemini
Image), с двумя реализациями:

- pollinations (ДЕФОЛТ) — модель nanobanana через шлюз Pollinations gen.pollinations.ai,
  тот же паттерн вызова, что в market-content-bot/generator.py (Bearer POLLINATIONS_TOKEN).
- gemini — напрямую Google Gemini API (модель gemini-2.5-flash-image, она же nano-banana),
  обычный requests.POST без SDK (чтобы не тянуть лишнюю зависимость), картинка приходит
  base64 в inlineData.

Выбор через .env IMAGE_PROVIDER. Обе реализации отдают одинаковый контракт:
generate_image(prompt, seed=None) -> PIL.Image (RGB).
"""
import base64
import io
import logging
import random
import time
from urllib.parse import quote

import requests
from PIL import Image

import config

log = logging.getLogger("providers")

# Тот же рабочий эндпоинт с ключом, что в market-content-bot (заданный размер + старшие
# модели, в отличие от классического image.pollinations.ai).
_GEN_BASE = "https://gen.pollinations.ai/image/"


def _pollinations_headers() -> dict:
    h = {"User-Agent": "print-factory-nb/1.0"}
    if config.POLLINATIONS_TOKEN:
        h["Authorization"] = f"Bearer {config.POLLINATIONS_TOKEN}"
    return h


def _generate_pollinations(prompt: str, seed: int, model: str = None,
                            width: int = None, height: int = None) -> Image.Image:
    """nanobanana через Pollinations. Ретраит один раз на сбой сети/пустой ответ."""
    mdl = model or config.POLLINATIONS_MODEL
    size = width or config.IMG_SIZE
    hsize = height or config.IMG_SIZE
    params = (f"?width={size}&height={hsize}&nologo=true&private=true"
              f"&seed={seed}&model={mdl}")
    url = _GEN_BASE + quote(prompt) + params
    last_err = None
    for attempt in range(2):
        try:
            r = requests.get(url, headers=_pollinations_headers(), timeout=300)
            if r.status_code == 200 and len(r.content) > 20_000:
                img = Image.open(io.BytesIO(r.content))
                img.load()
                return img.convert("RGB")
            last_err = f"HTTP {r.status_code} ({len(r.content)}b)"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if attempt == 0:
            time.sleep(6 + random.uniform(0, 3))
    raise RuntimeError(f"Pollinations (nanobanana) не отдал картинку: {last_err}")


def _generate_gemini(prompt: str, seed: int = None) -> Image.Image:
    """Google Gemini API напрямую (gemini-2.5-flash-image = nano-banana), без SDK."""
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY не задан в .env — нужен для IMAGE_PROVIDER=gemini "
            "(получить ключ: https://aistudio.google.com/apikey)")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{config.GEMINI_MODEL}:generateContent")
    headers = {"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_API_KEY}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    last_err = None
    for attempt in range(2):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=180)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
            else:
                data = r.json()
                for cand in data.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        inline = part.get("inlineData") or part.get("inline_data")
                        if inline and inline.get("data"):
                            raw = base64.b64decode(inline["data"])
                            img = Image.open(io.BytesIO(raw))
                            img.load()
                            return img.convert("RGB")
                last_err = f"ответ без inlineData: {str(data)[:300]}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if attempt == 0:
            time.sleep(5)
    raise RuntimeError(f"Gemini (nano-banana) не отдал картинку: {last_err}")


def generate_image(prompt: str, seed: int = None, model: str = None) -> Image.Image:
    """Единая точка входа. Провайдер выбирается через config.IMAGE_PROVIDER."""
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    provider = config.IMAGE_PROVIDER
    if provider == "gemini":
        return _generate_gemini(prompt, seed)
    if provider == "pollinations":
        return _generate_pollinations(prompt, seed, model)
    raise RuntimeError(f"неизвестный IMAGE_PROVIDER={provider!r} (ожидается "
                       f"'pollinations' или 'gemini')")
