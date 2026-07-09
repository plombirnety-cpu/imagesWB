# -*- coding: utf-8 -*-
"""art_director.py — Claude-арт-директор для print-factory-nb.

Пишет промпты КИНЕМАТОГРАФИЧНОЙ ПРОЗОЙ (не danbooru-теги!) — так nano-banana понимает
сцену лучше, чем теговый стиль SDXL/Illustrious. Источник ТРЕБОВАНИЙ к идее —
comfyui-print-server/art_director.py::SYSTEM_DIECUT (пол персонажа явно, 2-3 канон-
приметы, полный силуэт с полями, слоган-катчфраза, kana-имя, выбор chroma green/blue,
запрет вторых существ/спутников/трансформаций) — сами промпты картинки переписаны под
prose-стиль nanobanana с нуля.

Форматы:
- cutout  — просто персонаж на хромакее (без обрамления, без слогана).
- diecut  — персонаж в обрамлении пламени/энергии, образующем внешний силуэт,
            + слоган-катчфраза снизу.

ТЕКСТ В ГЕНЕРАЦИИ (config.TEXT_RENDER, десятый заход, 2026-07-08): новые поколения
nano-banana (gemini-3.1-flash-image = Nano Banana 2) рисуют встроенный текст БЕЗ
орфографических ошибок и гармоничнее, чем кодовая типографика поверх — подтверждено
A/B оркестратора (out_batch/ab_models/). TEXT_RENDER=image (дефолт) — системный
промпт требует ВСТРОЕННУЮ типографику (см. схему `type_spec` ниже + exact-spelling
блок в build_prompt), СТАРЫЙ повсеместный запрет букв (пункт 6 ниже) заменяется на
условный: запрет остаётся ТОЛЬКО когда text_mode/text_modes_v3 реально пустые (текст
дизайну не нужен вовсе). TEXT_RENDER=code — старое поведение 1:1 (текст всегда
запрещён в артworк, накладывается кодом typography.py/typography_v3.py) — остаётся
рабочим фолбэком (в т.ч. автоматическим при провале OCR-контроля спеллинга, см.
batch_print._verify_text).
"""
import json
import random
import re
import threading
from collections import deque
from pathlib import Path

import anthropic

import config

MODEL = config.MODEL

# ── Банк утверждённых визуальных стилей (docs/STYLE_BANK.json) ─────────────────
# Каталог стилей владельца (баррокко/медальон/пропаганда/укиё-э/витраж/ар-деко/таро/
# герб/пиксель/дорожный знак/метал-обложка и т.п.) — арт-директор ВЫБИРАЕТ стиль из
# банка по mood_tags темы + ротации (см. _pick_style_candidates/_style_bank_block
# ниже), essence/text_treatment вшиваются в художественный промпт (build_prompt).
# Отсутствие файла — ОБРАТНАЯ СОВМЕСТИМОСТЬ: старое поведение без банка стилей,
# design["style_id"] будет пустой строкой, предупреждение печатается один раз.
_STYLE_BANK_PATH = Path(__file__).resolve().parent / "docs" / "STYLE_BANK.json"
_style_bank_cache = {"loaded": False, "data": None}

# Сколько последних стилей помнить для ротации (не давать один стиль дважды подряд
# в скользящем окне) — прокидывается извне (theme_scout/daily_prints) как recent_styles.
STYLE_ROTATION_WINDOW = 5

# Сколько стилей-кандидатов передавать Claude на выбор за один вызов make_ideas —
# не весь банк целиком (промпт не должен раздуваться на полсотни стилей), но
# достаточно широкий выбор с учётом ротации.
_STYLE_CANDIDATES_N = 6


class RecentStyles:
    """Потокобезопасное скользящее окно последних STYLE_ROTATION_WINDOW style_id —
    для ротации в параллельных конвейерах (daily_prints.py — ThreadPoolExecutor с
    несколькими воркерами, batch_print.py — последовательный батч тем). Каждый поток
    вызывает snapshot() ПЕРЕД make_ideas (список для recent_styles=), затем record()
    ПОСЛЕ получения design — гонки условий возможны (два потока могут одновременно
    получить один и тот же style_id в кандидатах), но это не критично: ротация —
    качественное пожелание владельца ("не давать один стиль два раза подряд"), не
    жёсткая гарантия, и при WORKERS>1 полностью строгая последовательность недостижима
    в принципе. deque(maxlen=N) сам отбрасывает старые записи."""

    def __init__(self, window: int = STYLE_ROTATION_WINDOW):
        self._window = deque(maxlen=window)
        self._lock = threading.Lock()

    def snapshot(self) -> list:
        with self._lock:
            return list(self._window)

    def record(self, style_id: str) -> None:
        if not style_id:
            return
        with self._lock:
            self._window.append(style_id)


def _load_style_bank() -> dict | None:
    """Читает docs/STYLE_BANK.json один раз за процесс (кэш в модульной переменной).
    Возвращает None при отсутствии файла/невалидном JSON — вызывающий код (make_ideas/
    build_prompt) в этом случае работает СТАРЫМ путём без банка стилей (обратная
    совместимость), печатает предупреждение ОДИН раз за процесс."""
    if _style_bank_cache["loaded"]:
        return _style_bank_cache["data"]
    _style_bank_cache["loaded"] = True
    if not _STYLE_BANK_PATH.exists():
        print(f"!! art_director: {_STYLE_BANK_PATH.name} не найден — "
              f"работаю БЕЗ банка стилей (старое поведение)", flush=True)
        return None
    try:
        data = json.loads(_STYLE_BANK_PATH.read_text(encoding="utf-8"))
        styles = data.get("styles") or []
        if not styles:
            print(f"!! art_director: {_STYLE_BANK_PATH.name} пуст (нет styles) — "
                  f"работаю БЕЗ банка стилей", flush=True)
            return None
        _style_bank_cache["data"] = data
        return data
    except Exception as e:  # noqa: BLE001 — битый JSON не должен ронять генерацию
        print(f"!! art_director: не смог прочитать {_STYLE_BANK_PATH.name}: {e} — "
              f"работаю БЕЗ банка стилей", flush=True)
        return None


def _style_by_id(style_id: str) -> dict | None:
    bank = _load_style_bank()
    if not bank:
        return None
    for s in bank["styles"]:
        if s.get("id") == style_id:
            return s
    return None


def _pick_style_candidates(theme: str, recent_styles: list = None,
                            k: int = _STYLE_CANDIDATES_N,
                            style_pref: str = None) -> list:
    """Кандидаты стилей для передачи Claude на выбор: сортировка по релевантности
    mood_tags/name_ru к теме (простой substring-скор, финальное решение всё равно
    принимает Claude — это только сужение списка) + ИСКЛЮЧЕНИЕ recent_styles
    (ротация — не давать тот же стиль в последнем скользящем окне). Если после
    исключения recent осталось МЕНЬШЕ k стилей — донабирает из исключённых (лучше
    показать Claude немного стилей из недавних, чем урезать выбор до 1-2), явно
    помечая их как "недавние" в промпте (см. _style_bank_block), чтобы Claude их
    избегал, но не был жёстко заблокирован при малом банке.

    style_pref (mega_plan_800, build_mega_plan.py) — ФОРСИРОВАННЫЙ style_id для
    ЭТОГО конкретного дизайна (напр. taro_zodiac -> "19_tarot"): если задан И
    реально существует в банке (_style_by_id) — ротация/скоринг/recent_styles
    ПОЛНОСТЬЮ ИГНОРИРУЮТСЯ, кандидатом становится ТОЛЬКО этот один стиль (Claude
    не выбирает — обязан использовать именно его, см. _style_bank_block).
    Невалидный/пустой style_pref — обычное поведение ниже, как будто параметра
    не было (обратная совместимость)."""
    if style_pref:
        forced = _style_by_id(style_pref)
        if forced:
            return [forced]
    bank = _load_style_bank()
    if not bank:
        return []
    styles = bank["styles"]
    recent = set(recent_styles or [])
    theme_low = theme.lower()

    def _score(s: dict) -> int:
        score = 0
        for tag in s.get("mood_tags", []):
            if tag.lower() in theme_low:
                score += 2
        if s.get("name_ru", "").lower() in theme_low:
            score += 1
        return score

    fresh = [s for s in styles if s.get("id") not in recent]
    stale = [s for s in styles if s.get("id") in recent]
    fresh_sorted = sorted(fresh, key=_score, reverse=True)

    # Перемешиваем верхушку с одинаковым скором (не всегда один и тот же топ-стиль
    # для похожих тем) — детерминированность тестам не нужна, make_ideas сам по себе
    # недетерминированный (вызов Claude).
    top_score = _score(fresh_sorted[0]) if fresh_sorted else 0
    top_bucket = [s for s in fresh_sorted if _score(s) == top_score]
    rest_bucket = [s for s in fresh_sorted if _score(s) != top_score]
    random.shuffle(top_bucket)
    candidates = top_bucket + rest_bucket

    if len(candidates) < k and stale:
        candidates = candidates + stale
    return candidates[:k]


