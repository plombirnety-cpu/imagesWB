# -*- coding: utf-8 -*-
"""settings.py — настройки веб-панели генерации принтов, ТОЛЬКО из env.

Отдельно от корневого config.py движка (см. panel/PLAN.md — Разработчик B не
трогает config.py). Панель не хранит секреты сама — ключи (Gemini и т.п.)
по-прежнему читает config.py движка при импорте art_director/providers/
franchise_scout, панель их не дублирует и не переопределяет.
"""
import os
from pathlib import Path

PANEL_DIR = Path(__file__).resolve().parent
ENGINE_ROOT = PANEL_DIR.parent

# Порт панели (8030 занят GreenKey на том же NL-сервере, см. PLAN.md).
PORT = int(os.getenv("PANEL_PORT", "8040"))

# Папка вывода панели — готовые PNG по job_id, отдельная песочница панели,
# НЕ движковые out_batch/ и D:\800.
OUTPUT_DIR = Path(os.getenv("PANEL_OUTPUT_DIR", str(PANEL_DIR / "panel_out")))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Стиль по умолчанию, если владелец не отметил ни одного чекбокса.
DEFAULT_STYLE = os.getenv("PANEL_DEFAULT_STYLE", "34_anime_magazine_cover")

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
