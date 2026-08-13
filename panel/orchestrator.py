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

_AUTOMOTIVE_STYLE_ID = "37_auto_racing_editorial"

_YOUTH_MOTION_STYLE_ID = "38_youth_motion_mix"

_ROCK_BAND_STYLE_ID = "39_rock_band_print"

# Три композиционные семьи извлечены из пользовательских референсов: крупный
# фронтмен с инструментом, цветовой коллаж всего состава и сюжетный metal-маскот.
# Их назначает код ДО вызова арт-директора, иначе независимые генерации быстро
# сходятся к одному безопасному шаблону с гитаристом и круглым диском.
_ROCK_BAND_VARIANTS = (
    "A FRONTMAN IMPACT / crimson + antique gold — one iconic lead performer from the requested band, waist-up or three-quarter view, one real instrument cutting the composition on a strong diagonal, and one offset circular stage-light disc; render as a hand-inked 1970s gig-print with broad gouache colour masses and economical contour lines",
    "B COLOURIZED LINEUP COLLAGE / acid lime + cyan + magenta + violet ON BLUE CHROMA — the requested band's recognizable lineup grouped in one candid editorial moment, each member treated like a real cut-paper photo silkscreen in one distinct flat colour channel; because one member may be green-tinted, force a uniform BLUE chroma field and use one irregular acid-lime support field, not a rectangular page",
    "C METAL MASCOT NARRATIVE / inferno red + ember orange + steel blue — one original genre-appropriate mascot or fantasy scene derived from the requested band's themes, simplified into a hand-painted 1980s metal screenprint with two large silhouettes and a large sun or moon disc; no micro-detailed armour and no copied album artwork",
    "A FRONTMAN IMPACT / bone + electric blue + scarlet — one vocalist in a peak live gesture, microphone cable or instrument creating the diagonal; use broken rays and a partial disc instead of a complete badge",
    "B COLOURIZED LINEUP COLLAGE / cobalt + warm yellow + vermilion ON BLUE CHROMA — full band in an informal backstage or rehearsal interaction, posterized into separate colour zones with genuine chroma gaps between bodies and accents; force blue chroma if any person or accent uses green",
    "C METAL MASCOT NARRATIVE / toxic violet + flame orange + cold silver — an original symbolic creature, relic or mythic confrontation connected to the band's lyrical world; asymmetrical action and no generic skull-only shortcut",
    "A+B LIVE COLLAGE HYBRID / red + cream + cyan — one dominant performer in front with two or three smaller bandmates behind, diagonal instrument and torn geometric stage-light fragments; no duplicated person",
    "B+C LINEUP MYTH HYBRID / lime + magenta + charcoal — the band lineup integrated with one symbolic creature or emblem as a secondary layer, with each human still readable and correctly counted",
    "A+C HERO AND MASCOT HYBRID / antique gold + crimson + steel — one performer confronting or overlapping an original mascot silhouette, with the instrument forming the compositional spine and an incomplete disc behind them",
)

_ROCK_BAND_COMMON_CONTRACT = (
    "ROCK BAND PRINT COMMON CONTRACT: the user's theme is the band name and must stay "
    "the subject of this design. Create fresh merchandise artwork, not a copy of an "
    "album cover, photograph, official logo or the supplied reference. Put one huge, "
    "correctly spelled band-name wordmark at the top; preserve the exact requested "
    "spelling but design new letterforms appropriate to the genre. Use an open, "
    "irregular apparel-print silhouette directly on flat chroma, real chroma gaps "
    "between separated elements and a clean 6-8% moat. No rectangular poster, page, "
    "card, full-bleed scene, sticker cutline, shared white backing or enclosing halo. "
    "IGNORE halftone as a style feature. HUMAN-MADE PRINT RULE: use 3-5 broad flat "
    "ink masses, decisive hand-drawn contour, a few deliberate shadow shapes and "
    "slightly imperfect screenprint registration. Faces stay natural and specific, "
    "not beautified. Surfaces stay graphic and quiet. No photorealistic rendering, "
    "no CGI, 3D bevelled objects, HDR glow, glossy fantasy concept art, hyper-sharp "
    "microtexture, pores on every face, ornate noise on every surface, generic epic "
    "armour, random sparks or swirling effect overload. The design must be readable "
    "from two metres and visibly printable with real separated inks. Never reproduce the references' white "
    "rectangles, broken extraction patches, horizontal striping, pixel smears or torn "
    "digital artefacts. Keep anatomy and instrument geometry correct. Use 3-6 strong "
    "inks and make the wordmark readable on both black and white shirts with coloured "
    "fill, dark inner keyline and warm-light outer keyline. No tiny fake tour text."
)


