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
   выбора — арт-директор автоматически подбирает стиль под тему
   (`PANEL_DEFAULT_STYLE=auto`). Стиль `37_auto_racing_editorial` автономный:
   при его отдельном выборе тему можно не вводить.
2. **Тема** — свободная строка («поднятие уровня в одиночку», «тачки»…) либо
   пустая для автономного авто-стиля.
3. **Персонажи** — свободная строка, опционально (через запятую/с новой строки).
4. **Количество** — сколько дизайнов сгенерировать (1..`PANEL_MAX_COUNT`).
5. **Свободная генерация** — отдельный текстовый бриф и отдельная кнопка. В этом
   режиме тема, персонажи и чекбоксы не используются: арт-директор сам выбирает
   стиль, дорабатывает запрос, а штатный GreenKey выдаёт прозрачный PNG.
6. **Радар мемов** — TikTok-first экран для приёма публичных ссылок-сигналов,
   комментариев и метрик. Telegram/YouTube добавляются как независимые
   подтверждения. Карточка тренда показывает жизненный цикл, возраст, всплеск,
   распространение, новизну и повторяющиеся слова из комментариев. Платная
   генерация заблокирована, пока владелец явно не нажмёт «Одобрить».
7. **Мокап-мастер** — автономный массовый редактор в отдельном окне. Принты
   можно загрузить вручную либо передать в него одной кнопкой из завершённого
   задания генерации.

### Мокап-мастер

Ссылка «Мокап-мастер ↗» открывает
`/static/mockup-batcher/index.html` в отдельном окне. После завершения обычного,
ошибочного или остановленного задания с хотя бы одним готовым результатом рядом
с ZIP появляется кнопка «Передать в мокап-мастер ↗». Она открывает тот же экран
с `?job=<job_id>`; мастер получает защищённый `GET /api/job/{job_id}`, загружает
до 100 успешных `items[].file_url` и сразу добавляет их как принты. Отдельный
backend-маршрут и копирование файлов на сервере не нужны.

В мастере доступны несколько мокапов, перемещение, масштаб и поворот принта,
синхронизация раскладки между мокапами, режим сетки, PNG/JPG, фон и экспорт
активного либо всех мокапов. На HTTPS браузер может сохранить файлы прямо в
выбранную папку. На production HTTP, где системный выбор папки недоступен, весь
результат скачивается одним ZIP вместо десятков отдельных загрузок.

Защитные лимиты браузера: до 100 принтов, до 20 мокапов, до 30 МБ и 50
мегапикселей на исходное изображение; общий вход — до 300 МБ/150 мегапикселей.
Декодируются не более четырёх файлов одновременно. Один экспорт ограничен 300
файлами, 120 мегапикселями и ZIP до 300 МБ. Если партия крупнее, можно выбрать
активный мокап или уменьшить размер экспорта. Кнопка блокируется до завершения
текущей выгрузки.

История заданий хранится в памяти процесса и ограничена
`PANEL_JOB_HISTORY_LIMIT`. После рестарта или удаления старого задания
автоимпорт покажет ошибку, но ручные кнопки «+ Мокапы» и «+ Принты» продолжат
работать. Экран защищён той же сессионной авторизацией, что и остальная панель.

### Правило новизны радара

- `radar_first_seen_at` — дата, когда сигнал впервые увидела наша система; она
  **не считается** датой рождения мема.
- `earliest_published_at` — самая ранняя известная дата публикации.
- Без даты публикации и минимум двух независимых наблюдений статус всегда
  `UNVERIFIED`, даже если просмотров и повторов в комментариях много.
- Старый мем не может получить `NEW`/`RISING` только из-за комментариев. Новый
  доказанный всплеск старого мема помечается отдельно как `RESURGENCE`.
- Повторы из одного скопированного комментария и одного автора штрафуются.
- TikTok обогащается официальным `oEmbed`; MVP не пытается обходить ограничения
  платформы или скрейпить глобальную выдачу.

## Логика оркестрации (`orchestrator.py`)

Вход: `{styles, count, theme, characters, free_prompt}`.

0. `free_prompt` заполнен → `count` задач с исходным брифом, `style_pref=None`,
   без вызова `franchise_scout` и независимо от выбранных чекбоксов.
1. Тема и персонажи пусты, выбран только стиль с `theme_optional=true` →
   оркестратор строит `count` разных категорий авто-сюжетов, а конкретную машину,
   достоверные факты и смысловой текст выбирает арт-директор без
   `franchise_scout`.
2. `characters` заполнено → эти персонажи, добито до `count` круговой ротацией
   персонажей и стилей.
