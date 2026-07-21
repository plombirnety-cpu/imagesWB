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

# Модель Claude для арт-директора — используется, ТОЛЬКО когда
# ART_DIRECTOR_PROVIDER=anthropic (см. ниже).
MODEL = os.getenv("MODEL", "claude-sonnet-4-6")

# ── Провайдер LLM арт-директора (llm_provider.py) ────────────────────────────
# Арт-директор (идея + промпт + слоган + kana в art_director.py, синтез досье
# в franchise_scout.py) больше не завязан жёстко на Anthropic — провайдер
# переключаемый, см. llm_provider.generate_text.

# gemini (ДЕФОЛТ) — Google Gemini текстом, тот же ключ GEMINI_API_KEY, что уже
# используется для картинок (providers.py) — дешевле и не требует отдельного
# баланса Anthropic | openai — OpenAI chat.completions | anthropic — старое
# поведение 1:1 (anthropic SDK, MODEL/ANTHROPIC_API_KEY выше).
ART_DIRECTOR_PROVIDER = os.getenv("ART_DIRECTOR_PROVIDER", "gemini").strip().lower()

# Модель Gemini для ART_DIRECTOR_PROVIDER=gemini. gemini-pro-latest — живой
# алиас текущей Pro-модели, подтверждён с NL-сервера (Амстердам) HTTP 200
# (2026-07-21). ВАЖНО: gemini-2.5-pro отдаёт 404 "no longer available to new
# users" для новых ключей; версионные имена Gemini ротируются — поэтому дефолт
# именно ЖИВОЙ АЛИАС -latest (как providers.py::_OCR_MODEL = gemini-flash-latest).
# Запасная рабочая модель — gemini-2.5-flash (тоже 200, дешевле, слабее).
# ГЕО: generateContent для РФ-локации отдаёт 400 FAILED_PRECONDITION
# ("User location is not supported") — арт-директор работает ТОЛЬКО с
# поддерживаемой локации (NL-сервер), не с машины владельца. Смена модели —
# env-override ART_DIRECTOR_MODEL без правки кода.
ART_DIRECTOR_MODEL = os.getenv("ART_DIRECTOR_MODEL", "gemini-pro-latest")

# Бюджет выходных токенов арт-директора. gemini-pro-latest — «думающая» модель
# (Gemini 3 Pro): часть maxOutputTokens уходит на скрытое рассуждение, поэтому
# лаконичный лимит Claude (1500) обрезал JSON на полуслове (диагностировано с
# сервера 2026-07-21: 1500 -> обрыв на 240 симв., 8000 -> полный валидный JSON).
# _ask_claude масштабирует это на число дизайнов в вызове; llm_provider держит
# ещё и нижний «пол» для gemini (см. _generate_gemini_text).
ART_DIRECTOR_MAX_TOKENS = int(os.getenv("ART_DIRECTOR_MAX_TOKENS", "8000"))

# Ключ/модель OpenAI для ART_DIRECTOR_PROVIDER=openai.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")

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

# Соотношение сторон генерации Gemini (nano-banana) — прокидывается в
# generationConfig.imageConfig.aspectRatio (providers._generate_gemini). Пусто
# по умолчанию = НЕ задавать (старое поведение: модель выбирает сама, обычно
# горизонталь). Для принтов на футболку нужна ВЕРТИКАЛЬ — панель ставит "2:3"
# (ближе всего к эталонам обложек ~0.65). Допустимые Gemini: 1:1, 2:3, 3:4,
# 4:5, 9:16, 3:2, 4:3, 16:9 и т.п.
IMAGE_ASPECT_RATIO = os.getenv("IMAGE_ASPECT_RATIO", "").strip()

# ── Апскейл до печатных 300 DPI (upscale.py, portable realesrgan-ncnn-vulkan) ───

# on (ДЕФОЛТ) — после вырезки render_design дополнительно сохраняет <tag>_print.png =
# апскейл x4 (realesrgan-x4plus-anime) поверх diecut для печати | off — апскейл
# пропускается (например если tools/realesrgan/ не установлен на этой машине — сам
# upscale.py тоже не падает при отсутствии exe, это ДВОЙНАЯ защита). Замер на этой
# машине (встроенная AMD GPU через Vulkan, без CUDA): ~55-65с/шт на кадрах ~1300px
# стороной, в пределах порога 90с/шт — см. docs/PROJECT_STATE.md.
UPSCALE = os.getenv("UPSCALE", "on").strip().lower() == "on"

# Модель realesrgan-ncnn-vulkan для апскейла (см. tools/realesrgan/models/) —
# x4plus-anime специализирована под аниме-арт (чище держит контуры/плоскую заливку),
# чем общая x4plus.
UPSCALE_MODEL = os.getenv("UPSCALE_MODEL", "realesrgan-x4plus-anime")

# Кратность апскейла (2/3/4, см. realesrgan-ncnn-vulkan -h) — x4 даёт печатное
# разрешение на типичных диекатах nano-banana (IMG_SIZE=1536 исходник).
UPSCALE_SCALE = int(os.getenv("UPSCALE_SCALE", "4"))

