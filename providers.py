# -*- coding: utf-8 -*-
"""providers.py — тонкая абстракция генерации картинки поверх nano-banana (Google Gemini
Image), с двумя реализациями:

- pollinations (ДЕФОЛТ) — модель nanobanana через шлюз Pollinations gen.pollinations.ai,
  тот же паттерн вызова, что в market-content-bot/generator.py (Bearer POLLINATIONS_TOKEN).
- gemini — напрямую Google Gemini API (модель config.GEMINI_MODEL, дефолт
  gemini-3.1-flash-image = Nano Banana 2), обычный requests.POST без SDK (чтобы не
  тянуть лишнюю зависимость), картинка приходит base64 в inlineData.

Выбор через .env IMAGE_PROVIDER. Обе реализации отдают одинаковый контракт:
generate_image(prompt, seed=None, model=None, reference=None) -> PIL.Image (RGB).

reference (опционально, PIL.Image) — каноничный портрет персонажа (см.
character_ref.get_reference): рисование ПО РЕФЕРЕНСУ вместо чистого текстового
описания — лечит проблему «похож по мотивам, но не канон» (напр. Кенпачи без
фирменной повязки на глазу). Поддержано ТОЛЬКО gemini (contents.parts =
[inline_data JPEG референса, text промпта] — референс идёт ПЕРЕД текстом).
pollinations референс не поддерживает — печатает предупреждение и игнорирует.

verify_text_in_image(image, prompt=...) -> str — ОТДЕЛЬНАЯ дешёвая точка входа
(ДЕШЁВАЯ текстовая модель gemini-2.5-flash, НЕ image-модель): транскрипция всего
видимого текста на картинке, используется batch_print._verify_text для OCR-контроля
спеллинга при TEXT_RENDER=image (см. GOTCHAS/README раздел «Текст в генерации»).
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


def _reference_to_inline_part(reference: Image.Image, max_side: int = 768) -> dict:
    """Референс-картинка -> часть запроса Gemini {"inline_data": {...}}. Уменьшает
    референс до max_side по большей стороне (nano-banana не требует оригинального
    разрешения референса — достаточно узнаваемого лица/причёски/костюма, меньше
    картинка = меньше токенов на запрос) и кодирует как JPEG base64."""
    img = reference.convert("RGB")
    w, h = img.size
    scale = max_side / float(max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"inline_data": {"mime_type": "image/jpeg", "data": b64}}


def _generate_gemini(prompt: str, seed: int = None, model: str = None,
                      reference: Image.Image = None) -> Image.Image:
    """Google Gemini API напрямую (gemini-3.1-flash-image = nano-banana 2 по
    умолчанию, см. config.GEMINI_MODEL), без SDK.

    model (опционально): override конкретной модели Gemini для ЭТОГО вызова (напр.
    config.GEMINI_MODEL_PREMIUM = gemini-3-pro-image) — задел на использование
    премиум-модели точечно, дефолт (None) берёт config.GEMINI_MODEL как раньше.

    reference (опционально): PIL.Image канонiчного портрета персонажа — подмешивается
    ПЕРЕД текстом в contents.parts, чтобы модель рисовала ПО РЕФЕРЕНСУ (та же личность
    лица/причёски/костюма), а не «по мотивам» одного текстового описания."""
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY не задан в .env — нужен для IMAGE_PROVIDER=gemini "
            "(получить ключ: https://aistudio.google.com/apikey)")
    mdl = model or config.GEMINI_MODEL
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{mdl}:generateContent")
    headers = {"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_API_KEY}
    parts = []
    if reference is not None:
        parts.append(_reference_to_inline_part(reference))
    parts.append({"text": prompt})
    body = {
        "contents": [{"parts": parts}],
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


def generate_image(prompt: str, seed: int = None, model: str = None,
                    reference: Image.Image = None) -> Image.Image:
    """Единая точка входа. Провайдер выбирается через config.IMAGE_PROVIDER.

    model (опционально): для gemini — override конкретной модели (см.
    config.GEMINI_MODEL_PREMIUM, задел на премиум-путь, не обязателен); для
    pollinations — override POLLINATIONS_MODEL, как раньше.

    reference (опционально): PIL.Image канонiчного портрета персонажа (см.
    character_ref.get_reference) — рисование ПО РЕФЕРЕНСУ вместо чистого текста.
    Поддержано только провайдером gemini; pollinations референс игнорирует (с явным
    предупреждением в консоль) и генерирует как раньше — не падает."""
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    provider = config.IMAGE_PROVIDER
    if provider == "gemini":
        return _generate_gemini(prompt, seed, model=model, reference=reference)
    if provider == "pollinations":
        if reference is not None:
            print("  !! providers: референс проигнорирован (pollinations) — генерирую "
                  "по тексту, как раньше", flush=True)
        return _generate_pollinations(prompt, seed, model)
    raise RuntimeError(f"неизвестный IMAGE_PROVIDER={provider!r} (ожидается "
                       f"'pollinations' или 'gemini')")


# ── OCR-контроль спеллинга (TEXT_RENDER=image) ──────────────────────────────────

# Дешёвая ТЕКСТОВАЯ модель Gemini (не image-модель) — только транскрипция, не
# генерация картинки, стоимость на порядки меньше image-вызова.
_OCR_MODEL = "gemini-2.5-flash"

_OCR_PROMPT = (
    "Transcribe ALL text visible in this image, exactly as written. "
    "Reply with the text only."
)


def verify_text_in_image(image: Image.Image, prompt: str = _OCR_PROMPT) -> str:
    """Транскрибирует ВЕСЬ видимый текст на картинке дешёвой текстовой моделью
    Gemini (_OCR_MODEL = gemini-2.5-flash, НЕ image-модель) — используется
    batch_print._verify_text для OCR-контроля спеллинга при TEXT_RENDER=image.

    Возвращает сырой текст ответа модели (без нормализации — та делается вызывающим
    кодом). Любой сбой (нет ключа, сеть, HTTP-ошибка) -> RuntimeError, ОДНА попытка
    (ретраи — забота вызывающего кода batch_print._verify_text, там же учитывается
    факт OCR-вызова в лог)."""
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY не задан в .env — нужен для OCR-контроля спеллинга")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{_OCR_MODEL}:generateContent")
    headers = {"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_API_KEY}
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            {"text": prompt},
        ]}],
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"OCR-контроль: сеть упала: {e}") from e
    if r.status_code != 200:
        raise RuntimeError(f"OCR-контроль: HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    for cand in data.get("candidates", []):
        text = "".join(
            p.get("text", "") for p in cand.get("content", {}).get("parts", [])
        ).strip()
        if text:
            return text
    raise RuntimeError(f"OCR-контроль: ответ без текста: {str(data)[:300]}")