def _style_bank_block(theme: str, recent_styles: list = None,
                       style_pref: str = None) -> str:
    """Текстовый блок STYLE_BANK для системного промпта — список стилей-кандидатов
    (essence/text_treatment/mood_tags/constraints) + инструкция выбрать style_id
    (и опционально style_mix, до 2 стилей, если тема реально просит совмещения).
    Пустая строка, если банка нет (make_ideas тогда не добавляет поля style_id/
    style_mix в JSON-запрос вовсе — старое поведение).

    style_pref — см. _pick_style_candidates. Если форсирован (валидный id, банк
    вернул ровно один кандидат) — блок меняет формулировку с "выбери лучший" на
    "ОБЯЗАН использовать именно этот" (mega_plan_800, style_pref-приоритет)."""
    candidates = _pick_style_candidates(theme, recent_styles, style_pref=style_pref)
    if not candidates:
        return ""
    forced = bool(style_pref and len(candidates) == 1
                  and candidates[0].get("id") == style_pref)
    if forced:
        lines = [
            "\n\nСТИЛЬ ДЛЯ ЭТОГО ДИЗАЙНА ЗАДАН ЗАРАНЕЕ (владелец форсирует конкретный "
            "стиль под эту категорию/тему) — ОБЯЗАН вернуть style_id ровно как ниже, "
            "не выбирай другой и не оставляй пустым:",
        ]
    else:
        lines = [
            "\n\nБАНК УТВЕРЖДЁННЫХ СТИЛЕЙ ВЛАДЕЛЬЦА — ОБЯЗАН выбрать style_id СТРОГО из "
            "списка ниже для каждого дизайна (не изобретай свой стиль, не оставляй "
            "пустым). Стили можно миксовать (style_mix) до 2 штук, ТОЛЬКО если тема "
            "реально просит совмещения (иначе style_mix — пустая строка):",
        ]
    for s in candidates:
        ring_note = (" [ГИБРИД: кольцо текста рисует КОД — в художественном промпте "
                      "опиши кольцо-рамку ПУСТЫМ, без единой буквы на нём]"
                      if s.get("hybrid_ring_text") else "")
        lines.append(
            f"- id=\"{s['id']}\" ({s['name_ru']}){ring_note}: {s['essence']} "
            f"ТИПОГРАФИКА СТИЛЯ: {s['text_treatment']} ПАЛИТРА: {s['palette_rule']} "
            f"ПОДХОДИТ ДЛЯ: {', '.join(s.get('mood_tags', []))}. "
            f"ОГРАНИЧЕНИЯ: {'; '.join(s.get('constraints', []))}."
        )
    if forced:
        lines.append(
            f"style_id ДОЛЖЕН быть ровно \"{style_pref}\" в твоём JSON-ответе. Впиши "
            "essence/text_treatment этого стиля В САМ художественный промпт (prompt) — "
            "опиши сцену/рамку/типографику стиля явно, как часть композиции. style_mix "
            "оставь пустым, если тема явно не просит совмещения ещё с одним стилем."
        )
    else:
        lines.append(
            "Выбери style_id САМОГО подходящего стиля по теме/mood_tags из списка выше "
            "(это уже суженный список с учётом ротации — не проси других стилей, кроме "
            "перечисленных). Впиши essence/text_treatment выбранного стиля (и второго, "
            "если style_mix) В САМ художественный промпт (prompt) — опиши сцену/рамку/"
            "типографику этого стиля явно, как часть композиции, а не отдельным полем."
        )
    return "\n".join(lines)

# ── Общие требования к идее (для обоих форматов) ───────────────────────────────

_COMMON_RULES_BASE = (
    "Ты арт-директор ПРЕМИАЛЬНЫХ принтов для футболок (топ-мерч / аниме key-visual, "
    "не скучный портрет и не сток). Пишешь ПРОМПТ ДЛЯ nano-banana (Google Gemini Image) — "
    "модель понимает КИНЕМАТОГРАФИЧНУЮ ПРОЗУ (связное описание сцены, как режиссёрская "
    "ремарка), а НЕ список danbooru-тегов через запятую. Пиши промпт цельными "
    "предложениями на английском. "
    "ОБЯЗАТЕЛЬНЫЕ требования к содержанию сцены: "
    "(1) ПОЛ персонажа указывай ЯВНО прямым текстом в первом предложении (a young man / "
    "a young woman / an adult man и т.п.) — без этого модель иногда путает пол персонажа. "
    "(2) Назови персонажа по имени и опиши РОВНО 2-3 самые узнаваемые канон-приметы "
    "(причёска, фирменная одежда/аксессуар, характерный цвет) — не больше, лишние мелкие "
    "детали (серьги, мелкий декор) модель смазывает, не строй на них композицию. Держи "
    "ВЕРНОЕ число фирменных деталей (не плоди лишнее оружие/конечности). "
    "(2а) Если у персонажа есть КАНОНИЧНОЕ фирменное оружие/атрибут (меч конкретной формы, "
    "посох, маска и т.п.) — опиши его В ПРОМПТЕ С ТОЙ ЖЕ СТРОГОСТЬЮ, что и внешность "
    "персонажа: точная форма клинка/предмета, цвет, характерные детали отделки. "
    "ТЕРМИНОЛОГИЯ ОРУЖИЯ — ИСПОЛЬЗУЙ ТОЧНЫЙ японский/каноничный термин предмета, НЕ "
    "обобщай до общего 'a sword'/'a blade'/'a weapon': katana, nodachi, zanpakuto, "
    "wakizashi, naginata, tanto, kusarigama, tessen и т.п. — какой термин каноничен ИМЕННО "
    "для этого персонажа/франшизы, такой и пиши. Например: у Кенпачи Зараки (Bleach) не "
    "'a sword'/'a machete' и не обобщённый 'nodachi' — это ИЗНОШЕННАЯ КАТАНА-ДЗАНПАКТО "
    "(zanpakuto) с зазубренной, щербатой кромкой клинка и рукоятью в потрёпанных бинтах, "
    "так и опиши: 'his zanpakuto: a battered, unusually long katana blade with a heavily "
    "notched and chipped cutting edge, hilt wrapped in worn bandages'. Если канон говорит, "
    "что клинок длинный и зазубренный, так и опиши форму, но термин предмета — точный, не "
    "обобщённый. "
    "(3) ПОЛНЫЙ СИЛУЭТ С ШИРОКИМИ ПОЛЯМИ: персонаж (и любые эффекты вокруг) ПОЛНОСТЬЮ "
    "помещается в кадр, ничего не обрезано и не упирается в край — явно опиши широкие "
    "равномерные поля хромакей-фона со всех сторон вокруг фигуры. "
    "(4) СТРОГО ОДИН субъект в кадре — никаких вторых существ, мехов, спутников или "
    "гигантских трансформаций позади персонажа; сила персонажа показывается только "
    "эффектами/аурой/энергией вокруг него, не вторым телом. "
    "(5) Стиль рендера — сочный насыщенный аниме cel-shading (яркие плотные заливки, "
    "чёткий контур, высокая насыщенность цвета), НЕ пастель и НЕ размытая живопись. "
)

