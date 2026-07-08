# -*- coding: utf-8 -*-
"""config.py — конфиг из .env. Все секреты только отсюда, никогда хардкодом."""
import os

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
POLLINATIONS_TOKEN = os.getenv("POLLINATIONS_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Ключи franchise_scout.py (досье франшизы) — опциональны, каждый источник без
# своего ключа просто пропускается (graceful degradation), см. franchise_scout.py.
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")

# Модель Claude для арт-директора (идея + промпт + слоган + kana).
MODEL = os.getenv("MODEL", "claude-sonnet-4-6")

# Бренд-лейбл для подвала-этикетки типографики v3 (typography_v3.compose_text_v3,
# режим collection_footer, docs/PRINT_STYLE_GUIDE.md раздел 3.4) — не хардкод внутри
# модуля типографики, конфиг-константа.
# ПУСТОЙ по умолчанию: бренд на принты НЕ наносим (правка владельца 2026-07-08),
# подвал рисует только TITLE | CHARACTER. Задать значение — вернуть блок бренда.
BRAND_LABEL = os.getenv("BRAND_LABEL", "")

# gemini (дефолт — Pollinations-кошелёк студии периодически пуст, Gemini дешевле в
# 4-7 раз на объёме 500 принтов/день) | pollinations (шлюз gen.pollinations.ai).
IMAGE_PROVIDER = os.getenv("IMAGE_PROVIDER", "gemini").strip().lower()

# Модель Pollinations под nano-banana.
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "nanobanana")

# Модель Gemini (nano-banana напрямую).
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-image")

IMG_SIZE = int(os.getenv("IMG_SIZE", "1536"))

# ── Ежедневный контур (theme_scout.py / daily_prints.py) ────────────────────────

# Путь к соседнему проекту trend-watch/ (источник CSV-трендов + subprocess-парсеры).
TREND_WATCH_DIR = os.getenv("TREND_WATCH_DIR", "../trend-watch")

# Сколько принтов делать за один дневной прогон.
PRINTS_PER_DAY = int(os.getenv("PRINTS_PER_DAY", "500"))

# Параллельные генерации (ThreadPoolExecutor) в daily_prints.py.
WORKERS = int(os.getenv("WORKERS", "4"))

# Предохранитель дневного бюджета в USD — план обрезается перед стартом, если смета
# по цене провайдера превышает потолок.
MAX_DAILY_COST_USD = float(os.getenv("MAX_DAILY_COST_USD", "50"))

# Ориентировочная цена ОДНОЙ успешной генерации по провайдерам (USD/шт, включая типовой
# QC-ретрай) — для сметы дня в daily_prints.py. Не биллинговая точность, а плановая
# оценка для предохранителя.
COST_PER_IMAGE_USD = {"gemini": 0.04, "pollinations": 0.14}

# Для скольких САМЫХ высокоскоровых аниме-тайтлов дня (из pop:anilist/pop:jikan групп
# и аниме-трендов) theme_scout.py запускает глубокое досье franchise_scout.build_dossier
# перед вызовом тематизатора. Каждое досье с реальным YouTube-сигналом стоит ~101 юнит
# квоты YouTube (100 search.list + 1 videos.list) — при дефолте 3 это ~303 юнита/день
# из бесплатных 10000/сутки, с большим запасом.
FRANCHISE_DEEP_N = int(os.getenv("FRANCHISE_DEEP_N", "3"))