def _rock_band_variant_brief(slot: int) -> str:
    variant = _ROCK_BAND_VARIANTS[slot % len(_ROCK_BAND_VARIANTS)]
    return f"ASSIGNED ROCK VARIANT: {variant}. {_ROCK_BAND_COMMON_CONTRACT}"

# Пользователь выбрал визуальные направления 04, 08 и 05 именно в таком порядке.
# Ротация задаётся до вызова арт-директора, поэтому независимые Gemini-вызовы не
# сходятся к одному и тому же безопасному шаблону. Девять слотов дополнительно
# меняют баланс графики, акцентный цвет и характер типографики.
_YOUTH_MOTION_VARIANTS = (
    "04 CONTROLLED TYPE CHAOS / acid magenta — oversized condensed Cyrillic headline, staggered baselines, one crossed-out fragment and sparse registration marks; the theme-derived symbol stays secondary",
    "08 MARKER DOODLE MARGINS / signal yellow — hand-drawn Cyrillic headline with loose arrows, circles, underlines and only 3-5 small theme-derived doodles; keep the centre strong and uncluttered",
    "05 DIAGONAL MOTION / electric cyan — italic headline and one theme-derived hero object moving on a single rising diagonal with speed bars and broken trailing fragments",
    "04 CONTROLLED TYPE CHAOS / hot orange — wide grotesk headline split across unequal levels, one rotated word and compact editorial microtype; use a different subject symbol from every other slot",
    "08 MARKER DOODLE MARGINS / vivid violet — bold clean headline interrupted by dry-brush notes, brackets and hand-written emphasis; no sticker bubbles and no all-over doodle wallpaper",
    "05 DIAGONAL MOTION / acid red — compressed type stretched by perspective, two sharp direction changes and a theme-specific motion trace; preserve open chroma gaps",
    "04+08 TYPE AND MARKER HYBRID / lime accent — disciplined block lettering with one hand-written correction layer and a single symbolic sketch derived from the topic",
    "08+05 DOODLE IN MOTION / cobalt accent — loose marker headline carried by a curved motion path, with tiny arrows and impact marks that never form an enclosing badge",
    "04+05 TYPE IMPACT / warm yellow accent — giant asymmetric word pair cut by one diagonal speed axis, with a compact theme-specific object breaking the letter rhythm",
)

_YOUTH_MOTION_COMMON_CONTRACT = (
    "YOUTH MOTION COMMON CONTRACT: preserve the user's subject and invent an original, "
    "short, meaningful phrase connected to it. For a Russian subject use correctly "
    "spelled Cyrillic, never pseudo-letters. Derive the hero object and marks from the "
    "subject; DO NOT default to lightning bolts, stars, flames, skulls or city skylines. "
    "Build an open irregular apparel-print silhouette directly on chroma with visible "
    "chroma gaps and a clean 6-8% moat. Absolutely no rectangular poster, magazine "
    "cover, page, card, shared backing, sticker cutline, white halo or enclosing blob. "
    "Use 3-5 print-friendly inks. Main lettering must remain readable on both black "
    "and white shirts through a coloured or midtone fill plus dark inner keyline and "
    "warm-light outer keyline; never pure-black-only or pure-white-only type."
)


def _youth_motion_variant_brief(slot: int) -> str:
    variant = _YOUTH_MOTION_VARIANTS[slot % len(_YOUTH_MOTION_VARIANTS)]
    return f"ASSIGNED YOUTH VARIANT: {variant}. {_YOUTH_MOTION_COMMON_CONTRACT}"