# Пункт (6) — TEXT_RENDER=code (старое поведение): текст ВСЕГДА запрещён в артворк,
# накладывается кодом typography.py/typography_v3.py отдельно.
_RULE6_TEXT_CODE = (
    "(6) В КОНЦЕ промпта ЯВНО запрети текст на самой картинке: no letters, no words, "
    "no typography, no lettering, no watermarks, no signature in the artwork — весь текст "
    "накладывается кодом отдельно, диффузия текст не рисует. "
)

# Пункт (6) — TEXT_RENDER=image (дефолт, десятый заход): текст ВСТРАИВАЕТСЯ в саму
# генерацию, когда он реально нужен композиции — запрет остаётся условным (описан в
# JSON-схеме через type_spec/text_mode ниже, конкретный exact-spelling блок собирает
# build_prompt). Здесь только общее указание, что при НАЛИЧИИ текста он должен быть
# частью художественной композиции, а не наклейкой.
_RULE6_TEXT_IMAGE = (
    "(6) ТИПОГРАФИКА — ЧАСТЬ ХУДОЖЕСТВЕННОЙ КОМПОЗИЦИИ. Если по твоему выбору (см. "
    "поля text_mode/text_modes_v3/type_spec ниже) на принте есть текст — рисуй его "
    "КАК ДЕТАЛЬ АРТА (стиль леттеринга/размещение/цвета — по стайлгайду, см. "
    "type_spec), не как отдельно приклеенную табличку. Если ты решил, что тексту на "
    "ЭТОМ принте не место (text_mode='none' И text_modes_v3=[]) — тогда явно запрети "
    "буквы: no letters, no words, no typography, no lettering, no watermarks, no "
    "signature. Собственно exact-spelling инструкцию и запрет прочего текста добавит "
    "код отдельным блоком — в самом художественном промпте вставлять слова строкой в "
    "кавычках НЕ нужно, только описывай стиль/размещение через type_spec. "
)

# ЦВЕТ ФОНА-ХРОМАКЕЯ — общий хвост правила (6), не зависит от TEXT_RENDER.
_RULE6_TAIL = (
    "ЦВЕТ ФОНА-ХРОМАКЕЯ в сам художественный промпт НЕ включай (код добавит отдельным "
    "явным куском текста) — только опиши поля/пространство вокруг фигуры. "
)


def _common_rules() -> str:
    """_COMMON_RULES_BASE + пункт (6), выбранный по config.TEXT_RENDER ('image' —
    условный запрет + встроенная типографика, 'code' — старый безусловный запрет
    букв). Функция, а не константа-строка — TEXT_RENDER читается на момент КАЖДОГО
    вызова (важно для тестов, которые monkeypatch'ят config.TEXT_RENDER)."""
    rule6 = _RULE6_TEXT_CODE if config.TEXT_RENDER == "code" else _RULE6_TEXT_IMAGE
    return _COMMON_RULES_BASE + rule6 + _RULE6_TAIL

# Общий кусок JSON-схемы про типографику — Claude сам решает, нужен ли текст на принте и
# в каком виде, ПО КОМПОЗИЦИИ конкретного дизайна (не фиксированный стиль на все случаи).
_TEXT_MODE_SCHEMA = (
    "\"text_mode\":\"<none|under|punch|kana_side — выбери ПО КОМПОЗИЦИИ этого конкретного "
    "дизайна, не бери одно и то же каждый раз: 'none', если дизайн самодостаточен и текст "
    "только помешает (держи ЭТОТ вариант примерно в 20-30% дизайнов, реально выбирай его, "
    "не только формально); 'under' — стритвир-плакат, одно-два ударных слова слогана "
    "огромным кеглем ЗА фигурой (персонаж перекрывает буквы); 'punch' — короткий слоган "
    "1-3 строки с наклоном впритык к фигуре снизу; 'kana_side' — только если у персонажа "
    "есть узнаваемое японское имя, вертикальная катакана вдоль края фигуры. Выбирай "
    "осознанно под конкретную сцену>\","
)

# Типографика v3 (docs/PRINT_STYLE_GUIDE.md) — новые поля ПОВЕРХ text_mode (v2). Если
# text_modes_v3 непустой массив — типографика v3 (typography_v3.compose_text_v3)
# используется ВМЕСТО text_mode; пустой массив — старый путь v2 без изменений.
_TEXT_MODE_V3_SCHEMA = (
    "\"text_modes_v3\":\"<пустой массив [] ИЛИ подмножество из ['quote_bottom',"
    "'kanji_on','editorial','collection_footer'], согласно правилам комбинирования "
    "(docs/PRINT_STYLE_GUIDE.md раздел 3.6): 'editorial' — ВСЕГДА соло, никогда не "
    "комбинируется с другими; 'quote_bottom'+'kanji_on'+'collection_footer' — каноничная "
    "тройка (можно любым поднабором); максимум 3 визуальных текстовых блока. Включай "
    "v3-режимы примерно в 55-65% дизайнов (не в 100% — иначе конвейер снова монотонный), "
    "пустой массив [] в остальных 35-45% случаев (тогда используется text_mode v2 выше), "
    "предпочитая [] для тем без сильного персонажа-драмы (машины, бытовые концепты)>\","
    "\"quote\":\"<короткая цитата ЛАТИНИЦЕЙ, 4-10 слов, каноничная реплика персонажа или "
    "уместная авторская фраза под тему; ПУСТАЯ строка \\\"\\\", если text_modes_v3 не "
    "включает 'quote_bottom' И mood не 'pop_trash' (для pop_trash цитата уходит в подвал, "
    "см. стайлгайд раздел 4.3, даже без отдельного quote_bottom)>\","
    "\"name_jp\":\"<кандзи ИЛИ катакана имени персонажа для 'kanji_on'/подзаголовка "
    "'editorial', 2-6 знаков; ПУСТАЯ строка \\\"\\\", если не уверен в точном написании — "
    "приоритет иероглифов (кандзи) над катаканой, если у имени есть каноничное "
    "кандзи-написание (например 伏黒甚爾 для Тодзи Фусигуро), иначе катакана>\","
    "\"mood\":\"<одно слово: 'duotone_quote' | 'fashion_editorial' | 'pop_trash' — "
    "настроение, определяющее выбор палитровых ролей/шрифтовой пары/набора режимов v3 "
    "(docs/PRINT_STYLE_GUIDE.md раздел 4.2): 'duotone_quote' — мрачный/жёсткий персонаж, "
    "кровь/тени/проклятия, ограниченная 2-цветная аура; 'fashion_editorial' — статусный/"
    "элегантный/взрослый персонаж, тема моды/аристократии/сцены, спокойная палитра; "
    "'pop_trash' — агрессивный/кислотный/хоррор-комедийный персонаж, контрастные яркие "
    "эффекты, растровая/поп фактура. ПУСТАЯ строка \\\"\\\", если text_modes_v3 пустой>\","
)


def _signature_props_schema() -> str:
    return (
        "\"signature_props\":\"<короткое АНГЛИЙСКОЕ описание канон-оружия/ключевого "
        "атрибута персонажа с 2-3 опознавательными признаками формы/цвета/отделки. "
        "ОБЯЗАН содержать ТОЧНЫЙ японский/каноничный термин предмета (katana, nodachi, "
        "zanpakuto, naginata, tanto и т.п.) — НЕ обобщай до 'sword'/'blade'/'weapon' "
        "(например для Кенпачи Зараки: \\\"his zanpakuto: a battered, unusually long "
        "katana blade with a heavily notched and chipped cutting edge, hilt wrapped in "
        "worn bandages\\\", НЕ 'a nodachi'/'a machete'/'a sword'); "
        "пустая строка \\\"\\\", если у персонажа/темы нет знакового предмета>\","
    )