# Минимальный размер БОЛЬШЕЙ стороны <tag>_print.png в пикселях (пятнадцатый заход,
# адаптивный апскейл) — nano-banana отдаёт РАЗНЫЕ исходные размеры (768..1408px
# наблюдалось на живых raw), x4 realesrgan НЕ гарантирует единый печатный минимум
# на всех дизайнах. upscale.upscale_to_print_min (upscale.py) досчитывает PIL
# Lanczos-апскейлом ПОВЕРХ результата x4, если после него большая сторона всё ещё
# < PRINT_MIN_SIDE. Дефолт 3800 — задача лида, ~12.7см при 300 DPI (типичная зона
# принта на груди футболки с запасом).
PRINT_MIN_SIDE = int(os.getenv("PRINT_MIN_SIDE", "3800"))

# Таймаут ОДНОГО вызова realesrgan-ncnn-vulkan.exe (секунд) — пятнадцатый заход,
# см. upscale.py. Встроенная GPU (Vulkan) на этой машине даёт ~55-115с/шт в
# зависимости от параллельной нагрузки (WORKERS) — 300 с запасом даже при
# нескольких воркерах, ждущих своей очереди на GPU-семафоре (см. UPSCALE_LOCK ниже).
UPSCALE_TIMEOUT = int(os.getenv("UPSCALE_TIMEOUT", "300"))

# ── QC-гейт масштаба фигуры (batch_print._figure_fills_frame) ───────────────────

# Минимальная доля высоты кадра, которую обязан занимать bbox альфы вырезанной фигуры
# — если высота силуэта меньше этой доли высоты кадра, попытка считается браком
# ("фигура слишком мелкая", в общий QC-ретрай-цикл render_design). Калибровка из
# задачи лида — урок на мелких Маки/этикетке/Люси (персонаж терялся в углу кадра).
FIGURE_MIN_FRAC = float(os.getenv("FIGURE_MIN_FRAC", "0.55"))

# ── QC-гейт vision-анатомии рук (batch_print._verify_anatomy, providers.verify_anatomy_in_image) ──

# on (ДЕФОЛТ) — жалоба владельца (2026-07-11): персонажи иногда выходят с аномалиями рук
# (третья/лишняя рука, отсутствующая рука, сросшиеся/неверное число пальцев). Для КАЖДОЙ
# фигуративной генерации (design["has_human_figure"], дефолт True — см. art_director.py)
# дешёвая текстовая модель Gemini (та же _OCR_MODEL, что OCR-контроль спеллинга в
# providers.py) считает руки/кисти ГЛАВНОГО персонажа и ловит аномалии — тот же
# ретрай-цикл, что border coverage/OCR/масштаб фигуры (см. batch_print.render_design),
# best-effort (не блокирует выпуск дизайна, если чисто не вышло за все попытки).
# КАЖДЫЙ vision-вызов = +1 запрос к дневной RPD-квоте Gemini, ОТДЕЛЬНО от квоты
# OCR-контроля спеллинга — на партиях с TEXT_RENDER=image это удваивает нагрузку на
# лимит. off — гейт полностью пропускается (генерация принимается как есть, БЕЗ
# vision-проверки анатомии) — например когда дневная квота Gemini уже на исходе.
# Промпт-усиление (art_director._ANATOMY_BLOCK, встроено в build_prompt) работает
# ВСЕГДА независимо от этого флага — это бесплатная защита на уровне промпта, флаг
# отключает только ДОПОЛНИТЕЛЬНУЮ платную vision-проверку результата.
ANATOMY_QC = os.getenv("ANATOMY_QC", "on").strip().lower() == "on"

# ── HARD-reject кадра без хромакей-фона (batch_print.render_design) ──────────────

# Жёсткий нижний порог coverage рамки хромакеем: если ЛУЧШАЯ попытка ниже него —
# дизайн проваливается (ok=False), кадр НЕ выпускается. Ловит off-style кадры, где
# фон вообще не хромакей: типичный случай — nano-banana вместо стиля перерисовывает
# эталон-портрет персонажа лицом на непрозрачном/белом фоне (жалоба владельца на gym,
# Наруто/Ичиго). Раньше такой кадр (coverage ~0) всё равно принимался как ok (брали
# «лучшую» попытку без нижней отсечки). Порог низкий (0.5): легитимные принты идут
# ~0.9-1.0 border coverage, отбраковываем только явный НЕ-хромакей. off/0 — отключить.
CHROMA_HARD_MIN_COVERAGE = float(os.getenv("CHROMA_HARD_MIN_COVERAGE", "0.5"))

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

# Потолок СОВОКУПНОЙ сметы мега-партии (mega_batch_run.py --budget-cap, USD) —
# страховка по всем прогонам вместе (журнал D:\800\_journal.jsonl хранит накопленное).
# В смету входят только попытки, где Gemini ФАКТИЧЕСКИ отдал картинку (429/500 без
# изображения реально не списываются с баланса) — см. render_design result["images"].
MEGA_BUDGET_CAP_USD = float(os.getenv("MEGA_BUDGET_CAP_USD", "150"))

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
