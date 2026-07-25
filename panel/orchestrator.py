# -*- coding: utf-8 -*-
"""orchestrator.py — логика панели: тема/персонажи/стили -> список готовых PNG.

Тонкий слой НАД существующим движком print-factory-nb (art_director,
franchise_scout, batch_print) — сама генерация не переписывается, см.
panel/PLAN.md "Логика оркестрации". app.py вызывает `plan_tasks()` один раз на
job, затем `render_task()` по очереди на каждый элемент плана (в фоновом потоке),
обновляя прогресс job-стора между вызовами.

Ветки (вход: styles[], count, theme, characters, free_prompt):
  0. free_prompt заполнен -> отдельный свободный режим: запрос без поиска
     франшизы напрямую дорабатывает арт-директор, стиль всегда auto.
  1. characters заполнено -> эти персонажи, добито до count круговой ротацией
     персонажей и стилей.
  2. characters пусто, theme похоже на тайтл (franchise_scout.build_dossier
     реально нашёл персонажей) -> топ-персонажи досье, тоже добито до count.
  3. иначе -> count дизайнов по самой теме (theme используется как label для
     всех задач).
Как понять, что тема — тайтл: пробуем build_dossier честно (сам модуль умеет
graceful degradation — на не-тайтл или без сигналов возвращает пустой
characters), сеть/LLM-сбой тоже трактуется как "не тайтл" — падать не должны.
"""
from __future__ import annotations

import itertools
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# Движок лежит на уровень выше panel/ — добавляем в sys.path независимо от того,
# как импортирован этот модуль (напрямую, как panel.orchestrator, или через
# app.py, который уже мог это сделать) — идемпотентно.
_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

import art_director      # noqa: E402
import batch_print        # noqa: E402
import franchise_scout    # noqa: E402
import greenkey_postprocess  # noqa: E402

import settings           # noqa: E402  (panel/settings.py, тот же каталог)


# Стили, которые сами формируют тему партии. Пользователь выбирает такой чекбокс,
# количество и сразу запускает генерацию — franchise_scout для пустой темы не нужен.
_AUTONOMOUS_STYLE_BRIEFS = {
    "37_auto_racing_editorial": [
        "Автономная авто-серия: выбери один реальный болид Formula/Grand Prix из "
        "выразительной эпохи и построй смысловой принт вокруг его модели, номера, "
        "страны и одной знаменитой трассы; не копируй случайную стену спонсоров.",
        "Автономная авто-серия: выбери один узнаваемый endurance-прототип или GT "
        "для темы выносливости; смысловой текст про ночь, дистанцию, класс или "
        "легендарный автодром, без выдуманных характеристик.",
        "Автономная авто-серия: выбери одну культовую раллийную машину и свяжи "
        "типографику с покрытием, погодой, страной, эпохой или номером спецучастка.",
        "Автономная авто-серия: выбери один культовый JDM/tuner автомобиль; крупное "
        "точное имя модели, короткая осмысленная night-drive/precision фраза, город "
        "или горный маршрут и год только если уверен.",
        "Автономная авто-серия: выбери один европейский суперкар; сделай премиальный "
        "редакционный принт о скорости, форме и месте происхождения, с минимумом "
        "точных текстовых акцентов.",
        "Автономная авто-серия: выбери один классический спортивный автомобиль; "
        "используй heritage-композицию с названием модели, страной, годом/эпохой и "
        "короткой фразой о наследии.",
        "Автономная авто-серия: выбери один touring car или силуэт кольцевых гонок; "
        "соедини машину с номером, сеткой timing marks и смысловой фразой о точности.",
        "Автономная авто-серия: выбери один американский muscle/stock-car образ; "
        "крупное имя модели, номер и короткая фраза о мощности, без фальшивых фактов.",
        "Автономная авто-серия: выбери один современный электрический гиперкар или "
        "электрический гоночный прототип; смысловой текст про мгновенный момент и "
        "новую эру, без рекламных клише.",
        "Автономная авто-серия: выбери один редкий roadster или lightweight coupe; "
        "минималистичная museum-spec композиция с моделью, страной и одной честной "
        "эмоциональной фразой для автолюбителя.",
    ],
}


def allows_theme_free(styles: list[str]) -> bool:
    """Пустая тема допустима, только если ВСЕ выбранные стили автономны."""
    selected = [str(style).strip() for style in (styles or []) if str(style).strip()]
    return bool(selected) and all(style in _AUTONOMOUS_STYLE_BRIEFS for style in selected)


def _autonomous_briefs(style_id: str, count: int) -> list[str]:
    briefs = _AUTONOMOUS_STYLE_BRIEFS[style_id]
    return _expand_round_robin(briefs, count)


# ── slug для имён файлов ─────────────────────────────────────────────────────

_CYRILLIC_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def sanitize_slug(text: str, fallback: str = "item", max_len: int = 40) -> str:
    """Кириллица/спецсимволы -> безопасный slug для имени файла (тема на
    кириллице -> латиница, см. PLAN.md "Технические примечания")."""
    text = (text or "").strip().lower()
    translit = "".join(_CYRILLIC_TRANSLIT.get(ch, ch) for ch in text)
    ascii_text = unicodedata.normalize("NFKD", translit).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or fallback


