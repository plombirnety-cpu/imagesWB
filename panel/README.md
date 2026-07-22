# Print Factory — панель генерации принтов

Веб-панель поверх движка `print-factory-nb` — генерация принтов пачками без
командной строки. Панель раскладывает count по стилям/персонажам, вызывает движок,
автоматически удаляет зелёный или синий хромакей через встроенное ядро GreenKey
и отдаёт готовые прозрачные PNG в превью, индивидуальной загрузке и ZIP.

**Разворачивается на:** http://195.133.66.37:8040 (NL-сервер, Амстердам; 8030 занят
GreenKey на том же хосте).

Полный контракт — `PLAN.md` в этой же папке.

## Экран

1. **Стили** — чекбоксы, мультивыбор (список из `docs/STYLE_BANK.json`). Без
   выбора — стиль по умолчанию (`PANEL_DEFAULT_STYLE`).
2. **Тема** — свободная строка («поднятие уровня в одиночку», «тачки»…).
3. **Персонажи** — свободная строка, опционально (через запятую/с новой строки).
4. **Количество** — сколько дизайнов сгенерировать (1..`PANEL_MAX_COUNT`).

## Логика оркестрации (`orchestrator.py`)

Вход: `{styles, count, theme, characters}`.

1. `characters` заполнено → эти персонажи, добито до `count` круговой ротацией
   персонажей и стилей.
2. `characters` пусто, `theme` похожа на тайтл (аниме/сериал) →
   `franchise_scout.build_dossier(theme)` реально находит персонажей → топ по
   `score`, тоже добито до `count`.
3. Иначе → `count` дизайнов по самой теме.

Для каждого дизайна: `art_director.make_ideas(label, n=1, fmt="cutout",
style_pref=style_id)` → `batch_print.render_design(design, tag, outdir,
green_only=True)` → `greenkey_postprocess.process_file(...)`. Промежуточный PNG
создаётся на зелёном/синем хромакее, после чего атомарно заменяется прозрачным
RGBA PNG алгоритмом актуального GreenKey (`sharp=True`, без апскейла и мыла).
Ошибка одного дизайна не роняет весь job. Сбой GreenKey не запускает повторную
платную генерацию Gemini и не повреждает исходный хромакейный файл.

## Эндпоинты

- `GET  /` — интерфейс
- `GET  /api/styles` — `[{id, name_ru}, ...]` из `STYLE_BANK.json`
- `POST /api/generate` — `{styles, count, theme, characters}` → `{job_id}`
  (запускает генерацию в фоне)
- `GET  /api/job/{job_id}` — `{status, done, total, items:[{tag, thumb_url, ok,
  error}], error}` — статусы `queued|running|done|error`
- `GET  /api/thumb/{job_id}/{tag}` — PNG одного готового дизайна
- `GET  /api/download/{job_id}` — ZIP всех готовых PNG job-а
- `GET  /health` — статус

## Локальный запуск

```bash
# из папки print-factory-nb/ (движок ставит свои зависимости первым)
pip install -r requirements.txt
pip install -r panel/requirements.txt

cd panel
uvicorn app:app --host 0.0.0.0 --port 8040
# открыть http://localhost:8040
```

Ключи движка (`GEMINI_API_KEY` и т.п.) берутся из корневого `.env`
(`print-factory-nb/.env`) — `config.py` загружает его при импорте, панель
секреты не дублирует (см. `panel/settings.py` — там только настройки самой
панели: порт, папка вывода, дефолтный стиль, лимиты).

## Тесты (без платных вызовов)

```bash
cd panel
pytest tests/ -v
```

`tests/test_orchestrator.py` мокает платные вызовы и проверяет, что сбой GreenKey
не повторяет генерацию. `tests/test_greenkey_postprocess.py` проверяет зелёный и
синий фон, прозрачность, сохранение пурпурных деталей и атомарность файла.
Реальный сквозной прогон с живым Gemini — отдельная ручная проверка.

## Деплой на сервер (Docker)

Собирается из **родительской** папки `print-factory-nb/` (нужны модули
движка — Dockerfile лежит в `panel/`, но `context: ..` в `docker-compose.yml`):

```bash
# на сервере, в папке print-factory-nb/
docker compose -f panel/docker-compose.yml up -d --build
# сервис на :8040, автоперезапуск, healthcheck
```

Или скопировать на сервер только нужное (движок-модули + `panel/` + корневой
`requirements.txt` + `docs/STYLE_BANK.json` + `.env`) в `/opt/print-factory-panel/`
и запускать `docker compose up -d --build` там же — по аналогии с
`GreenKey/web/README.md`.

## Обновление на сервере

```bash
cd /opt/print-factory-panel  # или print-factory-nb/, смотря как разложено
docker compose -f panel/docker-compose.yml up -d --build
```

## Переменные окружения

Настройки панели (`panel/settings.py`, префикс `PANEL_*`):

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `PANEL_PORT` | `8040` | порт uvicorn (локальный запуск; в Docker порт фиксирован в `EXPOSE`/`ports`) |
| `PANEL_OUTPUT_DIR` | `panel/panel_out` | куда пишутся готовые PNG по job_id |
| `PANEL_DEFAULT_STYLE` | `34_anime_magazine_cover` | стиль, если чекбоксы не отмечены |
| `PANEL_MAX_COUNT` | `50` | предохранитель — макс. дизайнов за один запуск |
| `PANEL_STYLE_BANK` | `../docs/STYLE_BANK.json` | путь к банку стилей |
| `PANEL_JOB_HISTORY_LIMIT` | `20` | сколько завершённых job-ов держим (старые чистятся) |

Переменные движка (читает `config.py`, панель их не дублирует, см. `.env.example`
в корне): `GEMINI_API_KEY`, `IMAGE_PROVIDER`, `ART_DIRECTOR_PROVIDER`, `UPSCALE`
и т.д. В контейнере панели по умолчанию (`docker-compose.yml`): `UPSCALE=off`,
`IMAGE_PROVIDER=gemini`, `ART_DIRECTOR_PROVIDER=gemini`.

## Оговорки / TODO для деплоя

- Job-стор — **in-memory** (словарь в процессе): рестарт контейнера теряет
  список активных job-ов (файлы на диске остаются, если примонтирован volume,
  но панель про них "забывает" — ZIP из уже сгенерированного можно собрать
  вручную из `panel_out/<job_id>/`). Апгрейд на персистентный стор — вне
  объёма этого захода.
- `franchise_scout.build_dossier` при пустом `characters` и заданной `theme`
  вызывается на КАЖДЫЙ job (ветка 2/3 определяется его результатом) — это
  дешёвая операция (кэш на день в `data/franchise_cache/`), но требует сети;
  при сбое сети/LLM панель НЕ падает, тихо уходит в ветку 3 (дизайны по теме).
- Апскейл до печатного размера **сознательно выключен** (`UPSCALE=off`,
  GreenKey `sharp=True`) — итог сохраняет нативное разрешение генератора и не
  размывает мелкую типографику.
- Реальный сквозной прогон (живой Gemini, платный) панель-разработчик не
  делал — только мок-тесты оркестрации и `/health`/`/api/styles` вживую;
  сквозной прогон — задача тестировщика (см. задание оркестратора).