3. `characters` пусто, `theme` похожа на тайтл (аниме/сериал) →
   `franchise_scout.build_dossier(theme)` реально находит персонажей → топ по
   `score`, тоже добито до `count`.
4. Иначе → `count` дизайнов по самой теме.

Для каждого дизайна: `art_director.make_ideas(label, n=1, fmt="cutout",
style_pref=style_id)` → `batch_print.render_design(design, tag, outdir,
green_only=True)` → `greenkey_postprocess.process_file(...)`. Промежуточный PNG
создаётся на зелёном/синем хромакее, после чего атомарно заменяется прозрачным
RGBA PNG алгоритмом актуального GreenKey (`sharp=True`, без апскейла и мыла).
Ошибка одного дизайна не роняет весь job. Сбой GreenKey не запускает повторную
платную генерацию Gemini и не повреждает исходный хромакейный файл.

## Машинный API

Полная спецификация интеграции Telegram-ассистента, артикулов, Яндекс Диска,
виртуальной фотостудии и будущей выкладки находится в
[`docs/TELEGRAM_ASSISTANT_HANDOFF.md`](../docs/TELEGRAM_ASSISTANT_HANDOFF.md).
В ней явно отделён существующий API Print Factory от новой товарной логики бота.

Актуальная машиночитаемая схема доступна в `GET /openapi.json`, интерактивная
Swagger UI — в `GET /docs`. Оба адреса защищены так же, как `/api/*`: сервисный
клиент передаёт Bearer-токен и должен находиться в разрешённой сети, если
allowlist включён. Схема описывает только машинный API и `/health`, а не
внутренние HTML-маршруты панели.

Основные маршруты генерации и фотостудии:

- `GET  /api/capabilities` — версия API, доступные функции и действующие лимиты
- `GET  /api/styles` — `[{id, name_ru, theme_optional}, ...]` из `STYLE_BANK.json`
- `POST /api/generate` — `{styles, count, theme, characters, free_prompt}` →
  `{job_id}`; запускает обычную либо свободную генерацию в фоне
- `GET  /api/jobs?kind=all&limit=20` — последние job-ы генерации и фотостудии;
  `kind` принимает `all`, `generation` или `studio`
- `GET  /api/job/{job_id}` — прогресс и готовые позиции генерации
- `POST /api/job/{job_id}/cancel` — принудительная остановка генерации
- `GET  /api/thumb/{job_id}/{tag}` — превью готового принта
- `GET  /api/file/{job_id}/{tag}` — отдельный прозрачный PNG
- `GET  /api/download/{job_id}` — ZIP всех готовых PNG job-а
- `GET  /api/studio/models` — постоянный каталог фотомоделей; `image_url` каждой
  модели указывает на защищённый API-маршрут, а не на публичный `/static/`
- `GET  /api/studio/models/{model_id}/image` — защищённое preview модели
- `POST /api/studio/render` — multipart-запуск фотосессии: `model_id`,
  `shirt_color=black|white`, `placement=front|back`, `pose_count=1..4`,
  `quality=standard|premium`, `lighting=signature|catalog` и 1–6 файлов `prints`;
  не более 12 итоговых кадров, каждый файл не более 15 МБ
- `GET  /api/studio/jobs/{job_id}` — прогресс и готовые кадры фотосессии
- `POST /api/studio/jobs/{job_id}/cancel` — принудительная остановка фотосессии
- `GET  /api/studio/thumb/{job_id}/{tag}` — превью кадра
- `GET  /api/studio/file/{job_id}/{tag}` — отдельный PNG кадра
- `GET  /api/studio/download/{job_id}` — ZIP фотосессии

Маршруты радара:

- `POST /api/radar/signals` — принять публичную TikTok/Telegram/YouTube-ссылку,
  дату, метрики и комментарии; TikTok oEmbed запускается в отдельном фоне
- `GET  /api/radar/collector/status` — состояние автоматического поиска,
  источников, последнего и следующего запуска
- `POST /api/radar/collector/run` — поставить внеплановый автоматический проход
  в однопоточную очередь; при уже идущем проходе возвращает `409`
- `GET  /api/radar/seeds` — темы, автоматически найденные Google/Telegram и
  ожидающие/проходящие TikTok-проверку
- `GET  /api/radar/jobs/{job_id}` — статус фонового обогащения сигнала
- `GET  /api/radar/trends` — карточки радара
- `GET  /api/radar/trends/{trend_id}` — одна карточка с наблюдениями
- `POST /api/radar/trends/{trend_id}/approve|reject` — ручное решение владельца
- `POST /api/radar/trends/{trend_id}/generate` — 1–6 вариантов принта; до
  одобрения возвращает `409`
