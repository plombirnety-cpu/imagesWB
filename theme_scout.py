#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""theme_scout.py — сбор трендов из соседнего trend-watch/ и разворачивание их в
дневной план заданий на принты (для daily_prints.py).

Шаги:
1. Запускает media_watch.py, anime_watch.py и pop_watch.py в trend-watch/ (subprocess,
   таймаут, try/except — упавший парсер не валит цикл, работаем с тем, что уже лежит в
   CSV; отсутствие/падение pop_watch.py — та же деградация, план соберётся без него).
2. Читает trend-watch/data/media_latest.csv, anime_latest.csv и pop_latest.csv, берёт
   топ-N по score. pop_latest.csv — pop-тренды (anilist/jikan/youtube/gtrends):
   строки anilist/jikan уходят в аниме-пул (ростер персонажей), youtube/gtrends —
   в общий пул (наравне с медиа-трендами).
3. Claude-«тематизатор»: леммы + example_text -> задания на принты до дневного объёма.
   Аниме-тренд (тайтл/персонаж) -> ростер главных персонажей x форматы x 1-2 вариации.
   Общая тема -> несколько принтопригодных концептов. Pop-темы передаются отдельным
   блоком с пометкой «это ГОТОВЫЕ названия сущностей (тайтл/видео/запрос), их нужно
   превращать в задания, а не искать в них леммы». Политика/война/катастрофы/трагедии/
   смерти реальных людей/криминал — ОТСЕКАЮТСЯ МОЛЧА системным промптом, фильтр
   действует одинаково на медиа-, аниме- И pop-темы (YouTube-тренды этим полны).
4. Если тем не хватает на дневной объём — добор из evergreen_themes.txt.