# type_spec (десятый заход, TEXT_RENDER=image) — англ. описание ВСТРОЕННОЙ типографики
# по правилам стайлгайда (docs/PRINT_STYLE_GUIDE.md разделы 2-3): стиль леттеринга по
# mood, размещение относительно фигуры, цвета СЛОВАМИ (не hex — генерация текста не
# умеет точный hex, но понимает "deep purple"/"bone white" и т.п.), вертикальная
# колонка кандзи name_jp, если есть. Пустая строка — тексту не место (эквивалент
# text_mode=none/text_modes_v3=[]). build_prompt() оборачивает это в exact-spelling
# инструкцию отдельно — здесь ТОЛЬКО стиль/размещение/цвета, без самих слов слогана.
#
# Одиннадцатый заход, живой баг (out_batch/20260708_211920/02, Тандзиро): Claude
# описал ДВЕ отдельные зоны размещения для ОДНОЙ и той же цитаты (quote) — "quote text
# ... tucked tightly below the figure's feet" ПЛЮС отдельно "placed tilted along the
# bottom of the composition" — модель честно нарисовала обе (мелкая копия под фигурой
# + крупная копия внизу). Явное правило "ОДНА зона размещения" ниже — предотвращает
# описание двух мест ДЛЯ ОДНОЙ ФРАЗЫ в самом type_spec, до того как это дойдёт до
# генерации картинки.
_TYPE_SPEC_SCHEMA = (
    "\"type_spec\":\"<АНГЛИЙСКОЕ описание ВСТРОЕННОЙ типографики для этого дизайна, "
    "СТИЛЬ по mood (docs/PRINT_STYLE_GUIDE.md раздел 2/4.2): mood='duotone_quote' -> "
    "bold aggressive brush-graffiti lettering; mood='fashion_editorial' -> elegant "
    "high-contrast serif display lettering (Playfair-style); mood='pop_trash' -> "
    "gothic blackletter-style lettering; без mood/для простых тем -> крепкий street "
    "capital леттеринг. ОПИШИ: (a) характер шрифта СЛОВАМИ (bold brush / elegant serif "
    "/ gothic blackletter / street caps), (b) РАЗМЕЩЕНИЕ относительно фигуры (along "
    "the bottom, tilted / behind the figure, huge scale / integrated near the chest, "
    "как уместно композиции), (c) ЦВЕТА СЛОВАМИ из палитры СЦЕНЫ (напр. 'bright "
    "blood-red with dark outline', НЕ hex-код — просто цвет тем же словом, что и в "
    "остальном промпте). ГЛАВНАЯ ФРАЗА (quote/slogan) ДОЛЖНА иметь РОВНО ОДНУ зону "
    "размещения — НЕ описывай для неё два разных места на композиции (например НЕ "
    "пиши 'tucked below the figure's feet' И ОТДЕЛЬНО 'along the bottom' для одной и "
    "той же фразы — выбери ОДНО место). Если у персонажа есть name_jp — добавь фразу "
    "про 'a vertical column of Japanese calligraphy beside the figure' (это ОТДЕЛЬНЫЙ "
    "блок кандзи, не сама фраза slogan/quote — его не считай второй зоной фразы). "
    "ПУСТАЯ строка \\\"\\\", если тексту на этом принте не место (эквивалент "
    "text_mode='none' И text_modes_v3=[])>\","
)


# Схема полей style_id/style_mix (банк стилей) — общая для cutout/diecut, вставляется
# ПЕРЕД закрывающей скобкой JSON-объекта в обоих _BODY. Пустая строка по умолчанию
# для обоих полей (обратная совместимость: если банк недоступен, _style_bank_block
# пустая и Claude эти поля не увидит в system-промпте, но JSON-схема сама по себе
# безвредна — _parse просто санирует их отдельно).
_STYLE_ID_SCHEMA = (
    "\"style_id\":\"<id стиля из БАНКА СТИЛЕЙ ниже (если блок присутствует в этом "
    "системном промпте) — ОБЯЗАН быть ровно одним из перечисленных id; пустая строка "
    "\\\"\\\", если блок банка стилей в этом промпте отсутствует>\","
    "\"style_mix\":\"<id ВТОРОГО стиля из банка, ТОЛЬКО если реально миксуешь два "
    "стиля для этого дизайна (тема явно просит совмещения); пустая строка \\\"\\\" в "
    "подавляющем большинстве случаев (один стиль — норма, микс — редкое исключение)>\","
)


def _build_system(fmt_body: str, style_block: str = "") -> str:
    """_common_rules() (динамически по TEXT_RENDER) + тело формата (cutout/diecut) +
    опциональный блок банка стилей (style_block, см. _style_bank_block) в конце —
    после JSON-схемы, чтобы не разрывать описание полей."""
    return _common_rules() + fmt_body + style_block


_CUTOUT_BODY = (
    "ФОРМАТ: cutout — просто персонаж крупным планом (в полный рост или динамичный "
    "поясной кадр) на чистом хромакей-фоне, БЕЗ декоративного обрамления и БЕЗ слогана "
    "в самой картинке. "
    "Выдай N РАЗНЫХ дизайнов. Для КАЖДОГО верни JSON-объект: "
    "{\"prompt\":\"<готовый англ. промпт-проза целиком, без цвета фона>\","
    "\"chroma\":\"<green ИЛИ blue — green по умолчанию, blue если у персонажа/одежды есть "
    "заметный зелёный цвет (фон не должен сливаться с персонажем)>\","
    "\"slogan\":\"<короткая ударная фраза ЛАТИНИЦЕЙ, ОРИЕНТИР 1-4 слова (коронная фраза "
    "персонажа сжатая до сути, или уместный короткий слоган для не-персонажной темы); "
    "длинная каноничная фраза допустима, только если реально нужна для text_mode=punch "
    "с балансной разбивкой на 2-3 строки; можно оставить как есть, даже если в cutout не "
    "используется>\","
    "\"slogan_color\":\"<red|orange|white|yellow|purple|black — контрастный цвет>\","
    "\"kana\":\"<имя персонажа катаканой, 2-8 знаков, напр. タンジロウ; если не уверен "
    "в точном написании или тема не японская — пустая строка \\\"\\\">\","
    "\"character_en\":\"<имя персонажа ЛАТИНИЦЕЙ, как в англоязычных базах данных "
    "(напр. \\\"Kenpachi Zaraki\\\"); ПУСТАЯ строка \\\"\\\", если тема НЕ про конкретного "
    "вымышленного персонажа (машина, бытовой концепт и т.п.)>\","
    "\"title_en\":\"<франшиза/тайтл персонажа ЛАТИНИЦЕЙ (напр. \\\"Bleach\\\"); ПУСТАЯ "
    "строка \\\"\\\", если character_en пустой или франшиза неизвестна>\","
    + _signature_props_schema() +
    _TEXT_MODE_SCHEMA +
    _TEXT_MODE_V3_SCHEMA +
    _TYPE_SPEC_SCHEMA +
    _STYLE_ID_SCHEMA.rstrip(",") +
    "}. "
    "Отвечай СТРОГО JSON-массивом таких объектов, без markdown и пояснений."
)

