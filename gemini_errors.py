# -*- coding: utf-8 -*-
"""Понятная классификация HTTP-ошибок Gemini API.

Gemini использует один HTTP 429 как минимум для двух разных состояний:
временного rate limit и постоянного нулевого Prepay-баланса.  Второе нельзя
исправить повтором того же запроса, поэтому оно получает отдельный тип ошибки.
"""
from __future__ import annotations


AI_STUDIO_BILLING_URL = "https://aistudio.google.com/billing"
AI_STUDIO_PROJECTS_URL = "https://aistudio.google.com/projects"

_PREPAY_DEPLETED_MARKER = "prepayment credits are depleted"


class GeminiPrepayDepleted(RuntimeError):
    """У проекта серверного API-ключа закончились Gemini API Prepay-кредиты."""


def is_prepay_depleted(status_code: int, response_text: str) -> bool:
    """Отличает постоянную биллинговую 429 от обычного временного rate limit."""
    return (
        int(status_code) == 429
        and _PREPAY_DEPLETED_MARKER in str(response_text or "").lower()
    )


def gemini_http_error(
    context: str,
    status_code: int,
    response_text: str,
) -> RuntimeError:
    """Строит безопасную операторскую ошибку без сырого JSON в интерфейсе."""
    if is_prepay_depleted(status_code, response_text):
        return GeminiPrepayDepleted(
            f"{context}: закончились Prepay-кредиты Gemini API. "
            f"Пополните именно AI Studio: {AI_STUDIO_BILLING_URL}. "
            f"Проверьте проект серверного ключа: {AI_STUDIO_PROJECTS_URL}"
        )
    return RuntimeError(
        f"{context}: HTTP {status_code}: {str(response_text or '')[:300]}"
    )
