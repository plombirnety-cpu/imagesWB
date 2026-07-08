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

# Модель Gemini (nano-banana напрямую) — дефолт gemini-3.1-flash-image (Nano Banana 2):
# A/B оркестратора (2026-07-08, out_batch/ab_models/) подтвердил заметно лучшее
# качество текста ВСТРОЕННОГО в артworк и общую гармоничность композиции против
# gemini-2.5-flash-image при той же цене — см. TEXT_RENDER ниже.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-image")

# Премиум-модель Gemini (Nano Banana Pro) — задел на будущее, providers.generate_image
# принимает параметр model, использование премиума НЕ обязательно в этом заходе (пока
# просто доступный конфиг, боевой путь по умолчанию идёт через GEMINI_MODEL выше).
GEMINI_MODEL_PREMIUM = os.getenv("GEMINI_MODEL_PREMIUM", "gemini-3-pro-image")

# image — ВСТРОЕННЫЙ текст на самой генерации (nano-banana >= NB2 рисует буквы без
# ошибок и органичнее кодовой типографики, art_director просит exact-spelling блок
# вместо запрета букв, batch_print НЕ накладывает typography_v3/typography кодом) |
# code — старый путь: генерация БЕЗ текста, типографика накладывается кодом
# (typography_v3.compose_text_v3 / typography.compose_text) — остаётся фолбэком на
# случай провала OCR-контроля спеллинга (см. batch_print._verify_text).
TEXT_RENDER = os.getenv("TEXT_RENDER", "image").strip().lower()

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
# оценка для предохранителя. gemini: оценка ПЕРЕНЕСЕНА без изменений с gemini-2.5-
# flash-image на gemini-3.1-flash-image (Nano Banana 2) — уточнить по факту биллинга
# NB2, официальный прайсинг на момент переключения (2026-07-08) под рукой не было.
COST_PER_IMAGE_USD = {"gemini": 0.04, "pollinations": 0.14}

# Для скольких САМЫХ высокоскоровых аниме-тайтлов дня (из pop:anilist/pop:jikan групп
# и аниме-трендов) theme_scout.py запускает глубокое досье franchise_scout.build_dossier
# перед вызовом тематизатора. Каждое досье с реальным YouTube-сигналом стоит ~101 юнит
# квоты YouTube (100 search.list + 1 videos.list) — при дефолте 3 это ~303 юнита/день
# из бесплатных 10000/сутки, с большим запасом.
FRANCHISE_DEEP_N = int(os.getenv("FRANCHISE_DEEP_N", "3"))