_DIECUT_BODY = (
    "ФОРМАТ: diecut — вырезной мерч-принт. Персонаж в полный рост, ПОЛНОСТЬЮ ОКРУЖЁННЫЙ "
    "стилизованным пламенем/энергией/аурой (цвет и характер эффекта — под тему персонажа: "
    "огонь, молнии, чакра, тёмная энергия и т.п.), эти эффекты плотно обрамляют фигуру со "
    "всех сторон и ОБРАЗУЮТ ВНЕШНИЙ СИЛУЭТ всей композиции (как форма постера/наклейки) — "
    "опиши это явно как часть сцены. Ниже персонажа и эффектов должно остаться пустое "
    "пространство хромакей-фона для слогана. "
    "Выдай N РАЗНЫХ дизайнов. Для КАЖДОГО верни JSON-объект: "
    "{\"prompt\":\"<готовый англ. промпт-проза целиком, без цвета фона>\","
    "\"chroma\":\"<green ИЛИ blue — green по умолчанию, blue если у персонажа/пламени есть "
    "заметный зелёный цвет>\","
    "\"slogan\":\"<слоган-катчфраза персонажа ЛАТИНИЦЕЙ, ОРИЕНТИР 1-4 слова ударной сути "
    "(Кенпачи -> LETS PARTY, Гоку -> BEYOND LIMITS); длинная каноничная фраза (Кенпачи -> "
    "COME ON LETS PARTY, Гоку -> PUSH BEYOND LIMITS) допустима, только если реально нужна "
    "для text_mode=punch с балансной разбивкой на 2-3 строки; для не-персонажной темы "
    "(напр. машина) — короткая уместная фраза по теме; БЕЗ кириллицы>\","
    "\"slogan_color\":\"<red|orange|white|yellow|purple|black — контрастный низу дизайна>\","
    "\"kana\":\"<имя персонажа катаканой, 2-8 знаков, напр. タンジロウ; если не уверен в "
    "точном написании или тема не японская — пустая строка \\\"\\\">\","
    "\"character_en\":\"<имя персонажа ЛАТИНИЦЕЙ, как в англоязычных базах данных "
    "(напр. \\\"Kenpachi Zaraki\\\"); ПУСТАЯ строка \\\"\\\", если тема НЕ про конкретного "
    "вымышленного персонажа (машина, бытовой концепт и т.п.)>\","
    "\"title_en\":\"<франшиза/тайтл персонажа ЛАТИНИЦЕЙ (напр. \\\"Bleach\\\"); ПУСТАЯ "
    "строка \\\"\\\", если character_en пустой или франшиза неизвестна>\","
    + _signature_props_schema() +
    _TEXT_MODE_SCHEMA +
    _TEXT_MODE_V3_SCHEMA +
    _TYPE_SPEC_SCHEMA +
    _STYLE_ID_SCHEMA.rstrip(",") +
    "}. "
    "Отвечай СТРОГО JSON-массивом таких объектов, без markdown и пояснений."
)


def system_cutout(theme: str = "", recent_styles: list = None,
                   style_pref: str = None) -> str:
    """SYSTEM_CUTOUT как функция — _common_rules() читает config.TEXT_RENDER на
    момент КАЖДОГО вызова (важно для тестов, monkeypatch'ящих config.TEXT_RENDER).
    theme/recent_styles (опционально) — добавляют блок БАНКА СТИЛЕЙ (см.
    _style_bank_block); без темы (дефолт "") блок стилей не добавляется — сохраняет
    обратную совместимость константы SYSTEM_CUTOUT ниже (снимок без стилей).
    style_pref — форсированный style_id (mega_plan_800), см. _style_bank_block."""
    return _build_system(_CUTOUT_BODY,
                          _style_bank_block(theme, recent_styles, style_pref))


def system_diecut(theme: str = "", recent_styles: list = None,
                   style_pref: str = None) -> str:
    """SYSTEM_DIECUT как функция — см. system_cutout()."""
    return _build_system(_DIECUT_BODY,
                          _style_bank_block(theme, recent_styles, style_pref))


# SYSTEM_CUTOUT/SYSTEM_DIECUT — обратная совместимость (модули/тесты, читающие эти
# константы напрямую): снимок на момент ИМПОРТА модуля (TEXT_RENDER из .env на старте
# процесса, БЕЗ блока банка стилей — theme="" отключает _style_bank_block). Живой путь
# (_ask_claude ниже) использует функции выше С темой/recent_styles, не эти константы.
SYSTEM_CUTOUT = system_cutout()
SYSTEM_DIECUT = system_diecut()

_SYSTEMS_FN = {"cutout": system_cutout, "diecut": system_diecut}


def _ask_claude(theme: str, n: int, fmt: str, recent_styles: list = None,
                 style_pref: str = None) -> str:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    user = (f"Запрос: {theme}. Дай ровно {n} разных дизайн(ов). JSON-массив из {n} "
            f"объектов {{\"prompt\":..., \"chroma\":..., \"slogan\":..., "
            f"\"slogan_color\":..., \"kana\":..., \"character_en\":..., \"title_en\":..., "
            f"\"signature_props\":..., \"text_mode\":..., \"text_modes_v3\":..., "
            f"\"quote\":..., \"name_jp\":..., \"mood\":..., \"type_spec\":..., "
            f"\"style_id\":..., \"style_mix\":...}}.")
    system_fn = _SYSTEMS_FN.get(fmt, system_cutout)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=system_fn(theme, recent_styles, style_pref),
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def make_ideas(theme: str, n: int, fmt: str = "cutout", recent_styles: list = None,
               style_pref: str = None) -> list:
    """N дизайнов: список dict {prompt, chroma, slogan, slogan_color, kana, character_en,
    title_en, signature_props, text_mode, text_modes_v3, quote, name_jp, mood, type_spec,
    style_id, style_mix}.

    recent_styles (опционально) — скользящее окно последних использованных style_id
    (см. STYLE_ROTATION_WINDOW) — прокидывается в системный промпт как "не давай эти
    id снова" (см. _pick_style_candidates). Если docs/STYLE_BANK.json отсутствует —
    признак игнорируется, style_id/style_mix возвращаются пустыми строками (старое
    поведение без банка стилей).

    style_pref (mega_plan_800/build_mega_plan.py, опционально) — ФОРСИРОВАННЫЙ
    style_id для ВСЕХ n дизайнов этого вызова (напр. "19_tarot" для taro_zodiac).
    Если валиден (существует в docs/STYLE_BANK.json) — recent_styles/скоринг для
    выбора стиля полностью игнорируются (см. _pick_style_candidates), системный
    промпт требует у Claude вернуть ИМЕННО этот style_id, И ДОПОЛНИТЕЛЬНО код
    форсирует design["style_id"] = style_pref на каждом возвращённом дизайне ПОСЛЕ
    парсинга — двойная гарантия (промпт может не сработать, но код никогда не
    отдаст style_pref-заказ с другим/пустым style_id). None (дефолт) — старое
    поведение, авторотация банка как раньше.

    НЕ откатываемся тихо на сырую тему при сбое парсинга JSON: 1 ретрай запроса,
    затем ЯВНАЯ ошибка (вызывающий код пропускает этот дизайн с сообщением).
    """
    text = _ask_claude(theme, n, fmt, recent_styles, style_pref)
    designs = _parse(text)
    if not designs:
        # 1 ретрай — вдруг разовый сбой формата.
        text = _ask_claude(theme, n, fmt, recent_styles, style_pref)
        designs = _parse(text)
    if not designs:
        raise RuntimeError(f"арт-директор не смог собрать дизайн для {theme!r} "
                           f"(невалидный JSON от Claude дважды подряд)")
    if len(designs) < n:
        designs = (designs * n)[:n]
    designs = designs[:n]
    if style_pref and _style_by_id(style_pref):
        for d in designs:
            d["style_id"] = style_pref
    return designs


# Катакана (゠-ヿ) + знак долготы ー + разделитель имён ・ + пробел — допустимые символы
# поля "kana" (имя персонажа для diecut/kana-стиля типографики).
_KANA_RE = re.compile(r"^[゠-ヿー・ ]+$")
_COLORS = ("red", "orange", "white", "yellow", "purple", "black")

# text_mode — режимы типографики v2 (typography.compose_text), Claude выбирает под
# конкретную композицию дизайна; дефолт "punch" при отсутствии/невалидном значении в
# ответе (обратная совместимость со старым JSON без этого поля — старые дампы читаются
# как "punch", ближе всего к прежнему поведению v1, а не падают и не теряют текст молча,
# см. комментарий у _parse ниже).
TEXT_MODES = ("none", "under", "punch", "kana_side")