# A brand-only request used to be repeated verbatim for every slot.  Each
# independent image call consequently converged on the same best-known model
# (for Mercedes this was almost always the 190E).  For the most common marques
# we assign a concrete, real hero model before calling the art director.
_AUTOMOTIVE_BRAND_CATALOGS = {
    "mercedes": (
        "Mercedes-Benz 300 SL Gullwing (W198)",
        "Mercedes-Benz C111-II",
        "Mercedes-Benz 190 E 2.5-16 Evolution II (W201)",
        "Mercedes-Benz 500 E (W124)",
        "Mercedes-Benz CLK GTR",
        "Mercedes-Benz SLR McLaren",
        "Mercedes-Benz SLS AMG Black Series",
        "Mercedes-AMG GT Black Series",
        "Mercedes-Benz 280 SL Pagoda (W113)",
        "Mercedes-Benz 450 SEL 6.9 (W116)",
        "Mercedes-Benz 560 SEC (C126)",
        "Mercedes-Benz S 600 Coupe (C140)",
        "Mercedes-Benz SL 73 AMG (R129)",
        "Mercedes-Benz E 55 AMG (W210)",
        "Mercedes-Benz C 63 AMG Black Series (C204)",
        "Mercedes-Benz CLS 55 AMG (C219)",
        "Mercedes-Benz S 65 AMG (W221)",
        "Mercedes-Benz G 63 AMG 6x6",
        "Mercedes-AMG ONE",
        "Mercedes-Benz Sauber C9",
    ),
    "bmw": (
        "BMW 2002 Turbo",
        "BMW 3.0 CSL (E9)",
        "BMW M1 (E26)",
        "BMW M3 (E30)",
        "BMW M5 (E34)",
        "BMW M5 (E39)",
        "BMW M3 CSL (E46)",
        "BMW Z8 (E52)",
        "BMW M3 GTR (E46)",
        "BMW 850CSi (E31)",
        "BMW M Coupe (E36/8)",
        "BMW M5 Touring (E61)",
        "BMW 1 Series M Coupe (E82)",
        "BMW M4 GTS (F82)",
        "BMW M2 CS (F87)",
        "BMW M5 CS (F90)",
    ),
    "porsche": (
        "Porsche 356 Speedster",
        "Porsche 911 Carrera RS 2.7",
        "Porsche 911 Turbo 3.3 (930)",
        "Porsche 959",
        "Porsche 911 GT1 Straßenversion",
        "Porsche Carrera GT",
        "Porsche 918 Spyder",
        "Porsche 911 GT3 RS (997)",
        "Porsche 911 R (991)",
        "Porsche 935/78 Moby Dick",
        "Porsche 917K",
        "Porsche 944 Turbo",
    ),
    "ferrari": (
        "Ferrari 250 GTO",
        "Ferrari 365 GTB/4 Daytona",
        "Ferrari 288 GTO",
        "Ferrari Testarossa",
        "Ferrari F40",
        "Ferrari F50",
        "Ferrari Enzo",
        "Ferrari LaFerrari",
        "Ferrari 458 Speciale",
        "Ferrari 812 Competizione",
        "Ferrari F12tdf",
        "Ferrari SF90 XX Stradale",
    ),
    "audi": (
        "Audi Sport quattro S1 E2",
        "Audi 90 quattro IMSA GTO",
        "Audi V8 quattro DTM",
        "Audi RS2 Avant",
        "Audi TT quattro Sport",
        "Audi RS4 (B5)",
        "Audi R8 V10 plus",
        "Audi R8 LMP",
        "Audi RS6 Avant (C6)",
        "Audi RS3 LMS",
    ),
    "toyota": (
        "Toyota 2000GT",
        "Toyota Celica GT-Four ST185",
        "Toyota Supra Turbo A (A70)",
        "Toyota Supra RZ (A80)",
        "Toyota AE86 Sprinter Trueno",
        "Toyota MR2 GT-S (SW20)",
        "Toyota GT-One TS020",
        "Toyota GR Yaris",
        "Toyota GR Supra",
        "Toyota Century V12",
    ),
    "nissan": (
        "Nissan Skyline 2000 GT-R (Hakosuka)",
        "Nissan Skyline GT-R (R32)",
        "Nissan Skyline GT-R V-Spec (R33)",
        "Nissan Skyline GT-R V-Spec II (R34)",
        "Nissan GT-R Nismo (R35)",
        "Nissan Fairlady Z 432",
        "Nissan 300ZX Twin Turbo (Z32)",
        "Nissan Silvia K's (S13)",
        "Nissan Silvia Spec-R (S15)",
        "Nissan R390 GT1",
    ),
    "ford": (
        "Ford GT40 Mk II",
        "Ford Escort RS1600",
        "Ford RS200",
        "Ford Sierra RS500 Cosworth",
        "Ford Mustang Boss 302",
        "Ford Mustang Shelby GT500",
        "Ford Capri RS3100",
        "Ford GT",
        "Ford Focus RS WRC",
        "Ford Falcon XY GTHO",
    ),
}