Выход: themes_daily_YYYY-MM-DD.txt (по одной теме на строку, для batch_print-совместимого
формата) + themes_daily_YYYY-MM-DD.json (полный план: тема, формат, источник, score).
Источник задания из pop-ленты помечается в JSON как "trend:pop:<sources>" (например
"trend:pop:anilist" или "trend:pop:youtube") — обратная совместимость с прежними
значениями "trend"/"evergreen" сохранена, это просто более узкая метка внутри "trend".
"""
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

import config
import franchise_scout

HERE = Path(__file__).resolve().parent

TREND_WATCH_DIR = Path(config.TREND_WATCH_DIR)
if not TREND_WATCH_DIR.is_absolute():
    TREND_WATCH_DIR = (HERE / TREND_WATCH_DIR).resolve()

EVERGREEN_FILE = HERE / "evergreen_themes.txt"

# Сколько строк топ-N брать из каждой CSV-ленты трендов перед передачей тематизатору.
_TOP_N_MEDIA = 8
_TOP_N_ANIME = 6
_TOP_N_POP = 8

_PARSER_TIMEOUT = 300  # секунд на каждый парсер trend-watch

# Источники pop_latest.csv, которые маршрутизируются в аниме-пул (ростер персонажей).
# Остальные (youtube, gtrends) идут в общий пул наравне с медиа-трендами.
_POP_ANIME_SOURCES = {"anilist", "jikan"}


def _run_parser(script: str) -> bool:
    """Запускает парсер trend-watch subprocess-ом. Возвращает True при успехе.
    НЕ валит вызывающий цикл — падение/таймаут парсера логируется и цикл продолжается
    с тем, что уже лежит в data/*.csv (может быть от предыдущего запуска)."""
    script_path = TREND_WATCH_DIR / script
    if not script_path.exists():
        print(f"  !! {script} не найден в {TREND_WATCH_DIR} — пропуск", flush=True)
        return False
    try:
        print(f"  запускаю {script}...", flush=True)
        r = subprocess.run([sys.executable, script], cwd=str(TREND_WATCH_DIR),
                           timeout=_PARSER_TIMEOUT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(f"  !! {script} завершился с кодом {r.returncode}: "
                  f"{r.stderr[-500:] if r.stderr else '(нет stderr)'}", flush=True)
            return False
        print(f"  {script} ok", flush=True)
        return True
    except subprocess.TimeoutExpired:
        print(f"  !! {script} превысил таймаут {_PARSER_TIMEOUT}с — пропуск", flush=True)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  !! {script} упал: {e}", flush=True)
        return False


def collect_trends() -> None:
    """Запускает все парсеры trend-watch. Не бросает исключения наружу.
    pop_watch.py — третий источник (pop-тренды anilist/jikan/youtube/gtrends),
    появляется в trend-watch/ параллельно; отсутствие файла или его падение
    обрабатывается ТАК ЖЕ, как у media_watch/anime_watch — предупреждение, без
    остановки цикла (build_daily_plan работает и без свежего pop_latest.csv)."""
    print("сбор трендов из trend-watch...", flush=True)
    _run_parser("media_watch.py")
    _run_parser("anime_watch.py")
    _run_parser("pop_watch.py")


def _read_csv_top(path: Path, top_n: int) -> list:
    """Читает CSV трендов (score,lemma,mentions,delta,is_new,sources_count,sources,
    example_text,example_url), сортирует по score убыв., берёт top_n строк."""
    if not path.exists():
        print(f"  !! {path.name} не найден — эта лента пуста", flush=True)
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                row["score"] = float(row.get("score", 0) or 0)
            except ValueError:
                row["score"] = 0.0
            rows.append(row)
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:top_n]


def _split_pop_rows(pop_rows: list) -> tuple:
    """Маршрутизация pop_latest.csv по колонке sources: anilist|jikan -> аниме-пул
    (ростер персонажей), youtube|gtrends -> общий пул (наравне с медиа-трендами).
    Неизвестный/пустой sources — консервативно в общий пул (не аниме)."""
    pop_anime, pop_general = [], []
    for r in pop_rows:
        src = str(r.get("sources", "")).strip().lower()
        if src in _POP_ANIME_SOURCES:
            pop_anime.append(r)
        else:
            pop_general.append(r)
    return pop_anime, pop_general


def read_trend_rows() -> tuple:
    """Возвращает (media_rows, anime_rows, pop_anime_rows, pop_general_rows) — топ-N
    по score из всех лент. pop_latest.csv расщепляется по колонке sources на аниме-
    и общий пул (см. _split_pop_rows)."""
    media = _read_csv_top(TREND_WATCH_DIR / "data" / "media_latest.csv", _TOP_N_MEDIA)
    anime = _read_csv_top(TREND_WATCH_DIR / "data" / "anime_latest.csv", _TOP_N_ANIME)
    pop = _read_csv_top(TREND_WATCH_DIR / "data" / "pop_latest.csv", _TOP_N_POP)
    pop_anime, pop_general = _split_pop_rows(pop)
    return media, anime, pop_anime, pop_general


# ── franchise_scout: глубокие досье для самых горячих аниме-тайтлов дня ────────

def _pick_deep_dive_titles(anime_rows: list, pop_anime_rows: list, n: int) -> list:
    """Выбирает n САМЫХ высокоскоровых аниме-тайтлов дня из аниме-трендов
    (anime_rows) и pop:anilist/pop:jikan группы (pop_anime_rows) — именно те
    источники, откуда theme_scout получает "лемма = название тайтла или
    персонажа". Дедуп по точному совпадению строки (без учёта регистра),
    сортировка по score по убыванию."""
    seen = {}
    for r in list(anime_rows) + list(pop_anime_rows):
        title = str(r.get("lemma", "")).strip()
        if not title:
            continue
        key = title.lower()
        score = float(r.get("score", 0) or 0)
        if key not in seen or score > seen[key][1]:
            seen[key] = (title, score)
    ranked = sorted(seen.values(), key=lambda x: x[1], reverse=True)
    return [title for title, _ in ranked[:n]]


def _collect_dossiers(titles: list) -> dict:
    """Досье franchise_scout для каждого тайтла из titles. Падение ОДНОГО тайтла
    (сеть/Claude/парсинг) печатает предупреждение и пропускается — не валит
    сбор трендов дня. Возвращает {title: dossier}, тайтлы без досье просто
    отсутствуют в результате."""
    dossiers = {}
    if not titles:
        return dossiers
    print(f"  franchise_scout: глубокое досье для {len(titles)} тайтлов дня: "
          f"{', '.join(titles)}", flush=True)
    for title in titles:
        try:
            dossiers[title] = franchise_scout.build_dossier(title, kind="anime")
        except Exception as e:  # noqa: BLE001 — падение досье не валит цикл тематизатора
            print(f"  !! franchise_scout не смог собрать досье для {title!r}: {e} "
                  f"— тематизатор отработает по этому тайтлу как раньше, без досье",
                  flush=True)
    return dossiers


def _dossier_block_text(title: str, dossier: dict) -> str:
    """Текстовый блок «ДОСЬЕ ФРАНШИЗЫ» для одного тайтла — вставляется в user-
    промпт тематизатора рядом с соответствующей леммой."""
    characters = dossier.get("characters") or []
    if not characters:
        return ""
    lines = [f"  ДОСЬЕ ФРАНШИЗЫ «{title}» (ИЗМЕРЕННАЯ популярность — приоритет над "
             f"твоими знаниями, см. системную инструкцию):"]
    for c in characters:
        name = c.get("name_ru") or c.get("name_en") or "?"
        moment = f" | принт: {c['print_moment']}" if c.get("print_moment") else ""
        lines.append(f"    - {name} [score={c.get('score', 0):.0f}]{moment}")
    return "\n".join(lines)


# ── Claude-«тематизатор и расширитель» ──────────────────────────────────────────

SYSTEM_SCOUT = (
    "Ты продюсер принтов для футболок. Тебе дают список трендовых лемм (ключевых слов) "
    "из СМИ/соцсетей и аниме-комьюнити с примером текста-упоминания — твоя задача "
    "развернуть их в конкретные задания на дизайн принта. "
    "ЛОГИКА РАЗВОРАЧИВАНИЯ: "
    "(1) Если лемма — название аниме-тайтла или персонажа (сериал, манга, игра) — "
    "разверни в РОСТЕР главных персонажей этого тайтла (сколько реально известно, "
    "не выдумывай персонажей), для каждого персонажа укажи 1-2 вариации образа "
    "(разная поза/арка/форма — если применимо) и формат (cutout или diecut). ВАЖНО: "
    "даже если у франшизы сотни персонажей (напр. One Piece) — бери ТОЛЬКО 15-20 "
    "самых главных/узнаваемых с их вариациями на ОДИН тайтл, НЕ пытайся перечислить "
    "весь ростер целиком, это раздувает ответ до неотправляемого размера. "
    "(2) Если лемма — общая тема/предмет/событие (НЕ персонаж) — разверни в НЕСКОЛЬКО "
    "принтопригодных КОНКРЕТНЫХ визуальных концептов по этой теме (не абстрактных). "
    "(3) Если в блоке ПОП-ТРЕНДЫ пришла ГОТОВАЯ СУЩНОСТЬ (название аниме-тайтла в "
    "romaji/english, заголовок видео, поисковый запрос) — это НЕ лемма для поиска "
    "смысла внутри строки, а готовое название, которое нужно превратить в задание "
    "напрямую: аниме-тайтл -> ростер персонажей (та же логика, что в пункте 1), "
    "заголовок видео/поисковый запрос -> визуальный концепт по сути запроса (та же "
    "логика, что в пункте 2). Не пытайся расщепить готовое название на подслова. "
    "(4) Если рядом с тайтлом идёт блок «ДОСЬЕ ФРАНШИЗЫ» — это ИЗМЕРЕННАЯ (не твоя "
    "собственная) популярность персонажей этого конкретного тайтла из реальных "
    "источников (AniList/MyAnimeList favourites, YouTube-эдиты, поисковые тренды). "
    "В этом случае строй ростер СТРОГО ИЗ ДОСЬЕ, по порядку убывания score (а не по "
    "своим знаниям о том, кто в тайтле формально главный) — если у досье указан "
    "print_moment для персонажа, впиши именно его в поле theme (конкретная сцена/ "
    "форма/поза), а не общее описание персонажа. Персонажей без досье для этого "
    "тайтла не добавляй сверх того, что в досье, даже если знаешь других героев — "
    "досье уже отражает, кто реально резонирует у фанатов ПРЯМО СЕЙЧАС. "
    "СТРОГИЙ ФИЛЬТР ПРИНТОПРИГОДНОСТИ (ОБЯЗАТЕЛЬНО, без исключений, ОДИНАКОВО "
    "применяется ко ВСЕМ блокам — общие тренды, аниме-тренды И поп-тренды, включая "
    "YouTube-заголовки, которые часто полны такими темами) — ОТСЕКАЙ МОЛЧА (просто не "
    "включай в результат, без пояснений) любую лемму/название/пример текста про: "
    "политику и политиков, войну/боевые действия/армию, катастрофы и аварии, трагедии, "
    "смерти и травмы реальных людей, криминал и преступления, теракты, стихийные "
    "бедствия. Такие темы НИКОГДА не превращай в принт, даже завуалированно. Если ПОСЛЕ "
    "фильтра тема осталась нейтральной (аниме, кино, игры, техника, машины, животные, "
    "бытовые явления, мемы, спорт, еда, природа) — работай с ней. "
    "РЕАЛЬНЫЕ ЛЮДИ — ВСЕГДА РАЗРЕШЕНЫ (правило концепции владельца): реальные публичные "
    "персоны — спортсмены, музыканты, актёры, блогеры, стримеры и прочие знаменитости — "
    "полноценные темы принтов; НЕ отсекай их и НЕ заменяй обезличенными стилизациями, "
    "трендовый футболист/артист -> именное задание на принт с его узнаваемыми приметами. "
    "Единственное исключение: политики и персоны из запрещённого списка выше — отсекай. "
    "ВАЖНО про смешанные группы: фильтр применяется ПОСТРОЧНО, а не к группе целиком — "
    "если среди трендов есть и война, и футбольный матч, отсеки войну, но матч разверни. "
    "СПОРТИВНЫЕ МАТЧИ/ТУРНИРЫ (запросы вида «англия мексика», «четвертьфинал ЧМ») — это "
    "НЕ политика и НЕ трансляция: разверни в именные задания на звёзд матча (имена часто "
    "прямо в example_text: Беллингем, Холанн и т.п.) + 1-2 генерик-концепта турнира. "
    "ВЫМЫШЛЕННЫЕ ПРОИЗВЕДЕНИЯ: запрещённый список касается РЕАЛЬНЫХ событий и людей; "
    "фильм/сериал/аниме/дорама с криминальным, военным или мрачным СЮЖЕТОМ — печатабельны. "
    "Кино-тайтл или дорама из трендов -> задания на главных персонажей франшизы (если "
    "узнаваемы) или яркий постер-образ по духу произведения. "
    "Для каждого задания верни JSON-объект: "
    "{\"theme\":\"<готовая тема-описание для арт-директора принтов, на русском, "
    "конкретная — имя персонажа+тайтл, или конкретный предмет/сцена>\","
    "\"format\":\"<cutout ИЛИ diecut>\"}. "
    "Отвечай СТРОГО JSON-массивом объектов, БЕЗ markdown и пояснений. Если ПОСЛЕ фильтра "
    "не осталось ни одной годной лемм — верни пустой массив []."
)


# Claude пытается выдать ВЕСЬ дневной объём (может быть 500) ОДНИМ JSON-ответом, если
# попросить "нужно ~500 заданий" напрямую — ответ обрезается по max_tokens на середине
# JSON-массива (проверено: 9193 символа, незакрытый массив, json.loads падает). Явный
# потолок ЗА ОДИН вызов — тематизатор просят развернуть тренды в РАЗУМНОЕ число заданий
# (не пытаться закрыть весь дневной план одним тайтлом), а добор до полного объёма идёт
# через evergreen-пул (или несколько вызовов, если понадобится — см. build_daily_plan).
_MAX_TASKS_PER_CALL = 60


def _ask_claude_scout(media_rows: list, anime_rows: list, target_n: int,
                       pop_rows: list = None, pop_is_anime: bool = False,
                       dossiers: dict = None) -> str:
    """Один вызов тематизатора. pop_rows (если задан) добавляется отдельным блоком
    «ПОП-ТРЕНДЫ» с явной пометкой, что это готовые названия сущностей (тайтл/видео/
    запрос) — их нужно превращать в задания напрямую, а не парсить как леммы.
    pop_is_anime переключает подпись блока (тайтлы аниме vs общие видео/запросы) —
    сама логика разворачивания (пункт 3 SYSTEM_SCOUT) от неё не зависит, это только
    для читаемости промпта.

    dossiers (если задан) — {title: dossier} из franchise_scout.build_dossier
    (см. _collect_dossiers) — для лемм из anime_rows/pop_rows, совпадающих с
    ключом досье (без учёта регистра), сразу под строкой леммы добавляется блок
    «ДОСЬЕ ФРАНШИЗЫ» (см. пункт 4 SYSTEM_SCOUT)."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    dossiers = dossiers or {}
    dossiers_lower = {k.lower(): (k, v) for k, v in dossiers.items()}

    def _lemma_line(r: dict) -> list:
        out_lines = [f"- {r['lemma']} (score={r['score']}): "
                     f"{r.get('example_text', '')[:200]}"]
        hit = dossiers_lower.get(str(r.get("lemma", "")).strip().lower())
        if hit:
            title, dossier = hit
            block = _dossier_block_text(title, dossier)
            if block:
                out_lines.append(block)
        return out_lines

    call_n = min(target_n, _MAX_TASKS_PER_CALL)
    # Соло-вызов на pop-группу из нескольких готовых сущностей: не просить полный
    # дневной объём с 5-8 строк — Claude раздувает ответ до потолка max_tokens и
    # JSON обрезается посреди массива (наблюдалось на всех неаниме-группах разом).
    if pop_rows and not media_rows and not anime_rows:
        call_n = min(call_n, max(6, len(pop_rows) * 4))
    lines = ["ОБЩИЕ ТРЕНДЫ (СМИ/соцсети):"]
    for r in media_rows:
        lines.extend(_lemma_line(r))
    lines.append("\nАНИМЕ-ТРЕНДЫ:")
    for r in anime_rows:
        lines.extend(_lemma_line(r))
    if pop_rows:
        label = "названия аниме-тайтлов" if pop_is_anime else "заголовки видео/поисковые запросы"
        lines.append(f"\nПОП-ТРЕНДЫ ({label}, ГОТОВЫЕ названия сущностей — см. пункт 3 "
                     f"инструкции, НЕ леммы для разбора):")
        for r in pop_rows:
            lines.extend(_lemma_line(r))
    lines.append(f"\nРазверни эти тренды примерно в {call_n} заданий на принты суммарно "
                 f"(ОРИЕНТИР, не жёсткий потолок — можно чуть меньше или больше). НЕ "
                 f"пытайся закрыть весь дневной объём одним тайтлом/темой — бери "
                 f"РАЗУМНОЕ число заданий с каждого тренда (ростер персонажей — не "
                 f"более 15-20 на один тайтл, даже если у франшизы сотни персонажей), "
                 f"остальной объём дня доберётся из отдельного вечнозелёного пула.")
    user = "\n".join(lines)
    try:
        resp = client.messages.create(
            model=config.MODEL,
            max_tokens=8000,
            system=SYSTEM_SCOUT,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:  # noqa: BLE001 — сеть/баланс/rate-limit/5xx Claude:
        # не должно ронять daily_prints.py необработанным traceback. Пустая строка —
        # тот же сигнал, что и невалидный JSON: _parse_scout("") -> None -> вызывающий
        # _ask_and_parse_with_retry честно ретраит один раз, при двойном сбое печатает
        # предупреждение и пропускает ТОЛЬКО этот блок (см. expand_trends_to_tasks).
        print(f"  !! тематизатор: вызов Claude не удался: {e}", flush=True)
        return ""
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _parse_scout(text: str) -> list:
    # Кандидат 1: закрытый массив как есть. Кандидат 2: ремонт обрезанного по
    # max_tokens ответа — от первой '[' до последнего полного объекта '}' + ']'
    # (лучше принять 38 заданий из 40, чем выбросить весь блок).
    candidates = []
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        candidates.append(m.group(0))
    start = text.find("[")
    cut = text.rfind("}")
    if start != -1 and cut > start:
        candidates.append(text[start:cut + 1] + "]")
    data = None
    for cand in candidates:
        try:
            data = json.loads(cand)
            break
        except Exception:
            continue
    if not isinstance(data, list):
        return None  # сбой парсинга; валидный пустой [] — НЕ сбой, вернётся ниже как []
    out = []
    for x in data:
        if isinstance(x, dict) and str(x.get("theme", "")).strip():
            fmt = str(x.get("format") or "").strip().lower()
            out.append({"theme": str(x["theme"]).strip(),
                        "format": fmt if fmt in ("cutout", "diecut") else "diecut"})
    return out


def _ask_and_parse_with_retry(media_rows: list, anime_rows: list, target_n: int,
                               pop_rows: list = None, pop_is_anime: bool = False,
                               dossiers: dict = None) -> list:
    """Один вызов тематизатора + парсинг, с 1 ретраем при сбое JSON (та же схема,
    что раньше была inline в expand_trends_to_tasks). Пустой список = двойной сбой."""
    text = _ask_claude_scout(media_rows, anime_rows, target_n, pop_rows, pop_is_anime,
                              dossiers)
    tasks = _parse_scout(text)
    if tasks is None:  # именно сбой парсинга; валидный [] ретраить не нужно
        _dump_scout_failure(text, attempt=1)
        text = _ask_claude_scout(media_rows, anime_rows, target_n, pop_rows, pop_is_anime,
                                  dossiers)
        tasks = _parse_scout(text)
        if tasks is None:
            _dump_scout_failure(text, attempt=2)
    return tasks


def _dump_scout_failure(text: str, attempt: int) -> None:
    """Сырой ответ Claude при сбое парсинга — в файл, иначе сбой недиагностируем."""
    try:
        dump_dir = HERE / "out_batch" / "scout_failures"
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (dump_dir / f"scout_fail_{stamp}_try{attempt}.txt").write_text(
            text or "<пустой ответ>", encoding="utf-8")
    except Exception:
        pass


def expand_trends_to_tasks(media_rows: list, anime_rows: list, target_n: int,
                            pop_anime_rows: list = None,
                            pop_general_rows: list = None,
                            dossiers: dict = None) -> list:
    """Тематизатор Claude: тренды -> список заданий {theme, format, source}.

    НЕ откатывается тихо при сбое парсинга JSON (как art_director): 1 ретрай на каждый
    вызов, затем явная ошибка на ЭТОТ вызов — остальные блоки (media/anime, pop) всё
    равно пробуются, план строится из того, что реально распарсилось. Если совсем
    ничего не распарсилось ни из одного блока — вызывающий код (build_daily_plan)
    переходит на чистый evergreen-добор.

    pop_anime_rows/pop_general_rows группируются по конкретному значению колонки
    sources (anilist/jikan/youtube/gtrends) и уходят ОТДЕЛЬНЫМ вызовом тематизатора
    каждая группа — так задания из pop-ленты получают точную метку источника
    "trend:pop:<sources>" без необходимости просить Claude возвращать это поле.

    dossiers — {title: dossier} из franchise_scout (см. _collect_dossiers) для
    самых высокоскоровых аниме-тайтлов дня; передаётся ТОЛЬКО в аниме-блоки
    (media/anime и pop:anime группу) — досье в общем/pop:general блоке не нужно,
    там лемм-тайтлов франшиз не бывает по построению (_pick_deep_dive_titles
    берёт только из anime_rows/pop_anime_rows).
    """
    pop_anime_rows = pop_anime_rows or []
    pop_general_rows = pop_general_rows or []
    dossiers = dossiers or {}
    all_tasks = []

    if media_rows or anime_rows:
        tasks = _ask_and_parse_with_retry(media_rows, anime_rows, target_n,
                                          dossiers=dossiers)
        if tasks:
            for t in tasks:
                t["source"] = "trend"
            all_tasks.extend(tasks)
        elif tasks is None:
            print("  !! тематизатор (медиа/аниме) не смог собрать валидный JSON "
                  "дважды подряд — этот блок пропущен", flush=True)
        else:
            print("  тематизатор (медиа/аниме): фильтр отсёк все темы — штатно",
                  flush=True)

    # Pop-строки группируются по точному значению sources (обычно 1-4 группы: anilist,
    # jikan, youtube, gtrends) — каждая группа уходит отдельным вызовом, чтобы задания
    # можно было пометить "trend:pop:<sources>" без гадания, откуда какое задание.
    for pop_rows, is_anime in ((pop_anime_rows, True), (pop_general_rows, False)):
        if not pop_rows:
            continue
        groups = {}
        for r in pop_rows:
            src = str(r.get("sources", "")).strip().lower() or "unknown"
            groups.setdefault(src, []).append(r)
        for src, rows in groups.items():
            tasks = _ask_and_parse_with_retry([], [], target_n, pop_rows=rows,
                                               pop_is_anime=is_anime,
                                               dossiers=dossiers if is_anime else None)
            if tasks:
                for t in tasks:
                    t["source"] = f"trend:pop:{src}"
                all_tasks.extend(tasks)
            elif tasks is None:
                print(f"  !! тематизатор (pop:{src}) не смог собрать валидный JSON "
                      f"дважды подряд — этот блок пропущен", flush=True)
            else:
                print(f"  тематизатор (pop:{src}): фильтр отсёк все темы группы — "
                      f"штатно, блок пуст", flush=True)

    if not all_tasks:
        print("  !! ни один блок тематизатора не дал валидных заданий — "
              "переходим на чистый evergreen-добор", flush=True)
    return all_tasks


def _interleave_by_source(tasks: list) -> list:
    """Чередует задания по группам source (round-robin), сохраняя относительный
    порядок ВНУТРИ каждой группы. Нужно для того, чтобы срез списка ЛЮБОЙ длины
    (tasks[:target_n] здесь, либо plan[:limit] в daily_prints.py) содержал
    пропорциональное представительство ВСЕХ источников, а не "все trend сначала,
    потом pop, потом evergreen".

    Без этого при небольшом --target/--limit (например 10) блок media+anime
    ("trend"), который сам по себе обычно даёт десятки заданий, съедает срез
    целиком, а trend:pop:anilist/jikan (обычно добавляются ПОСЛЕ в
    expand_trends_to_tasks) структурно не могут попасть в маленький срез —
    найдено тестировщиком на реальном прогоне --dry-run --limit 10 (0 заданий
    trend:pop:* при 10/10 trend). Порядок группы (не порядок заданий внутри
    неё) не имеет значения для итогового объёма плана — при большом target_n
    результат идентичен несортированному списку (те же элементы, другой порядок)."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for t in tasks:
        src = t.get("source", "unknown")
        if src not in groups:
            groups[src] = []
            order.append(src)
        groups[src].append(t)

    interleaved = []
    idx = 0
    remaining = sum(len(v) for v in groups.values())
    while remaining > 0:
        src = order[idx % len(order)]
        bucket = groups[src]
        if bucket:
            interleaved.append(bucket.pop(0))
            remaining -= 1
        idx += 1
    return interleaved


# ── Evergreen-добор ──────────────────────────────────────────────────────────────

def _read_evergreen() -> list:
    if not EVERGREEN_FILE.exists():
        return []
    lines = []
    with open(EVERGREEN_FILE, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                lines.append(ln)
    return lines


def build_daily_plan(target_n: int) -> list:
    """Полный план дня: тренды (расширенные тематизатором) + evergreen-добор до
    target_n. Возвращает список {theme, format, source}, source = "trend" |
    "trend:pop:<anilist|jikan|youtube|gtrends>" | "evergreen"."""
    media_rows, anime_rows, pop_anime_rows, pop_general_rows = read_trend_rows()

    # franchise_scout: глубокое досье (измеренная популярность персонажей) для
    # config.FRANCHISE_DEEP_N самых высокоскоровых аниме-тайтлов дня — падение
    # ОДНОГО тайтла (сеть/Claude) не валит сбор трендов, см. _collect_dossiers.
    deep_dive_titles = _pick_deep_dive_titles(anime_rows, pop_anime_rows,
                                               config.FRANCHISE_DEEP_N)
    dossiers = _collect_dossiers(deep_dive_titles)

    tasks = expand_trends_to_tasks(media_rows, anime_rows, target_n,
                                   pop_anime_rows, pop_general_rows, dossiers)
    # Чередуем ПО ИСТОЧНИКАМ до финальной обрезки tasks[:target_n] ниже (и до
    # plan[:limit] в daily_prints.py) — иначе большой блок "trend" (media+anime)
    # съедает срез целиком, а "trend:pop:*" (добавляется позже в
    # expand_trends_to_tasks) структурно не может попасть в маленький --target/
    # --limit. См. docstring _interleave_by_source().
    tasks = _interleave_by_source(tasks)
    print(f"  из трендов получено {len(tasks)} заданий", flush=True)

    if len(tasks) < target_n:
        evergreen = _read_evergreen()
        need = target_n - len(tasks)
        # Не повторяем темы, которые уже взяты из трендов (по точному совпадению строки).
        used_themes = {t["theme"].strip().lower() for t in tasks}
        added = 0
        idx = 0
        while added < need and idx < len(evergreen) * 3:  # запас на случай мало строк
            theme = evergreen[idx % len(evergreen)] if evergreen else None
            idx += 1
            if not theme:
                break
            key = theme.strip().lower()
            # Разрешаем повтор evergreen-темы несколько раз только если строк реально
            # не хватает на нужный объём (используем round-robin по списку).
            if key in used_themes and idx <= len(evergreen):
                continue
            used_themes.add(key)
            tasks.append({"theme": theme, "format": "diecut", "source": "evergreen"})
            added += 1
        print(f"  добрано {added} заданий из evergreen-пула", flush=True)

    return tasks[:target_n]


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=config.PRINTS_PER_DAY,
                     help="сколько заданий собрать на день (дефолт из .env PRINTS_PER_DAY)")
    ap.add_argument("--skip-collect", action="store_true",
                     help="не запускать media_watch.py/anime_watch.py заново — работать "
                          "с уже лежащими CSV в trend-watch/data/")
    args = ap.parse_args()

    if not args.skip_collect:
        collect_trends()
    else:
        print("пропускаю запуск парсеров (--skip-collect) — читаю существующие CSV",
              flush=True)

    print(f"\nсобираю дневной план (цель: {args.target} заданий)...", flush=True)
    plan = build_daily_plan(args.target)

    date_str = time.strftime("%Y-%m-%d")
    txt_path = HERE / f"themes_daily_{date_str}.txt"
    json_path = HERE / f"themes_daily_{date_str}.json"

    with open(txt_path, "w", encoding="utf-8") as f:
        for t in plan:
            f.write(t["theme"] + "\n")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    n_trend = sum(1 for t in plan if t["source"].startswith("trend"))
    n_pop = sum(1 for t in plan if t["source"].startswith("trend:pop:"))
    n_ever = sum(1 for t in plan if t["source"] == "evergreen")
    print(f"\nГотово: {len(plan)} заданий ({n_trend} из трендов [{n_pop} pop], "
          f"{n_ever} из evergreen)")
    print(f"-> {txt_path}")
    print(f"-> {json_path}")


if __name__ == "__main__":
    main()