# text_modes_v3 — режимы типографики v3 (typography_v3.compose_text_v3). Список
# продублирован (не импортируем typography_v3 сюда — art_director не должен тянуть
# palette/PIL-тяжёлую логику типографики, та же причина, по которой typography.py уже
# не импортирует art_director, зависимости идут только в одну сторону художник -> текст).
TEXT_MODES_V3 = ("quote_bottom", "kanji_on", "editorial", "collection_footer")

# name_jp допускает И катакану (゠-ヿ), И диапазон кандзи (CJK Unified Ideographs
# 一-鿿, раздел 4.1 стайлгайда), плюс знак долготы/разделитель/пробел — шире, чем
# _KANA_RE, который остаётся только катаканой для обратной совместимости "kana".
_NAME_JP_RE = re.compile(r"^[゠-ヿー・ 一-鿿]+$")

_MOODS = ("duotone_quote", "fashion_editorial", "pop_trash")


def _parse(text: str) -> list:
    """Парсит JSON-массив дизайнов; применяет код-предохранитель по цвету хромакея
    и санацию слогана/каны. При сбое парсинга возвращает []."""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for x in data:
        if not (isinstance(x, dict) and str(x.get("prompt", "")).strip()):
            continue
        prompt = str(x["prompt"]).strip()
        chroma = str(x.get("chroma") or "").strip().lower()
        chroma = chroma if chroma in ("green", "blue") else "green"

        # Код-предохранитель: если в промпте/идее встречается "green"/"зелён" (регистро-
        # независимо) — форсим blue, даже если Claude выбрал green. Не доверяем LLM
        # вслепую: фон не должен сливаться с зелёными элементами персонажа.
        has_green = bool(re.search(r"\bgreen\b|зелён|зелен|emerald", prompt, re.I))
        if chroma == "green" and has_green:
            chroma = "blue"

        slogan = re.sub(r"[^A-Za-z0-9 !?'\-]", "", str(x.get("slogan") or "")).strip()[:34]
        scolor = str(x.get("slogan_color") or "").strip().lower()
        scolor = scolor if scolor in _COLORS else "orange"
        kana = str(x.get("kana") or "").strip()
        if not (2 <= len(kana) <= 8 and _KANA_RE.match(kana)):
            kana = ""

        # character_en/title_en: имя персонажа и франшизы ЛАТИНИЦЕЙ (для character_ref.py —
        # поиск каноничного референс-портрета на Jikan/AniList). Дефолт "" (обратная
        # совместимость со старым JSON без этих полей — Claude их просто не пришлёт).
        character_en = re.sub(r"[^A-Za-z0-9 .'\-]", "",
                              str(x.get("character_en") or "")).strip()[:60]
        title_en = re.sub(r"[^A-Za-z0-9 .:!'\-]", "",
                          str(x.get("title_en") or "")).strip()[:60]

        # signature_props: канон-оружие/атрибут персонажа, та же санация, что у
        # character_en (латиница/цифры/базовая пунктуация), до 200 симв. Дефолт "" —
        # обратная совместимость со старым JSON без этого поля.
        signature_props = re.sub(r"[^A-Za-z0-9 .,'\-]", "",
                                 str(x.get("signature_props") or "")).strip()[:200]

        # text_mode: режим типографики v2 (typography.compose_text). Дефолт "punch" при
        # отсутствии/невалидном значении — ближе всего к прежнему поведению (anton-стиль
        # слогана впритык к фигуре), не "none", чтобы старые дампы без поля не теряли
        # текст молча.
        text_mode = str(x.get("text_mode") or "").strip().lower()
        text_mode = text_mode if text_mode in TEXT_MODES else "punch"

        # text_modes_v3: массив режимов типографики v3 (docs/PRINT_STYLE_GUIDE.md раздел
        # 3.6/4.1). Дефолт [] при отсутствии/невалидном значении — обратная совместимость
        # со старым JSON без этого поля (typography_v3 просто не вызывается, остаётся
        # text_mode v2). Защитный код (антиправило 7): если editorial пришёл ВМЕСТЕ с
        # другими режимами — editorial приоритизируется, остальные отбрасываются (не
        # падаем, но и не рисуем всё сразу).
        raw_modes = x.get("text_modes_v3")
        text_modes_v3 = []
        if isinstance(raw_modes, list):
            text_modes_v3 = [str(m).strip().lower() for m in raw_modes
                             if str(m).strip().lower() in TEXT_MODES_V3]
            # Дедуп, сохраняя порядок появления.
            seen = set()
            text_modes_v3 = [m for m in text_modes_v3
                             if not (m in seen or seen.add(m))]
            if "editorial" in text_modes_v3:
                text_modes_v3 = ["editorial"]

        # quote: короткая каноничная реплика персонажа для quote_bottom/pop_trash-
        # подвала (раздел 3.1/4.3) — системный промпт ЯВНО разрешает quote на языке
        # персонажа (не только английском), санация НЕ должна быть ASCII-only как у
        # slogan/character_en (те — служебные латинские поля для поиска референса,
        # quote — реальный видимый текст на принте). РЕГРЕСС (нашёл тестировщик, живая
        # партия 2026-07-09, out_batch/daily_2026-07-09/0005_by_индия___портрет_певиц):
        # старый regex `[^A-Za-z0-9 !?'\-]` пропускал только голый ASCII и молча вырезал
        # акцентированную латиницу (например испанское 'género' -> 'gnero'), портя
        # exact-spelling инструкцию генерации и эталон OCR-сравнения. Санация теперь
        # убирает только управляющие символы (\x00-\x1f, \x7f) и опасные для промпта
        # символы разметки/кавычек (двойные кавычки, обратные слэши, переводы строк
        # уже покрыты диапазоном control), сохраняя ЛЮБЫЕ печатные буквы Unicode
        # (акцентированная латиница, кириллица, кандзи и т.п. — язык персонажа решает
        # Claude, код не сужает алфавит). Длина не изменена — до 70 символов (антиправило
        # 4 стайлгайда: не ужимать кегль ниже порога ради длинной строки, вместо этого
        # код переносит на 2-ю строку/использует другой размер).
        quote = re.sub(r"[\x00-\x1f\x7f\"\\]", "",
                       str(x.get("quote") or "")).strip()[:70]

        # name_jp: кандзи ИЛИ катакана имени персонажа (раздел 4.1) — санация допускает
        # ОБА диапазона (в отличие от kana выше, только катакана). Дефолт "" при
        # отсутствии/невалидном значении.
        name_jp = str(x.get("name_jp") or "").strip()
        if not (2 <= len(name_jp) <= 6 and _NAME_JP_RE.match(name_jp)):
            name_jp = ""

        # mood: настроение, определяющее палитровые роли/шрифтовую пару/набор режимов
        # v3 (раздел 4.2). Дефолт "" при отсутствии/невалидном значении — пустая строка
        # ЯВНО означает "v3 не применяется по mood" (typography_v3 сам обрабатывает
        # пустой mood как дефолт duotone_quote-подобный набор ролей, см. _mood_font_pair).
        mood = str(x.get("mood") or "").strip().lower()
        mood = mood if mood in _MOODS else ""

        # type_spec: англ. описание ВСТРОЕННОЙ типографики (TEXT_RENDER=image, десятый
        # заход) — характер шрифта/размещение/цвета словами, БЕЗ самих слов слогана
        # (build_prompt добавляет exact-spelling блок отдельно). Санация мягче, чем у
        # slogan/quote (это описательная английская проза, не короткая фраза) — просто
        # ограничение длины и запрет управляющих символов, до 400 симв. Дефолт "" —
        # обратная совместимость со старым JSON без этого поля (build_prompt тогда не
        # добавляет текст-блок вообще, как раньше).
        type_spec = re.sub(r"[\r\n\t]+", " ", str(x.get("type_spec") or "")).strip()[:400]

        # style_id/style_mix: банк утверждённых стилей (docs/STYLE_BANK.json). Дефолт
        # "" при отсутствии/невалидном id — обратная совместимость (банк не загружен
        # ИЛИ Claude вернул id, которого нет в банке — не доверяем LLM вслепую, тот же
        # принцип, что у chroma-предохранителя выше). style_mix санируется тем же
        # способом, но ДОПОЛНИТЕЛЬНО не может совпадать со style_id (миксовать стиль
        # сам с собой бессмысленно — если совпали, style_mix сбрасывается в "").
        style_id = str(x.get("style_id") or "").strip()
        if not _style_by_id(style_id):
            style_id = ""
        style_mix = str(x.get("style_mix") or "").strip()
        if not _style_by_id(style_mix) or style_mix == style_id:
            style_mix = ""

        out.append({"prompt": prompt, "chroma": chroma,
                    "slogan": slogan, "slogan_color": scolor, "kana": kana,
                    "character_en": character_en, "title_en": title_en,
                    "signature_props": signature_props, "text_mode": text_mode,
                    "text_modes_v3": text_modes_v3, "quote": quote,
                    "name_jp": name_jp, "mood": mood, "type_spec": type_spec,
                    "style_id": style_id, "style_mix": style_mix})
    return out