_AUTOMOTIVE_BRAND_ALIASES = {
    "mercedes": "mercedes",
    "mercedes benz": "mercedes",
    "мерседес": "mercedes",
    "мерседес бенц": "mercedes",
    "bmw": "bmw",
    "бмв": "bmw",
    "porsche": "porsche",
    "порше": "porsche",
    "ferrari": "ferrari",
    "феррари": "ferrari",
    "audi": "audi",
    "ауди": "audi",
    "toyota": "toyota",
    "тойота": "toyota",
    "nissan": "nissan",
    "ниссан": "nissan",
    "ford": "ford",
    "форд": "ford",
}

_AUTOMOTIVE_COMPOSITION_FAMILIES = (
    "ASYMMETRIC MUSEUM GRID — car low and wide, model wordmark high left, sparse facts on the opposite edge",
    "OVERSIZED VERTICAL WORDMARK — tall letters behind the car, compact facts in a narrow side rail",
    "DIAGONAL SPEED AXIS — low three-quarter car crossing italic display type, timing marks following the same angle",
    "ENGINEERING BLUEPRINT — clean exploded callouts and monospaced facts around one intact hero car, no panel background",
    "HERITAGE EMBLEM — restrained arched title, small origin line and one open crest-like line motif",
    "TRACK-LINE ARC — one circuit or route line frames the car while the main title remains horizontal",
    "SWISS EDITORIAL — generous negative space, offset car, geometric type blocks with no enclosing rectangle",
    "WIDE PANORAMIC LOCKUP — side-profile car anchors the lower half, broad letterspaced title above it",
)

_AUTOMOTIVE_TEXT_DIRECTIONS = (
    "heritage: write an original 2-5 word line about legacy, not an advertisement",
    "engineering: write an original 2-5 word line about precision or mechanical intent",
    "endurance: write an original 2-5 word line about distance, resilience or night racing",
    "motion: write an original 2-5 word line about controlled speed, without generic 'born to race' clichés",
    "origin: write an original 2-5 word line tied to country, city or design culture",
    "touring: write an original 2-5 word line about road-and-track duality",
    "icon: write an original 2-5 word line explaining why this silhouette matters",
    "driver feeling: write an original 2-5 word line about analogue connection or restraint",
)

_AUTOMOTIVE_UNKNOWN_MODEL_AXES = (
    "a heritage road or racing icon from the 1950s-1970s",
    "a homologation, rally or touring model from the 1980s-1990s",
    "a performance sedan or coupe from the 1990s-2000s",
    "a modern halo, GT or supercar from the 2010s-2020s",
    "an endurance prototype or GT competition model",
    "a lightweight roadster or compact sports coupe",
    "a rare estate, shooting brake or performance wagon",
    "a technically important flagship rather than the marque's most obvious default",
)

_AUTOMOTIVE_NEUTRAL_TYPE_CONTRACT = (
    "NEUTRAL DUAL-CONTRAST TYPE: all main and supporting lettering must use neutral "
    "silver/mid-grey or warm greige fills with a thin charcoal inner stroke AND a "
    "thin warm-ivory outer keyline, so it stays readable on both black and white "
    "T-shirts. Never use the car body colour or a saturated brand colour for the "
    "main lettering; body colour is allowed only in tiny marks."
)