- `GET  /health` — статус

При заданном `PANEL_ACCESS_PASSWORD_SHA256` все маршруты, кроме `/health` и
страницы `/login`, закрыты парольным экраном. API без действующей сессионной cookie
отвечает `401`; пять неверных попыток с одного адреса блокируются на пять минут.
Сам пароль не хранится в конфигурации — только его SHA-256. На обычном HTTP это
лёгкая защита от случайного доступа, а не замена HTTPS.

Для доверенных машинных интеграций задаются `PRINT_FACTORY_SERVICE_TOKEN` и,
рекомендуется, `PRINT_FACTORY_SERVICE_ALLOWED_IPS`. Доступ к `/api/*`,
`/openapi.json`, `/docs`, `/redoc` и `/docs/oauth2-redirect` требует одновременно:

1. `Authorization: Bearer <token>` с токеном длиной не менее 32 символов;
2. адрес TCP-клиента из allowlist, если список не пуст.

Список принимает отдельные IP и CIDR через запятую, например
`72.56.67.18,127.0.0.1,::1`. Заголовок `X-Forwarded-For` намеренно не считается
доказательством адреса, поэтому его нельзя подделать для обхода allowlist. Пустой
allowlist сохраняет доступ по одному Bearer-токену; пустой сервисный токен целиком
отключает машинную авторизацию. Правильный токен с запрещённого адреса получает
`403`, отсутствующий или неправильный токен — `401`. Сервисный ключ не открывает
`/` и другие HTML-страницы панели.

Токен хранится только в `.env` сервера: его нельзя коммитить, писать в логи,
документацию или чат. IP-allowlist ограничивает источник запроса, но **не шифрует
трафик**. Передавать Bearer через публичный `http://` допустимо только как
временную меру; для постоянной интеграции нужен HTTPS, VPN или SSH-туннель.

### Примеры `curl`

Во всех примерах значения условные; реальный токен в команду подставляет окружение
Telegram-бота.

```bash
BASE_URL="http://195.133.66.37:8040"
TOKEN="<PRINT_FACTORY_SERVICE_TOKEN>"

# Проверить контракт и скачать OpenAPI-схему.
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${TOKEN}" \
  "${BASE_URL}/api/capabilities"
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${TOKEN}" \
  "${BASE_URL}/openapi.json" -o print-factory-openapi.json

# Создать четыре принта. Из ответа взять job_id.
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"styles":["34_anime_magazine_cover"],"count":4,"theme":"Клинок, рассекающий демонов","characters":"","free_prompt":""}' \
  "${BASE_URL}/api/generate"

# Опросить job, скачать отдельный PNG и ZIP (JOB_ID/TAG — из ответа API).
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${TOKEN}" \
  "${BASE_URL}/api/job/<JOB_ID>"
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${TOKEN}" \
  "${BASE_URL}/api/file/<JOB_ID>/<TAG>" -o print.png
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${TOKEN}" \
  "${BASE_URL}/api/download/<JOB_ID>" -o prints.zip

# Получить каталог моделей и защищённое preview Алисы.
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${TOKEN}" \
  "${BASE_URL}/api/studio/models"
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${TOKEN}" \
  "${BASE_URL}/api/studio/models/alisa/image" -o alisa.png

# Наложить готовый print.png на чёрную футболку, спереди, в двух позах.
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${TOKEN}" \
  -F "model_id=alisa" -F "shirt_color=black" -F "placement=front" \
  -F "pose_count=2" -F "quality=standard" -F "lighting=signature" \
  -F "prints=@print.png" \
  "${BASE_URL}/api/studio/render"
```

