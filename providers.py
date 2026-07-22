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

verify_anatomy_in_image(image, prompt=...) -> dict — ЕЩЁ ОДНА дешёвая точка входа
(ТА ЖЕ _OCR_MODEL, НЕ image-модель): считает руки/кисти ГЛАВНОГО персонажа и
структурировано отдаёт {"arms_visible": int|None, "anomaly": bool, "reason": str} —
используется batch_print._verify_anatomy для vision-QC-гейта анатомии рук (жалоба
владельца на 3-рукие/безрукие персонажи, 2026-07-11), гейт за config.ANATOMY_QC.
"""
import base64
import io
import json
import logging
import os
import random
import re
import time
from urllib.parse import quote

import requests
from PIL import Image

import config

log = logging.getLogger("providers")

# Тот же рабочий эндпоинт с ключом, что в market-content-bot (заданный размер + старшие
# модели, в отличие от классического image.pollinations.ai).
_GEN_BASE = "https://gen.pollinations.ai/image/"


class GeminiImageRejected(RuntimeError):
    """Gemini закончил запрос без картинки и требует изменить вход, а не seed."""

    def __init__(self, finish_reason: str, message: str = ""):
        self.finish_reason = str(finish_reason or "UNKNOWN")
        self.finish_message = str(message or "").strip()
        super().__init__(
            f"Gemini отклонил изображение ({self.finish_reason}); нужен изменённый запрос"
        )


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
    gen_cfg = {"responseModalities": ["IMAGE"]}
    # Портретный аспект для принтов (config.IMAGE_ASPECT_RATIO, напр. "2:3") —
    # без него nano-banana выбирает свою ориентацию (часто горизонталь, не
    # годится для обложки-принта). Пусто = старое поведение.
    if getattr(config, "IMAGE_ASPECT_RATIO", ""):
        gen_cfg["imageConfig"] = {"aspectRatio": config.IMAGE_ASPECT_RATIO}
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": gen_cfg,
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
                for cand in data.get("candidates", []):
                    finish_reason = str(cand.get("finishReason") or "").strip()
                    if finish_reason == "IMAGE_OTHER":
                        # Повтор того же payload после IMAGE_OTHER бессмысленен:
                        # Gemini прямо просит rephrase. Адаптацию делает batch_print.
                        raise GeminiImageRejected(
                            finish_reason,
                            str(cand.get("finishMessage") or ""),
                        )
                last_err = f"ответ без inlineData: {str(data)[:300]}"
        except GeminiImageRejected:
            raise
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
#
# РЕГРЕСС (2026-07-09, живой смоук прод-job 832a4d13, "Зоро..."): жёстко
# зашитая "gemini-2.5-flash" стала отдавать HTTP 404 "This model
# models/gemini-2.5-flash is no longer available" — Google ротирует модели
# без предупреждения в коде. КАЖДЫЙ вызов OCR-контроля (обязателен при
# TEXT_RENDER=image) падал -> 3 ретрая исчерпывались -> откат на text_fallback
# (доп. генерация БЕЗ встроенного текста) на 100% партий с текстом, тихая
# деградация качества (не падение батча — потому и не было замечено раньше).
# Взято "gemini-flash-latest" — алиас, который Google сам держит указывающим
# на актуальную рекомендованную flash-модель (тот же принцип, что убирает
# целый класс будущих 404 от ротации конкретных версионных имён). Оставлен
# env-override (GEMINI_OCR_MODEL) на случай если alias тоже когда-то подведёт
# и нужно быстро поставить конкретную версию без правки кода.
_OCR_MODEL = os.getenv("GEMINI_OCR_MODEL", "gemini-flash-latest")

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


# ── vision-QC-гейт анатомии рук (жалоба владельца на 3-рукие/безрукие персонажи, ────
# 2026-07-11) ─────────────────────────────────────────────────────────────────────

# Тот же ВОПРОС к ТОЙ ЖЕ дешёвой текстовой модели Gemini (_OCR_MODEL выше, НЕ отдельная
# платная image-модель), что OCR-контроль спеллинга, но другой prompt: считает руки/
# кисти ГЛАВНОГО персонажа и явно называет паттерны аномалий (лишняя/третья рука,
# отсутствующая/ампутированная рука, сросшиеся/неверное число пальцев). Модель отвечает
# КОМПАКТНЫМ JSON на одной строке, verify_anatomy_in_image разбирает его и отдаёт
# структурировано вызывающему коду (batch_print._verify_anatomy).
_ANATOMY_PROMPT = (
    "Look ONLY at the main human or human-like character in this image (ignore any "
    "background ornament, weapon, or decorative ring/border). Count how many arms and "
    "how many hands are visible or clearly implied by the pose on that character. "
    "Reply with STRICT compact JSON on a single line, no markdown, no code fences, no "
    "extra commentary before or after it: "
    "{\"arms_visible\": <integer, how many arms you can count>, "
    "\"anomaly\": <true or false>, "
    "\"reason\": \"<short reason in English, empty string if no anomaly>\"}. "
    "Set \"anomaly\" to true ONLY for a genuine anatomical defect: a third or extra arm, "
    "a duplicated limb, a hand or arm that is missing/amputated where one clearly should "
    "be, or a fused/malformed hand with the wrong number of fingers. A hand that is "
    "simply hidden behind the back, in a pocket, gripping a weapon, or out of frame "
    "because of the pose is NOT an anomaly — only flag a REAL drawing defect."
)


def verify_anatomy_in_image(image: Image.Image, prompt: str = _ANATOMY_PROMPT) -> dict:
    """Vision-QC анатомии рук: ОДИН доп. вызов _OCR_MODEL (та же дешёвая текстовая
    модель Gemini, что verify_text_in_image, НЕ отдельная платная image-модель) с
    вопросом посчитать руки/кисти ГЛАВНОГО персонажа и оценить аномалии. Используется
    batch_print._verify_anatomy — гейт применяется ТОЛЬКО к фигуративным персонажам
    (design["has_human_figure"]) и только при config.ANATOMY_QC=on (см. .env.example —
    каждый вызов = +1 запрос к дневной RPD-квоте Gemini, отдельной от квоты OCR-
    контроля спеллинга).

    Возвращает {"arms_visible": int|None, "anomaly": bool, "reason": str} — разобранный
    JSON-ответ модели. "arms_visible" — None, если модель не назвала распознаваемое
    целое число (не блокирует саму проверку — "anomaly" остаётся главным сигналом).

    Любой сбой (нет ключа, сеть, HTTP-ошибка, ответ без валидного JSON) -> RuntimeError
    — ОДНА попытка (ретраи и трактовка сбоя как "не подтверждено" — забота вызывающего
    кода batch_print._verify_anatomy, тот же принцип, что verify_text_in_image)."""
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY не задан в .env — нужен для vision-QC-контроля анатомии")
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
        raise RuntimeError(f"vision-QC анатомии: сеть упала: {e}") from e
    if r.status_code != 200:
        raise RuntimeError(f"vision-QC анатомии: HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    raw_text = ""
    for cand in data.get("candidates", []):
        raw_text = "".join(
            p.get("text", "") for p in cand.get("content", {}).get("parts", [])
        ).strip()
        if raw_text:
            break
    if not raw_text:
        raise RuntimeError(f"vision-QC анатомии: ответ без текста: {str(data)[:300]}")
    m = re.search(r"\{.*\}", raw_text, re.S)
    if not m:
        raise RuntimeError(f"vision-QC анатомии: ответ без JSON: {raw_text[:300]}")
    try:
        parsed = json.loads(m.group(0))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"vision-QC анатомии: битый JSON в ответе: {raw_text[:300]}") from e
    arms_visible = parsed.get("arms_visible")
    try:
        arms_visible = int(arms_visible) if arms_visible is not None else None
    except (TypeError, ValueError):
        arms_visible = None
    anomaly = bool(parsed.get("anomaly"))
    reason = str(parsed.get("reason") or "").strip()[:300]
    return {"arms_visible": arms_visible, "anomaly": anomaly, "reason": reason}