def _normalize_automotive_subject(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", value.lower()).strip()


def _automotive_slot_briefs(subjects: list[str], count: int) -> list[str]:
    """Assign a different concrete model, layout and text voice to every car slot."""
    expanded = _expand_round_robin(subjects, count)
    occurrences: dict[str, int] = {}
    result: list[str] = []
    for slot, subject in enumerate(expanded):
        normalized = _normalize_automotive_subject(subject)
        occurrence = occurrences.get(normalized, 0)
        occurrences[normalized] = occurrence + 1
        brand_key = _AUTOMOTIVE_BRAND_ALIASES.get(normalized)
        catalog = _AUTOMOTIVE_BRAND_CATALOGS.get(brand_key or "")

        if catalog:
            hero = catalog[occurrence % len(catalog)]
            model_direction = (
                f"CONCRETE HERO MODEL: {hero}. Depict exactly this model; do not "
                "substitute the marque's more famous default model."
            )
        elif re.search(r"\d", normalized):
            model_direction = (
                f"CONCRETE HERO MODEL REQUESTED BY USER: {subject}. Keep this exact "
                "model; vary the art direction rather than replacing it."
            )
        else:
            axis = _AUTOMOTIVE_UNKNOWN_MODEL_AXES[occurrence % len(_AUTOMOTIVE_UNKNOWN_MODEL_AXES)]
            model_direction = (
                f"AUTOMOTIVE MARQUE/SUBJECT: {subject}. Choose one real, correctly "
                f"named model matching this assigned lane: {axis}; не повторяй модель "
                "из другого слота этой пачки и не своди запрос к самой известной модели."
            )

        composition = _AUTOMOTIVE_COMPOSITION_FAMILIES[
            slot % len(_AUTOMOTIVE_COMPOSITION_FAMILIES)
        ]
        text_direction = _AUTOMOTIVE_TEXT_DIRECTIONS[
            slot % len(_AUTOMOTIVE_TEXT_DIRECTIONS)
        ]
        result.append(
            f"{model_direction} COMPOSITION FAMILY: {composition}. "
            f"TEXT DIRECTION: {text_direction}; use a different headline, wording, "
            "font character and fact arrangement from every other slot. "
            f"{_AUTOMOTIVE_NEUTRAL_TYPE_CONTRACT}"
        )
    return result


def allows_theme_free(styles: list[str]) -> bool:
    """Пустая тема допустима, только если ВСЕ выбранные стили автономны."""
    selected = [str(style).strip() for style in (styles or []) if str(style).strip()]
    return bool(selected) and all(style in _AUTONOMOUS_STYLE_BRIEFS for style in selected)


def _autonomous_briefs(style_id: str, count: int) -> list[str]:
    briefs = _AUTONOMOUS_STYLE_BRIEFS[style_id]
    expanded = _expand_round_robin(briefs, count)
    if style_id != _AUTOMOTIVE_STYLE_ID:
        return expanded
    return [
        (
            f"{brief} COMPOSITION FAMILY: "
            f"{_AUTOMOTIVE_COMPOSITION_FAMILIES[index % len(_AUTOMOTIVE_COMPOSITION_FAMILIES)]}. "
            f"TEXT DIRECTION: "
            f"{_AUTOMOTIVE_TEXT_DIRECTIONS[index % len(_AUTOMOTIVE_TEXT_DIRECTIONS)]}. "
            f"{_AUTOMOTIVE_NEUTRAL_TYPE_CONTRACT}"
        )
        for index, brief in enumerate(expanded)
    ]


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
    style_brief: str = ""    # принудительная вариация внутри выбранного стиля


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
    elif style_list == [_ROCK_BAND_STYLE_ID] and theme:
        # Название рок-группы — самостоятельная тема, а не тайтл аниме/фильма.
        # Не запускаем franchise_scout и не превращаем найденных музыкантов в
        # отдельные несвязанные слоты. Если владелец перечислил состав вручную,
        # сохраняем его как один общий reference-блок для каждой композиции.
        band_reference = theme
        if characters:
            band_reference = (
                f"ROCK BAND: {theme}. REFERENCE LINEUP (use only when the assigned "
                f"composition needs members): {characters}"
            )
        entries = [(band_reference, "")] * count
        source = "rock_band"
    elif names and style_list == [_AUTOMOTIVE_STYLE_ID]:
        entries = [(brief, "") for brief in _automotive_slot_briefs(names, count)]
        source = "automotive_subject"
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
    style_occurrences: dict[str, int] = {}
    tasks: list[DesignTask] = []
    used_tags: set[str] = set()
    for i, (label, char_en) in enumerate(entries, start=1):
        style_id = next(style_cycle)
        style_occurrence = style_occurrences.get(style_id, 0)
        style_occurrences[style_id] = style_occurrence + 1
        if style_id == _YOUTH_MOTION_STYLE_ID:
            style_brief = _youth_motion_variant_brief(style_occurrence)
        elif style_id == _ROCK_BAND_STYLE_ID:
            style_brief = _rock_band_variant_brief(style_occurrence)
        else:
            style_brief = ""
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
            style_brief=style_brief,
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
        idea_theme = task.label
        if task.style_brief:
            idea_theme = f"{idea_theme}\n\n{task.style_brief}"
        designs = art_director.make_ideas(
            idea_theme, 1, fmt="cutout", style_pref=style_pref
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