def _split_characters(raw: str) -> list[str]:
    parts = re.split(r"[,\n;]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


def _expand_round_robin(values: list, count: int) -> list:
    """Растягивает (циклически повторяя) или обрезает список до длины count.
    Используется и для персонажей (ручных и из досье), и для стилей."""
    if not values:
        return []
    cyc = itertools.cycle(values)
    return [next(cyc) for _ in range(count)]


# ── план задач ────────────────────────────────────────────────────────────────

@dataclass
class DesignTask:
    index: int          # 1-based порядковый номер в job
    label: str           # что передаём в art_director.make_ideas как theme
    style_id: str         # style_pref
    tag: str              # уникальное имя файла (без расширения)
    source: str            # "free" | "characters" | "franchise" | "theme"
    # Протяжка из досье franchise_scout (ветка "franchise"): надёжные имя ЛАТИНИЦЕЙ и
    # тайтл персонажа, которыми ПЕРЕЗАПИСЫВАЕМ character_en/title_en в дизайне ПОСЛЕ
    # арт-директора — тот на нишевых/свежих тайтлах не узнаёт персонажа и оставляет
    # character_en пустым (или трактует имя буквально: "Энджин"->движок), из-за чего
    # character_ref не тянет референс и сходство теряется. Пусто для веток
    # "characters"/"theme" (там character_en решает арт-директор, как раньше).
    char_en: str = ""       # name_en персонажа из досье (для character_ref)
    title_hint: str = ""     # тайтл франшизы из досье (для character_ref, fallback)


def plan_tasks(
    styles: list[str],
    count: int,
    theme: str,
    characters: str,
    free_prompt: str = "",
) -> list[DesignTask]:
    """Строит план из `count` задач по правилам PLAN.md. Не делает никаких
    платных вызовов КРОМЕ (возможно) одного franchise_scout.build_dossier,
    когда characters пусто и theme задана (ветка 2/3, см. модульный докстринг)."""
    theme = (theme or "").strip()
    characters = (characters or "").strip()
    free_prompt = (free_prompt or "").strip()
    count = max(1, int(count))

    # Свободный запрос — отдельный режим: чекбоксы намеренно игнорируются, чтобы
    # арт-директор сам выбрал композицию под весь пользовательский бриф.
    style_list = (["auto"] if free_prompt else
                  ([s for s in (styles or []) if s] or [settings.DEFAULT_STYLE]))

    names = _split_characters(characters)
    title_hint = ""
    # entries — список (label, char_en): label уходит арт-директору как theme,
    # char_en (name_en из досье) ПЕРЕЗАПИШЕТ character_en в дизайне для character_ref.
    if free_prompt:
        entries = [(free_prompt, "")] * count
        source = "free"
    elif names:
        entries = [(n, "") for n in _expand_round_robin(names, count)]
        source = "characters"
    elif not theme and allows_theme_free(style_list):
        # Сейчас автономный стиль один; код оставляет цикл стилей расширяемым.
        # Краткие категории дают разнообразие внутри партии, а конкретную машину,
        # факты и смысловой текст выбирает арт-директор.
        entries = [
            (brief, "")
            for brief in _autonomous_briefs(style_list[0], count)
        ]
        source = "autonomous_style"
    else:
        dossier_pairs: list[tuple[str, str]] = []  # (label=name_ru, name_en)
        if theme:
            try:
                dossier = franchise_scout.build_dossier(theme, kind="auto")
                # title_ref — romaji/english-тайтл для character_ref (точный
                # title-match); title/theme (может быть кириллицей) — fallback.
                title_hint = (dossier.get("title_ref") or dossier.get("title") or theme).strip()
                for c in (dossier.get("characters") or []):
                    label = (c.get("name_ru") or c.get("name_en") or "").strip()
                    if label:
                        dossier_pairs.append((label, (c.get("name_en") or "").strip()))
            except Exception as e:  # noqa: BLE001 — сеть/LLM не должны валить панель
                logger.warning(f"franchise_scout.build_dossier({theme!r}) упал, "
                                f"считаем тему НЕ тайтлом: {e}")
                dossier_pairs, title_hint = [], ""
        if dossier_pairs:
            entries = _expand_round_robin(dossier_pairs, count)
            source = "franchise"
        else:
            if not theme:
                raise ValueError(
                    "нужно указать тему, персонажей, свободный запрос или выбрать "
                    "автономный стиль"
                )
            entries = [(theme, "")] * count
            source = "theme"
            title_hint = ""

    style_cycle = itertools.cycle(style_list)
    tasks: list[DesignTask] = []
    used_tags: set[str] = set()
    for i, (label, char_en) in enumerate(entries, start=1):
        style_id = next(style_cycle)
        base = sanitize_slug(label, fallback="item")
        tag = f"{i:02d}_{base}_{style_id}"[:120]
        suffix = 2
        while tag in used_tags:
            tag = f"{i:02d}_{base}_{style_id}_{suffix}"[:120]
            suffix += 1
        used_tags.add(tag)
        tasks.append(DesignTask(
            index=i, label=label, style_id=style_id, tag=tag, source=source,
            char_en=char_en,
            title_hint=(title_hint if source == "franchise" else ""),
        ))
    return tasks


# ── рендер одной задачи ────────────────────────────────────────────────────────

# Сколько раз всего пытаться отрендерить один слот. >1 — авто-ретрай при провале
# (напр. HARD-reject кадра без хромакея: nano-banana изредка перерисовывает эталон-
# портрет персонажа вместо стиля — глюк интермиттентный, свежий make_ideas даёт новый
# промпт/сид/сценарий и обычно проходит со 2-й попытки, чтобы батч не оставался с
# дыркой). Каждая попытка — платная генерация; 2 = максимум 1 доп. попытка на слот.
_RENDER_ATTEMPTS = int(os.getenv("PANEL_RENDER_ATTEMPTS", "2"))


def _render_once(task: "DesignTask", outdir: Path) -> dict:
    """Одна попытка рендера слота (без ретрая). См. render_task."""
    try:
        # `auto` означает именно отсутствие принудительного банковского стиля:
        # арт-директор выбирает композицию по теме, а не получает буквальный id.
        style_pref = None if task.style_id == "auto" else task.style_id
        designs = art_director.make_ideas(
            task.label, 1, fmt="cutout", style_pref=style_pref
        )
        design = designs[0]
    except Exception as e:  # noqa: BLE001
        return {"tag": task.tag, "ok": False, "path": None, "error": f"арт-директор: {e}"}

    # Протяжка из досье (ветка franchise): перезаписываем character_en/title_en
    # НАДЁЖНЫМИ значениями досье поверх догадки арт-директора — иначе на нишевых
    # тайтлах он оставляет character_en пустым и character_ref не тянет референс
    # (см. DesignTask.char_en). character_en заменяем всегда (если досье его знает);
    # title_en — только если арт-директор оставил пустым (его romaji-тайтл, когда он
    # его узнал, точнее нашего title_hint).
    if task.char_en:
        design["character_en"] = task.char_en
        # Тайтл из досье ПЕРЕЗАПИСЫВАЕТ догадку арт-директора (для нишевых он ставит
        # мусор вроде 'Original Concept' -> title-match референса ломается). title_hint
        # = romaji title_ref, если досье его добыло.
        if task.title_hint:
            design["title_en"] = task.title_hint
        logger.info(f"[{task.tag}] протяжка досье -> character_en={task.char_en!r} "
                    f"title_en={design.get('title_en')!r}")

    try:
        result = batch_print.render_design(design, task.tag, outdir, green_only=True)
    except Exception as e:  # noqa: BLE001
        return {"tag": task.tag, "ok": False, "path": None, "error": f"render_design: {e}"}

    if not result.get("ok"):
        return {"tag": task.tag, "ok": False, "path": None,
                "error": result.get("error") or "неизвестная ошибка генерации"}

    path = result.get("green")
    if not path:
        return {"tag": task.tag, "ok": False, "path": None,
                "error": "render_design вернул ok=True без пути green_only"}
    return {"tag": task.tag, "ok": True, "path": path, "error": None}


def render_task(task: DesignTask, outdir: Path) -> dict:
    """Один дизайн с авто-ретраем (_RENDER_ATTEMPTS попыток): make_ideas(label,
    style_pref=style_id) -> render_design(..., green_only=True) -> GreenKey.
    Ретрай относится только к генерации: если уже оплаченный хромакейный PNG
    получен, сбой финальной подготовки НЕ запускает новую платную генерацию.
    Возвращает
    {"tag", "ok", "path", "error"} — НИКОГДА не бросает исключение наружу (ошибка
    одного дизайна не должна ронять весь job, см. app.py._run_job). При провале
    (напр. HARD-reject off-style-кадра) пробует заново — глюк интермиттентный."""
    res = {"tag": task.tag, "ok": False, "path": None, "error": "не запускалось"}
    for attempt in range(1, max(1, _RENDER_ATTEMPTS) + 1):
        res = _render_once(task, outdir)
        if res.get("ok"):
            break
        if attempt < max(1, _RENDER_ATTEMPTS):
            logger.warning(f"[{task.tag}] попытка {attempt} провалилась "
                           f"({res.get('error')}) — авто-ретрай")
    if not res.get("ok"):
        return res

    try:
        prepared = greenkey_postprocess.process_file(res["path"], sharp=True)
    except Exception as e:  # noqa: BLE001 — исходный хромакейный PNG сохранён
        logger.exception(f"[{task.tag}] GreenKey не подготовил финальный PNG")
        return {
            "tag": task.tag,
            "ok": False,
            "path": None,
            "error": f"GreenKey: {e}",
        }

    logger.info(
        f"[{task.tag}] GreenKey: {prepared.key} bg={prepared.detected_bg} -> RGBA"
    )
    return {"tag": task.tag, "ok": True, "path": str(prepared.path), "error": None}
