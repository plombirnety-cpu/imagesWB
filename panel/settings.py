# -*- coding: utf-8 -*-
"""settings.py — настройки веб-панели генерации принтов, ТОЛЬКО из env.

Отдельно от корневого config.py движка (см. panel/PLAN.md — Разработчик B не
трогает config.py). Панель не хранит секреты сама — ключи (Gemini и т.п.)
по-прежнему читает config.py движка при импорте art_director/providers/
franchise_scout, панель их не дублирует и не переопределяет.
"""
import os
import re
from pathlib import Path

PANEL_DIR = Path(__file__).resolve().parent
ENGINE_ROOT = PANEL_DIR.parent

# Порт панели (8030 занят GreenKey на том же NL-сервере, см. PLAN.md).
PORT = int(os.getenv("PANEL_PORT", "8040"))

# Папка вывода панели — готовые PNG по job_id, отдельная песочница панели,
# НЕ движковые out_batch/ и D:\800.
OUTPUT_DIR = Path(os.getenv("PANEL_OUTPUT_DIR", str(PANEL_DIR / "panel_out")))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Если владелец не отметил ни одного чекбокса, стиль выбирает арт-директор по теме.
# Раньше здесь был принудительный anime style 34: поэтому Doctor Doom без выбора
# неожиданно уходил в аниме-журнал и чаще ловил IMAGE_OTHER/PROHIBITED_CONTENT.
DEFAULT_STYLE = os.getenv("PANEL_DEFAULT_STYLE", "auto")

# Предохранитель: сколько генераций разрешаем за один запрос панели (каждая —
# платный вызов Gemini, см. PLAN.md "Технические примечания").
MAX_COUNT = int(os.getenv("PANEL_MAX_COUNT", "50"))

# Путь к банку стилей движка (docs/STYLE_BANK.json) — источник чекбоксов.
STYLE_BANK_PATH = Path(
    os.getenv("PANEL_STYLE_BANK", str(ENGINE_ROOT / "docs" / "STYLE_BANK.json"))
)

# Сколько завершённых job-ов (done/error) держим в памяти/на диске — старые
# чистятся при превышении (защита от утечки на долгоживущем процессе).
JOB_HISTORY_LIMIT = int(os.getenv("PANEL_JOB_HISTORY_LIMIT", "20"))

# Лёгкая защита публичной панели паролем. В env хранится только SHA-256 пароля,
# сам пароль не попадает ни в исходники, ни в compose. Пустое значение отключает
# gate для локальной разработки и обратной совместимости.
ACCESS_PASSWORD_SHA256 = os.getenv("PANEL_ACCESS_PASSWORD_SHA256", "").strip().lower()
if ACCESS_PASSWORD_SHA256 and not re.fullmatch(r"[0-9a-f]{64}", ACCESS_PASSWORD_SHA256):
    raise RuntimeError("PANEL_ACCESS_PASSWORD_SHA256 должен быть SHA-256 в hex (64 символа)")

AUTH_COOKIE_SECURE = os.getenv("PANEL_AUTH_COOKIE_SECURE", "off").strip().lower() in {
    "1", "true", "yes", "on",
}
AUTH_COOKIE_MAX_AGE = int(os.getenv("PANEL_AUTH_COOKIE_MAX_AGE", str(30 * 24 * 60 * 60)))
AUTH_FAILURE_LIMIT = int(os.getenv("PANEL_AUTH_FAILURE_LIMIT", "5"))
AUTH_FAILURE_WINDOW = int(os.getenv("PANEL_AUTH_FAILURE_WINDOW", "300"))