# ── Финальный промпт картинки (идея + хромакей-фон + анти-текст-хвост) ─────────

def _chroma_bg(color: str) -> str:
    """Явный кусок промпта про фон-хромакей нужного цвета — nano-banana честно рисует
    ровный насыщенный хромакей, если просить ПРЯМО с hex-значением (граблю см. GOTCHAS).

    Одиннадцатый заход (2026-07-08): живой кейс (out_batch/20260708_205007/01) — модель
    нарисовала БЕЛЫЙ фон вместо хромакея (стикер-рамку) и пустила запрошенный зелёный
    ВНУТРЬ дизайна как ауру вокруг фигуры. Два усиления: (а) явный запрет белой
    подложки/стикер-рамки вокруг дизайна — фон должен быть хромакеем от края до края,
    не окантовкой; (б) явный запрет использовать сам цвет хромакея КАК ЦВЕТ ВНУТРИ
    артворка (никаких зелёных/синих свечений/аур/акцентов при соответствующем фоне) —
    иначе QC-гейт (batch_print._border_chroma_coverage) не поможет вырезке: если
    хромакей-цвет есть и на фоне, и в самом дизайне, colour-key неизбежно вырезает его
    из дизайна тоже.

    Живой РЕГРЕСС того же семейства дефектов (2026-07-08, out_batch/20260708_211920/02,
    Тандзиро, ПОСЛЕ усиления выше) — попиксельно подтверждена ТОЛСТАЯ чисто белая
    полоса, обрамляющая ВНЕШНИЙ КОНТУР водно-огненной ауры вокруг фигуры (не рамка
    кадра — та честно осталась синим хромакеем, coverage=1.00; это декоративная белая
    обводка САМОГО силуэта эффектов внутри дизайна, "sticker outline"/die-cut stroke).
    Старая формулировка запрещала только "border"/"mount"/"frame around the artwork"
    (интерпретируется как рамка/подложка КАДРА целиком) — не называла явно запрет
    именно обводки ВОКРУГ силуэта персонажа/эффектов внутри самой композиции. Третье
    усиление ниже называет этот конкретный паттерн прямо (die-cut sticker outline
    hugging the silhouette of the character or the flame/energy effects).

    Живой РЕГРЕСС #3 (2026-07-09, прод-job 9295bf9c, item 1, style 28_metal_cover) —
    рамка кадра НЕ была белой (та запрещена выше), но essence/text_treatment стиля
    из STYLE_BANK.json явно просят "jagged lightning-bolt streaks... toward the frame
    corners", "thorned-spike ornament... frame the lower edge" и "thin engraved
    metallic border frames the whole canvas" — ЛЮБОГО цвета декор, физически
    дотянувшийся до истинного края холста. chroma_remove._border_key() сэмплирует
    медиану внешней полосы кадра как эталон хромакея; на живом raw этой полосы ~65-70%
    оказалось decor-пикселями (метал/чёрные шипы), а не чистым хромакеем — эталон
    съехал с чистого blue (0,71,255) в грязный (19,82,152). От этого смещённого
    эталона gunmetal-silver текст (колонка катаканы того же стиля) оказался
    ХРОМАТИЧЕСКИ близко к "ключу" (dist<52) и был вырезан в прозрачность до контуров
    (border coverage=0.70 < 0.90 — QC-гейт поймал брак, но батч всё равно принял
    "лучшую попытку" с предупреждением вместо провала). Старый запрет ниже называл
    только WHITE border/mount/sticker-outline — четвёртое усиление обобщает запрет на
    декор ЛЮБОГО цвета (не только белый) у истинного края/углов кадра и требует явный
    чистый хромакей-отступ по периметру ПЕРЕД началом любой декоративной графики."""
    color = color if color in ("green", "blue") else "green"
    hexv = {"green": "0 177 64", "blue": "0 71 255"}[color]
    return (f"The entire background behind the subject is a solid, perfectly uniform "
            f"bright {color} chroma-key screen, RGB {hexv}, like a professional film "
            f"{color}-screen studio backdrop — one single flat tone filling the whole "
            f"frame around the character, edge to edge, corner to corner, no gradient, "
            f"no texture, no grain, no shadow or vignette on the backdrop, no white "
            f"border, no white sticker-style mount or frame around the artwork — the "
            f"chroma-key colour itself must reach every edge of the image. The chroma "
            f"background color must appear ONLY as the flat background, never inside "
            f"the artwork (no glow, aura, outline or accent in that color) — do not use "
            f"{color} anywhere on the character, clothing, weapon or effects, to avoid "
            f"blending with the background during keying. Do NOT draw a white sticker-"
            f"style die-cut outline or stroke hugging the silhouette of the character "
            f"or the flame/energy/aura effects around them — the edge where the "
            f"character and effects meet the {color} background must be a direct clean "
            f"line straight into the chroma-key color, with no white or light-colored "
            f"ring, halo outline, or contour stroke of any kind separating them. "
            f"IMPORTANT — clean chroma margin: even if the requested art style calls "
            f"for a decorative border, frame, engraved line, lightning-bolt streaks, "
            f"thorned spikes, rays, or any other ornament reaching toward the edges or "
            f"corners of the canvas, that decoration (of ANY colour — metallic, silver, "
            f"gold, black, or otherwise, not just white) must stop and stay clearly "
            f"INSET from the true outer edge of the image, leaving an untouched, "
            f"perfectly flat {color} chroma-key margin at least 3% of the shorter side "
            f"wide running all the way around the full perimeter of the canvas, with "
            f"nothing but the flat chroma colour touching the literal edges and corners "
            f"of the frame.")


# Явный запрет любых букв в артворк — используется build_prompt, когда дизайну
# текст НЕ нужен вовсе (design['type_spec'] пусто, эквивалент text_mode='none' +
# text_modes_v3=[]), даже при TEXT_RENDER=image. Идентичен старому пункту (6)
# _RULE6_TEXT_CODE — тот же список запретов, чтобы не рисовать случайные буквы, когда
# ни один режим типографики не выбран.
_NO_TEXT_TAIL = (
    "No letters, no words, no typography, no lettering, no watermarks, no signature "
    "anywhere in the artwork."
)


def _exact_spelling_phrase(design: dict) -> str:
    """Какую фразу требовать exact-spelling в тексте-блоке (TEXT_RENDER=image):
    приоритет 'quote' (типографика v3, quote_bottom/pop_trash-цитата — она обычно
    длиннее и содержательнее), иначе 'slogan' (v2 путь). Пустая строка, если ни того,
    ни другого нет — тогда текст-блок не собирается вовсе (см. build_prompt)."""
    quote = str(design.get("quote") or "").strip()
    if quote:
        return quote
    return str(design.get("slogan") or "").strip()


