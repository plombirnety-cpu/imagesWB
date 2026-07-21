# -*- coding: utf-8 -*-
"""llm_provider.py — единая точка входа генерации ТЕКСТА для арт-директора
(art_director.py) и синтеза досье франшизы (franchise_scout.py).

Раньше оба модуля были жёстко привязаны к Anthropic Claude (`import anthropic`
напрямую). Этот модуль абстрагирует выбор LLM-провайдера через
config.ART_DIRECTOR_PROVIDER, ДЕФОЛТ — gemini (тот же ключ GEMINI_API_KEY, что
уже используется для картинок в providers.py, и Gemini дешевле/не требует
отдельного баланса Anthropic):

- gemini    — Google Gemini текстом (config.ART_DIRECTOR_MODEL), REST без SDK,
              тот же паттерн запроса, что providers.py::_generate_gemini
              (URL generativelanguage.googleapis.com, заголовок x-goog-api-key).
- openai    — OpenAI chat.completions (config.OPENAI_MODEL), REST без SDK.
- anthropic — старое поведение 1:1 (anthropic SDK, config.MODEL, ANTHROPIC_API_KEY).

Единственная публичная функция:
    generate_text(system, user, max_tokens=1500) -> str

Возвращает СЫРОЙ текст ответа модели (без парсинга/нормализации — этим
занимается вызывающий код art_director._parse / franchise_scout._parse_dossier,
как и раньше). Одна попытка на вызов — ретраи остаются заботой вызывающего кода
(art_director.make_ideas делает 1 ретрай, franchise_scout._ask_and_parse_dossier_
with_retry — тоже), это НЕ меняется переключением провайдера.

Любой сбой (нет нужного ключа, сеть, HTTP-ошибка, неизвестный провайдер,
пустой ответ) -> RuntimeError с понятным сообщением — единый тип исключения
НЕЗАВИСИМО от провайдера, чтобы вызывающий код мог ловить один класс ошибок,
а не разбираться в SDK-специфичных исключениях (anthropic.APIError и т.п.)."""
import time

import config

import requests

# Модель для дешёвого текстового Gemini-запроса (тот же способ вызова, что
# providers.py::_generate_gemini, но endpoint/модель — текстовые, не image).
_GEMINI_TEXT_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def _generate_gemini_text(system: str, user: str, max_tokens: int) -> str:
    """Gemini REST (без SDK) — системная инструкция кладётся в system_instruction
    (официальный способ Gemini API отделить system prompt от user turn), user —
    единственный content-turn с role=user."""
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY не задан в .env — нужен для ART_DIRECTOR_PROVIDER=gemini "
            "(получить ключ: https://aistudio.google.com/apikey)")
    model = config.ART_DIRECTOR_MODEL
    url = _GEMINI_TEXT_URL_TMPL.format(model=model)
    headers = {"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_API_KEY}
    # «Пол» бюджета: gemini «думающие» модели (pro-latest = Gemini 3 Pro) тратят
    # часть maxOutputTokens на скрытое рассуждение — при малом лимите JSON
    # обрывается на полуслове. Держим не меньше 8000, даже если вызывающий код
    # передал лаконичное значение (диагностировано с сервера 2026-07-21).
    out_tokens = max(int(max_tokens), 8000)
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": out_tokens},
    }
    # Ретрай на ВРЕМЕННЫЕ сбои: 503 (модель перегружена — популярная
    # gemini-pro-latest часто отдаёт "high demand"), 429 (rate-limit),
    # 500 (внутренняя). backoff 4/10/20с, до 4 попыток. Постоянные ошибки
    # (404 нет модели, 400 гео/невалидный ключ) НЕ ретраятся — сразу RuntimeError.
    _TRANSIENT = {429, 500, 503}
    _BACKOFF = (4, 10, 20)
    r = None
    for attempt in range(len(_BACKOFF) + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
        except Exception as e:  # noqa: BLE001 — сетевой сбой тоже временный
            if attempt < len(_BACKOFF):
                time.sleep(_BACKOFF[attempt]); continue
            raise RuntimeError(f"Gemini (арт-директор, текст): сеть упала: {e}") from e
        if r.status_code == 200:
            break
        if r.status_code in _TRANSIENT and attempt < len(_BACKOFF):
            time.sleep(_BACKOFF[attempt]); continue
        raise RuntimeError(
            f"Gemini (арт-директор, текст): HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    for cand in data.get("candidates", []):
        text = "".join(
            p.get("text", "") for p in cand.get("content", {}).get("parts", [])
        ).strip()
        if text:
            return text
    raise RuntimeError(f"Gemini (арт-директор, текст): ответ без текста: {str(data)[:300]}")


def _generate_openai_text(system: str, user: str, max_tokens: int) -> str:
    """OpenAI chat.completions REST (без SDK) — system/user как обычные messages."""
    if not config.OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY не задан в .env — нужен для ART_DIRECTOR_PROVIDER=openai")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
    }
    body = {
        "model": config.OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=120)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"OpenAI (арт-директор): сеть упала: {e}") from e
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI (арт-директор): HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(
            f"OpenAI (арт-директор): ответ без текста: {str(data)[:300]}") from e
    text = (text or "").strip()
    if not text:
        raise RuntimeError(f"OpenAI (арт-директор): пустой ответ: {str(data)[:300]}")
    return text


def _generate_anthropic_text(system: str, user: str, max_tokens: int) -> str:
    """Старое поведение 1:1 — anthropic SDK, config.MODEL/ANTHROPIC_API_KEY.
    Импорт anthropic — ЛОКАЛЬНЫЙ (внутри функции), чтобы провайдеры gemini/openai
    работали даже если пакет anthropic не установлен в окружении."""
    import anthropic

    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY не задан в .env — нужен для ART_DIRECTOR_PROVIDER=anthropic")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=config.MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:  # noqa: BLE001 — сеть/баланс/rate-limit/5xx Claude
        raise RuntimeError(f"Claude (арт-директор): вызов не удался: {e}") from e
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


_PROVIDERS = {
    "gemini": _generate_gemini_text,
    "openai": _generate_openai_text,
    "anthropic": _generate_anthropic_text,
}


def generate_text(system: str, user: str, max_tokens: int = 1500) -> str:
    """Единая точка входа. Провайдер выбирается через config.ART_DIRECTOR_PROVIDER
    ('gemini' — ДЕФОЛТ, 'openai', 'anthropic'). Возвращает сырой текст ответа
    модели. Неизвестный провайдер/отсутствующий ключ/сетевой сбой -> RuntimeError
    с понятным сообщением (единый тип исключения для всех провайдеров)."""
    provider = config.ART_DIRECTOR_PROVIDER
    fn = _PROVIDERS.get(provider)
    if not fn:
        raise RuntimeError(
            f"неизвестный ART_DIRECTOR_PROVIDER={provider!r} (ожидается "
            f"'gemini', 'openai' или 'anthropic')")
    return fn(system, user, max_tokens)