Job-ы выполняются асинхронно. Бот должен опрашивать соответствующий status-route,
пока `status` не станет `done`, `error` или `cancelled`, и не считать один долгий
HTTP-запрос зависшей генерацией. Список job-ов хранится в памяти процесса и после
рестарта контейнера очищается; готовые файлы в persistent volume остаются.

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
| `PANEL_RADAR_DB` | `<PANEL_OUTPUT_DIR>/trend_radar.sqlite3` | постоянная SQLite-база сигналов, комментариев, решений и фоновых заданий радара |
| `PANEL_RADAR_AUTO_ENABLED` | `off` | включает расписание; production держит выключенным, ручной запуск доступен отдельно |
| `PANEL_RADAR_COLLECTION_INTERVAL` | `10800` | интервал проходов в секундах, минимум 900 |
| `PANEL_RADAR_INITIAL_DELAY` | `20` | устаревшая совместимая настройка; платный проход после рестарта больше не запускается |
| `PANEL_RADAR_GOOGLE_TRENDS_GEO` | `RU` | регион официального Google Trends RSS |
| `PANEL_RADAR_TELEGRAM_CHANNELS` | `memachh,memsearch,meme_forum,BrandAnalytics` | публичные Telegram-каналы через запятую |
| `PANEL_RADAR_DISCOVERY_TERMS_PER_RUN` | `6` | максимум приоритетных тем одного ручного TikTok-прохода |
| `PANEL_RADAR_POSTS_PER_TERM` | `5` | сколько TikTok-роликов брать на тему |
| `PANEL_RADAR_COMMENTS_POSTS_PER_RUN` | `0` | зафиксировано в 0: общий проход никогда не загружает комментарии |
| `PANEL_RADAR_REQUEST_TIMEOUT` | `30` | обычный сетевой таймаут; синхронный TikTok scrape автоматически ждёт минимум 75 секунд |
| `BRIGHTDATA_API_TOKEN` | пусто | секрет API TikTok discovery/comments; хранить только в `.env` сервера |
| `PANEL_BRIGHTDATA_POSTS_DAILY_LIMIT` | `6` | максимум платных TikTok discovery-запросов за UTC-сутки; таймаут тоже считается |
| `PANEL_BRIGHTDATA_COMMENTS_DAILY_LIMIT` | `1` | максимум одной ручной выгрузки комментариев за UTC-сутки |
| `PANEL_BRIGHTDATA_RECORDS_DAILY_LIMIT` | `1000` | после достижения числа доставленных записей новые платные запросы блокируются |
| `PANEL_BRIGHTDATA_COMMENT_MAX_EXPECTED` | `500` | ручная кнопка доступна только для ролика с не более чем 500 заявленными комментариями |
| `PANEL_BRIGHTDATA_PRICE_PER_1000` | `1.5` | тариф для локальной оценки расхода в UI; точное списание смотреть в Cost Explorer |
| `PANEL_DEFAULT_STYLE` | `auto` | автовыбор арт-директором, если чекбоксы не отмечены |
| `PANEL_MAX_COUNT` | `50` | предохранитель — макс. дизайнов за один запуск |
| `PANEL_STYLE_BANK` | `../docs/STYLE_BANK.json` | путь к банку стилей |
| `PANEL_JOB_HISTORY_LIMIT` | `20` | сколько завершённых job-ов держим (старые чистятся) |
| `PANEL_ACCESS_PASSWORD_SHA256` | пусто | SHA-256 пароля в hex; пусто отключает экран входа |
| `PRINT_FACTORY_SERVICE_TOKEN` | пусто | Bearer-ключ 32+ символа для машинного доступа к `/api/*` и документации API |
| `PRINT_FACTORY_SERVICE_ALLOWED_IPS` | пусто | IP/CIDR через запятую; при непустом значении сервисному клиенту одновременно нужны правильный Bearer и разрешённый TCP-адрес |
| `PANEL_AUTH_COOKIE_SECURE` | `off` | `on` только после включения HTTPS |
| `PANEL_AUTH_COOKIE_MAX_AGE` | `2592000` | срок сессии в секундах (30 дней) |
| `PANEL_AUTH_FAILURE_LIMIT` | `5` | число неверных попыток до временной блокировки |
| `PANEL_AUTH_FAILURE_WINDOW` | `300` | окно блокировки в секундах |

Переменные движка (читает `config.py`, панель их не дублирует, см. `.env.example`
в корне): `GEMINI_API_KEY`, `IMAGE_PROVIDER`, `ART_DIRECTOR_PROVIDER`, `UPSCALE`
и т.д. В контейнере панели по умолчанию (`docker-compose.yml`): `UPSCALE=off`,
`IMAGE_PROVIDER=gemini`, `ART_DIRECTOR_PROVIDER=gemini`.

Без `BRIGHTDATA_API_TOKEN` бесплатные Google/Telegram-темы остаются доступны, но
TikTok-ролики не ищутся. При настроенном токене платный discovery запускает только
владелец кнопкой «Проверить тренды сейчас». Каждая попытка заранее резервирует слот
в SQLite; timeout не вызывает дополнительный запрос и всё равно расходует слот.
Комментарии общий проход не читает: владелец выбирает конкретную карточку, видит
ожидаемое число записей и оценку цены, подтверждает одну ручную выгрузку. Точное
списание проверяется в Bright Data Cost Explorer; дополнительно обязательно задать
account-level daily spend limit в кабинете провайдера.

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
