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

# SQLite радара хранится в том же постоянном volume, что и готовые PNG. Отдельный
# путь нужен тестам и возможному будущему выносу БД на самостоятельный диск.
RADAR_DB_PATH = Path(
    os.getenv("PANEL_RADAR_DB", str(OUTPUT_DIR / "trend_radar.sqlite3"))
)


def _env_bool(name: str, default: str = "off") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        item.strip().lstrip("@")
        for item in os.getenv(name, default).split(",")
        if item.strip()
    )


# Автоматический радар. Бесплатные Google Trends и публичные Telegram-каналы
# дают темы-кандидаты; Bright Data выполняет discovery TikTok и чтение комментариев.
# Токен хранится только в env сервера и никогда не записывается в SQLite.
RADAR_AUTO_ENABLED = _env_bool("PANEL_RADAR_AUTO_ENABLED", "off")
RADAR_COLLECTION_INTERVAL = max(
    900, int(os.getenv("PANEL_RADAR_COLLECTION_INTERVAL", str(3 * 60 * 60)))
)
RADAR_INITIAL_DELAY = max(1, int(os.getenv("PANEL_RADAR_INITIAL_DELAY", "20")))
RADAR_GOOGLE_TRENDS_GEO = os.getenv("PANEL_RADAR_GOOGLE_TRENDS_GEO", "RU").strip() or "RU"
RADAR_TELEGRAM_CHANNELS = _env_csv(
    "PANEL_RADAR_TELEGRAM_CHANNELS",
    "memachh,memsearch,meme_forum,BrandAnalytics",
)
RADAR_DISCOVERY_TERMS_PER_RUN = max(
    1, min(20, int(os.getenv("PANEL_RADAR_DISCOVERY_TERMS_PER_RUN", "6")))
)
RADAR_POSTS_PER_TERM = max(
    1, min(50, int(os.getenv("PANEL_RADAR_POSTS_PER_TERM", "5")))
)
RADAR_COMMENTS_POSTS_PER_RUN = max(
    0, min(10, int(os.getenv("PANEL_RADAR_COMMENTS_POSTS_PER_RUN", "0")))
)
RADAR_REQUEST_TIMEOUT = max(
    5, min(120, int(os.getenv("PANEL_RADAR_REQUEST_TIMEOUT", "30")))
)
BRIGHTDATA_API_TOKEN = os.getenv("BRIGHTDATA_API_TOKEN", "").strip()
# Жёсткий предохранитель платного провайдера. Любая попытка резервирует лимит ДО
# HTTP-запроса: таймаут тоже расходует слот, потому что snapshot уже мог быть создан.
BRIGHTDATA_POSTS_DAILY_LIMIT = max(
    0, int(os.getenv("PANEL_BRIGHTDATA_POSTS_DAILY_LIMIT", "6"))
)
BRIGHTDATA_COMMENTS_DAILY_LIMIT = max(
    0, int(os.getenv("PANEL_BRIGHTDATA_COMMENTS_DAILY_LIMIT", "1"))
)
BRIGHTDATA_RECORDS_DAILY_LIMIT = max(
    0, int(os.getenv("PANEL_BRIGHTDATA_RECORDS_DAILY_LIMIT", "1000"))
)
BRIGHTDATA_COMMENT_MAX_EXPECTED = max(
    0, int(os.getenv("PANEL_BRIGHTDATA_COMMENT_MAX_EXPECTED", "500"))
)
BRIGHTDATA_PRICE_PER_1000 = max(
    0.0, float(os.getenv("PANEL_BRIGHTDATA_PRICE_PER_1000", "1.5"))
)

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

# Отдельный машинный ключ для Telegram-бота и других доверенных сервисов.
# Он открывает только /api/* и не заменяет парольную сессию веб-интерфейса.
SERVICE_TOKEN = os.getenv("PRINT_FACTORY_SERVICE_TOKEN", "").strip()
if SERVICE_TOKEN and len(SERVICE_TOKEN) < 32:
    raise RuntimeError("PRINT_FACTORY_SERVICE_TOKEN должен содержать не меньше 32 символов")

AUTH_COOKIE_SECURE = os.getenv("PANEL_AUTH_COOKIE_SECURE", "off").strip().lower() in {
    "1", "true", "yes", "on",
}
AUTH_COOKIE_MAX_AGE = int(os.getenv("PANEL_AUTH_COOKIE_MAX_AGE", str(30 * 24 * 60 * 60)))
AUTH_FAILURE_LIMIT = int(os.getenv("PANEL_AUTH_FAILURE_LIMIT", "5"))
AUTH_FAILURE_WINDOW = int(os.getenv("PANEL_AUTH_FAILURE_WINDOW", "300"))
