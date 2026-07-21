# PLAN — Панель генерации принтов (print-factory-nb/panel)

Общий контракт для всех разработчиков. Не отклоняться без согласования с оркестратором.

## Цель
Веб-панель поверх движка `print-factory-nb` для генерации принтов пачками.
Разворачивается на NL-сервере 195.133.66.37 (Амстердам), порт **8040** (8030 занят GreenKey).
Образец по духу/масштабу — `../../GreenKey/web/` (FastAPI + одна статичная HTML-страница + Docker).

Панель — ТОНКАЯ обёртка. Вся генерация — существующий движок, ничего в логике генерации не переписываем.

## Экран (что видит владелец)
1. **Стили** — чекбоксы, мультивыбор. Список берётся из `docs/STYLE_BANK.json` (поле `styles[].id` + `name_ru`). Первый рабочий стиль — `34_anime_magazine_cover`. Новые стили в банке подхватываются автоматически.
2. **Количество** генераций (число, 1..N).
3. **Тема** — свободная строка («поднятие уровня в одиночку», «тачки» и т.п.).
4. **Персонажи** — свободная строка, опционально (для аниме/сериалов).

## Логика оркестрации (backend)
Вход: `{styles: [style_id,...], count: N, theme: str, characters: str|""}`.

1. Если `characters` заполнено → генерим ровно этих персонажей (по одному дизайну на персонажа, добивая до N ротацией/стилями).
2. Если `characters` пусто И тема — это тайтл (аниме/сериал) → вызвать
   `franchise_scout.build_dossier(theme, kind="auto")` → взять топ-N персонажей по `score`.
3. Если тема не про персонажей (досье пустое/не тайтл) → N дизайнов по самой теме.

Для каждого дизайна:
- `designs = art_director.make_ideas(theme_or_character, n=1, fmt="cutout", style_pref=<выбранный style_id>)`
  (при мультивыборе стилей — раскидываем count по выбранным стилям по кругу).
- `batch_print.render_design(design, tag, outdir, green_only=True)` — ОДИН PNG на зелёном фоне,
  БЕЗ вырезки и апскейла (апскейл владелец делает отдельной программой).
- Собрать все PNG в ZIP.

## Выдача
- Фоновый job (в процессе, in-memory) + прогресс, чтобы 10-50 картинок не упирались в HTTP-таймаут.
- Эндпоинты:
  - `POST /api/generate` → `{job_id}`
  - `GET  /api/job/{job_id}` → `{status, done, total, items:[{tag, thumb_url, ok}], error?}`
  - `GET  /api/download/{job_id}` → ZIP всех готовых PNG
  - `GET  /api/styles` → список стилей из STYLE_BANK для чекбоксов
  - `GET  /health`
- Превью по мере готовности + кнопка «Скачать ZIP».

## Провайдер арт-директора (убираем зависимость от Anthropic)
- Новый модуль `print-factory-nb/llm_provider.py` с единой функцией генерации текста.
- Выбор провайдера: `config.ART_DIRECTOR_PROVIDER` = `gemini` (дефолт) | `openai` | `anthropic`.
- Gemini-текст (дефолт): модель `config.ART_DIRECTOR_MODEL` (дефолт `gemini-2.5-pro`), REST
  `generativelanguage.googleapis.com/v1beta/models/<model>:generateContent`, ключ `GEMINI_API_KEY`
  (тот же, что для картинок) — без SDK, обычный `requests`, как `providers.py::_generate_gemini`.
- `art_director._ask_claude` и синтез в `franchise_scout` зовут `llm_provider`, а не `anthropic` напрямую.
- Поведение (вход/выход, возвращает text) остаётся идентичным — интерфейс `make_ideas`/`build_dossier` НЕ меняется, панель от смены провайдера не зависит.

## Разделение зон (не пересекаться файлами)
- **Разработчик A (провайдер):** `llm_provider.py` (новый), `art_director.py`, `franchise_scout.py`,
  `config.py`, `.env.example`. Плюс живой smoke-тест текстового Gemini.
- **Разработчик B (панель):** только `panel/**` (всё новое). Свои настройки — в `panel/settings.py`
  (порт, папка вывода, дефолтный стиль), НЕ трогать корневой `config.py`.
- Config трогает ТОЛЬКО разработчик A.

## Ключевые точки входа движка (публичный API, НЕ менять сигнатуры)
- `art_director.make_ideas(theme, n, fmt="cutout", recent_styles=None, style_pref=None) -> list[dict]`
- `franchise_scout.build_dossier(title, kind="auto") -> dict` (персонажи по убыванию score)
- `batch_print.render_design(design, tag, outdir, ..., green_only=False) -> dict`
  (для панели `green_only=True` — один PNG на зелёном, без cutout/upscale)
- `providers.generate_image(...)` — уже используется render_design, трогать не нужно.

## Деплой (devops / финальный шаг)
- `panel/Dockerfile` + `panel/docker-compose.yml` (порт 8040, autorestart, healthcheck).
- Контекст сборки — папка `print-factory-nb/` (нужны модули движка). Копировать в образ ТОЛЬКО:
  `*.py` движка (art_director, batch_print, providers, franchise_scout, character_ref, chroma_remove,
  config, llm_provider, palette, typography*, upscale, meme_ref, theme_scout — по факту импортов),
  `docs/STYLE_BANK.json` (+ прочие docs/*.json, если импортируются), `panel/`, `requirements.txt`.
  НЕ копировать `tools/realesrgan/`, `out_batch/`, `data/`, `D:\800`.
- `UPSCALE=off`, `IMAGE_PROVIDER=gemini`, `ART_DIRECTOR_PROVIDER=gemini` в окружении контейнера.
- Инструкция обновления на сервере — как в `GreenKey/web/README.md` (docker compose up -d --build).

## Технические примечания
- Каждый `render_design` = платные вызовы Gemini (генерация + QC). Держать разумный дефолт count
  и не плодить лишние ретраи.
- Санитайзить `tag`/имена файлов (тема на кириллице → безопасный slug).
- `franchise_scout` без `YOUTUBE_API_KEY`/`TMDB_API_KEY` работает по AniList+MAL (graceful degradation).