def _text_render_block(design: dict) -> str:
    """TEXT_RENDER=image: собирает блок промпта со ВСТРОЕННОЙ типографикой — точный
    приём из эталонного A/B-промпта оркестратора (out_batch/ab_models/ab_models_text.py):
    type_spec (стиль/размещение/цвета) + EXACT-SPELLING инструкция построчно с фразой
    + 'No other text anywhere'. Возвращает пустую строку, если типографика этому
    дизайну не нужна (design['type_spec'] пусто ИЛИ нет фразы для exact-spelling) —
    вызывающий код (build_prompt) в этом случае добавляет _NO_TEXT_TAIL вместо этого.

    Одиннадцатый заход (2026-07-08): живой кейс (out_batch/20260708_205007) — модель
    рисовала фразу ДВАЖДЫ (крупно + мелкий дубль, дизайн 02) и слепляла соседние слова
    без видимого пробела ("BORN TO" читалось как "BORNTO", дизайн 03). Два усиления
    инструкции: (а) явный запрет дубликатов фразы где-либо ещё на дизайне; (б) явное
    требование видимого зазора между словами, буквы соседних слов не должны касаться/
    сливаться.

    Живой РЕГРЕСС того же дефекта (2026-07-08, out_batch/20260708_211920/02, Тандзиро,
    ПОСЛЕ первого усиления выше) — модель снова нарисовала фразу дважды: мелкая белая
    копия сразу под фигурой ("тизер"/echo рядом с силуэтом) + крупная красная копия в
    нижней полосе. Правило "no duplicates elsewhere" оказалось недостаточно КОНКРЕТНЫМ
    — модель явно не расценивала маленькую копию у фигуры как "дубликат" в том же
    смысле. Третье усиление ниже называет этот конкретный паттерн прямо: явно
    запрещает именно "маленькое эхо/тизер фразы рядом с фигурой + отдельная крупная
    копия внизу" как named anti-pattern, плюс закрывает лазейку "другая формулировка
    того же смысла" (модель не должна перефразировать фразу как обходной путь)."""
    type_spec = str(design.get("type_spec") or "").strip()
    phrase = _exact_spelling_phrase(design)
    if not type_spec or not phrase:
        return ""
    name_jp = str(design.get("name_jp") or design.get("kana") or "").strip()
    type_spec_sentence = type_spec if type_spec.endswith((".", "!", "?")) else type_spec + "."
    parts = [
        f"The design INCLUDES integrated typography as part of the artwork: "
        f"{type_spec_sentence}",
        f"Spell the phrase EXACTLY, letter by letter: {phrase}.",
        "Write the phrase exactly ONCE — no duplicates elsewhere, no repeated or "
        "smaller copy of the same text anywhere else in the artwork.",
        "Specifically: do NOT add a small echo or teaser copy of the phrase near the "
        "figure in addition to the main lettering placement — there is only ONE "
        "lettering placement for this phrase on the entire composition, nowhere else, "
        "not even in a smaller size or different color. Do not rephrase or repeat the "
        "same meaning in different words either — the phrase appears as this exact "
        "wording, one single time, period.",
        "Leave clear, visible spacing between words — words must never touch or "
        "merge together; each word stays legible as a separate cluster of letters.",
    ]
    if name_jp:
        parts.append(
            f"Also include a vertical Japanese calligraphy column with the kanji "
            f"{name_jp} placed beside the figure."
        )
    parts.append("No other text anywhere.")
    return " ".join(parts)


def _style_bank_prompt_block(design: dict) -> str:
    """Код-предохранитель: essence/text_treatment выбранного style_id (и style_mix,
    если есть) ЯВНО дописываются в финальный промпт — не полагаемся только на то, что
    Claude сам вписал стиль в design['prompt'] по инструкции _style_bank_block (LLM
    иногда забывает деталь стайлгайда). hybrid_ring_text=true (09_ring_medallion) —
    ДОПОЛНИТЕЛЬНО явно требует ПУСТОЕ кольцо без единой буквы (текст на кольце рисует
    typography/typography_v3 кодом ПОСЛЕ генерации, не сама модель картинки — см.
    docs/STYLE_BANK.json). Пустая строка, если style_id пуст/неизвестен (банк
    недоступен ИЛИ Claude не выбрал стиль) — обратная совместимость."""
    style_id = str(design.get("style_id") or "").strip()
    if not style_id:
        return ""
    style = _style_by_id(style_id)
    if not style:
        return ""
    parts = [f"VISUAL STYLE ({style['name_ru']}): {style['essence']} "
             f"TYPOGRAPHY STYLE: {style['text_treatment']}"]
    if style.get("hybrid_ring_text"):
        parts.append(
            "IMPORTANT: the decorative ring/medallion border must be drawn "
            "COMPLETELY PLAIN — a plain metal/enamel band with only small "
            "ornamental notches or a single emblem glyph at the seam, with "
            "ABSOLUTELY NO LETTERS, NO WORDS, NO TEXT of any kind on the ring "
            "itself. Any name/phrase for the ring is added separately afterward "
            "by a different process, not by you."
        )
    mix_id = str(design.get("style_mix") or "").strip()
    mix_style = _style_by_id(mix_id) if mix_id else None
    if mix_style:
        parts.append(
            f"MIXED WITH A SECOND STYLE ({mix_style['name_ru']}): "
            f"{mix_style['essence']} Blend the two styles into one coherent "
            f"composition, do not just place them side by side."
        )
        if mix_style.get("hybrid_ring_text"):
            parts.append(
                "IMPORTANT: if this second style's ring/medallion border is part "
                "of the mixed composition, it must also be drawn COMPLETELY "
                "PLAIN with no letters on it, same rule as above."
            )
    return " ".join(parts)


def build_prompt(design: dict) -> str:
    """Идея (dict из make_ideas) -> финальный промпт для generate_image.

    Если design['signature_props'] непусто — вставляется явное предложение, что
    фирменное оружие/атрибут персонажа должно совпадать с каноном ТОЧНО (форма/цвет/
    отделка), а не обобщаться до "a sword" — вставляется ПЕРЕД хромакей-хвостом, сразу
    после художественного промпта, где Claude уже описал сцену.

    Если design['style_id'] непусто (банк стилей, docs/STYLE_BANK.json) — essence/
    text_treatment выбранного стиля (и style_mix, если задан) дописываются кодом
    ПОСЛЕ художественного промпта Claude (см. _style_bank_prompt_block) — код-
    предохранитель поверх инструкции в системном промпте, не полагаемся только на
    добросовестность LLM. hybrid_ring_text-стили (кольцо-медальон) получают явный
    запрет рисовать буквы на самом кольце — эту типографику накладывает код позже.

    TEXT_RENDER=image (десятый заход, дефолт): если у дизайна есть type_spec и фраза
    для exact-spelling (quote ИЛИ slogan) — вставляется блок ВСТРОЕННОЙ типографики
    (см. _text_render_block); иначе — старый безусловный запрет букв (_NO_TEXT_TAIL),
    ровно как раньше, дизайну текст не нужен. TEXT_RENDER=code — ВСЕГДА запрет букв
    (текст накладывается кодом typography.py/typography_v3.py после генерации, как в
    девятом заходе и раньше) — text_mode/type_spec для самой генерации не участвуют."""
    parts = [design["prompt"]]
    signature_props = str(design.get("signature_props") or "").strip()
    if signature_props:
        parts.append(
            f"The character's signature weapon/prop must match canon exactly: "
            f"{signature_props}."
        )

    style_block = _style_bank_prompt_block(design)
    if style_block:
        parts.append(style_block)

    if config.TEXT_RENDER == "image":
        text_block = _text_render_block(design)
        parts.append(text_block if text_block else _NO_TEXT_TAIL)
    else:
        parts.append(_NO_TEXT_TAIL)

    parts.append(_chroma_bg(design["chroma"]))
    return " ".join(parts)
