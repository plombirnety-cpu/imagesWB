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
            + слоган-катчфраза снизу (наносится typography.py КОДОМ).
"""
import json
import re

import anthropic

import config

MODEL = config.MODEL

# ── Общие требования к идее (для обоих форматов) ───────────────────────────────

_COMMON_RULES = (
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
    "(3) ПОЛНЫЙ СИЛУЭТ С ШИРОКИМИ ПОЛЯМИ: персонаж (и любые эффекты вокруг) ПОЛНОСТЬЮ "
    "помещается в кадр, ничего не обрезано и не упирается в край — явно опиши широкие "
    "равномерные поля хромакей-фона со всех сторон вокруг фигуры. "
    "(4) СТРОГО ОДИН субъект в кадре — никаких вторых существ, мехов, спутников или "
    "гигантских трансформаций позади персонажа; сила персонажа показывается только "
    "эффектами/аурой/энергией вокруг него, не вторым телом. "
    "(5) Стиль рендера — сочный насыщенный аниме cel-shading (яркие плотные заливки, "
    "чёткий контур, высокая насыщенность цвета), НЕ пастель и НЕ размытая живопись. "
    "(6) В КОНЦЕ промпта ЯВНО запрети текст на самой картинке: no letters, no words, "
    "no typography, no lettering, no watermarks, no signature in the artwork — весь текст "
    "накладывается кодом отдельно, диффузия текст не рисует. "
    "ЦВЕТ ФОНА-ХРОМАКЕЯ в сам художественный промпт НЕ включай (код добавит отдельным "
    "явным куском текста) — только опиши поля/пространство вокруг фигуры. "
)

SYSTEM_CUTOUT = (
    _COMMON_RULES +
    "ФОРМАТ: cutout — просто персонаж крупным планом (в полный рост или динамичный "
    "поясной кадр) на чистом хромакей-фоне, БЕЗ декоративного обрамления и БЕЗ слогана "
    "в самой картинке. "
    "Выдай N РАЗНЫХ дизайнов. Для КАЖДОГО верни JSON-объект: "
    "{\"prompt\":\"<готовый англ. промпт-проза целиком, без цвета фона>\","
    "\"chroma\":\"<green ИЛИ blue — green по умолчанию, blue если у персонажа/одежды есть "
    "заметный зелёный цвет (фон не должен сливаться с персонажем)>\","
    "\"slogan\":\"<короткая уместная фраза ЛАТИНИЦЕЙ 8-32 символа — коронная фраза "
    "персонажа, или уместный слоган для не-персонажной темы; можно оставить как есть, "
    "даже если в cutout не используется>\","
    "\"slogan_color\":\"<red|orange|white|yellow|purple|black — контрастный цвет>\","
    "\"kana\":\"<имя персонажа катаканой, 2-8 знаков, напр. タンジロウ; если не уверен "
    "в точном написании или тема не японская — пустая строка \\\"\\\">\","
    "\"character_en\":\"<имя персонажа ЛАТИНИЦЕЙ, как в англоязычных базах данных "
    "(напр. \\\"Kenpachi Zaraki\\\"); ПУСТАЯ строка \\\"\\\", если тема НЕ про конкретного "
    "вымышленного персонажа (машина, бытовой концепт и т.п.)>\","
    "\"title_en\":\"<франшиза/тайтл персонажа ЛАТИНИЦЕЙ (напр. \\\"Bleach\\\"); ПУСТАЯ "
    "строка \\\"\\\", если character_en пустой или франшиза неизвестна>\"}. "
    "Отвечай СТРОГО JSON-массивом таких объектов, без markdown и пояснений."
)

SYSTEM_DIECUT = (
    _COMMON_RULES +
    "ФОРМАТ: diecut — вырезной мерч-принт. Персонаж в полный рост, ПОЛНОСТЬЮ ОКРУЖЁННЫЙ "
    "стилизованным пламенем/энергией/аурой (цвет и характер эффекта — под тему персонажа: "
    "огонь, молнии, чакра, тёмная энергия и т.п.), эти эффекты плотно обрамляют фигуру со "
    "всех сторон и ОБРАЗУЮТ ВНЕШНИЙ СИЛУЭТ всей композиции (как форма постера/наклейки) — "
    "опиши это явно как часть сцены. Ниже персонажа и эффектов должно остаться пустое "
    "пространство хромакей-фона для слогана (его наносит код после генерации, не рисуй "
    "текст). "
    "Выдай N РАЗНЫХ дизайнов. Для КАЖДОГО верни JSON-объект: "
    "{\"prompt\":\"<готовый англ. промпт-проза целиком, без цвета фона>\","
    "\"chroma\":\"<green ИЛИ blue — green по умолчанию, blue если у персонажа/пламени есть "
    "заметный зелёный цвет>\","
    "\"slogan\":\"<слоган-катчфраза персонажа ЛАТИНИЦЕЙ, 8-32 символа — каноничная коронная "
    "фраза или её суть (Кенпачи -> COME ON LETS PARTY, Гоку -> PUSH BEYOND LIMITS); для "
    "не-персонажной темы (напр. машина) — короткая уместная фраза по теме; БЕЗ кириллицы>\","
    "\"slogan_color\":\"<red|orange|white|yellow|purple|black — контрастный низу дизайна>\","
    "\"kana\":\"<имя персонажа катаканой, 2-8 знаков, напр. タンジロウ; если не уверен в "
    "точном написании или тема не японская — пустая строка \\\"\\\">\","
    "\"character_en\":\"<имя персонажа ЛАТИНИЦЕЙ, как в англоязычных базах данных "
    "(напр. \\\"Kenpachi Zaraki\\\"); ПУСТАЯ строка \\\"\\\", если тема НЕ про конкретного "
    "вымышленного персонажа (машина, бытовой концепт и т.п.)>\","
    "\"title_en\":\"<франшиза/тайтл персонажа ЛАТИНИЦЕЙ (напр. \\\"Bleach\\\"); ПУСТАЯ "
    "строка \\\"\\\", если character_en пустой или франшиза неизвестна>\"}. "
    "Отвечай СТРОГО JSON-массивом таких объектов, без markdown и пояснений."
)

_SYSTEMS = {"cutout": SYSTEM_CUTOUT, "diecut": SYSTEM_DIECUT}


def _ask_claude(theme: str, n: int, fmt: str) -> str:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    user = (f"Запрос: {theme}. Дай ровно {n} разных дизайн(ов). JSON-массив из {n} "
            f"объектов {{\"prompt\":..., \"chroma\":..., \"slogan\":..., "
            f"\"slogan_color\":..., \"kana\":..., \"character_en\":..., \"title_en\":...}}.")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=_SYSTEMS.get(fmt, SYSTEM_CUTOUT),
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def make_ideas(theme: str, n: int, fmt: str = "cutout") -> list:
    """N дизайнов: список dict {prompt, chroma, slogan, slogan_color, kana}.

    НЕ откатываемся тихо на сырую тему при сбое парсинга JSON: 1 ретрай запроса,
    затем ЯВНАЯ ошибка (вызывающий код пропускает этот дизайн с сообщением).
    """
    text = _ask_claude(theme, n, fmt)
    designs = _parse(text)
    if not designs:
        text = _ask_claude(theme, n, fmt)  # 1 ретрай — вдруг разовый сбой формата
        designs = _parse(text)
    if not designs:
        raise RuntimeError(f"арт-директор не смог собрать дизайн для {theme!r} "
                           f"(невалидный JSON от Claude дважды подряд)")
    if len(designs) < n:
        designs = (designs * n)[:n]
    return designs[:n]


# Катакана (゠-ヿ) + знак долготы ー + разделитель имён ・ + пробел — допустимые символы
# поля "kana" (имя персонажа для diecut/kana-стиля типографики).
_KANA_RE = re.compile(r"^[゠-ヿー・ ]+$")
_COLORS = ("red", "orange", "white", "yellow", "purple", "black")


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

        out.append({"prompt": prompt, "chroma": chroma,
                    "slogan": slogan, "slogan_color": scolor, "kana": kana,
                    "character_en": character_en, "title_en": title_en})
    return out


# ── Финальный промпт картинки (идея + хромакей-фон + анти-текст-хвост) ─────────

def _chroma_bg(color: str) -> str:
    """Явный кусок промпта про фон-хромакей нужного цвета — nano-banana честно рисует
    ровный насыщенный хромакей, если просить ПРЯМО с hex-значением (граблю см. GOTCHAS)."""
    color = color if color in ("green", "blue") else "green"
    hexv = {"green": "0 177 64", "blue": "0 71 255"}[color]
    return (f"The entire background behind the subject is a solid, perfectly uniform "
            f"bright {color} chroma-key screen, RGB {hexv}, like a professional film "
            f"{color}-screen studio backdrop — one single flat tone filling the whole "
            f"frame around the character, no gradient, no texture, no grain, no shadow "
            f"or vignette on the backdrop, no {color} glow or rim light bleeding onto the "
            f"subject.")


def build_prompt(design: dict) -> str:
    """Идея (dict из make_ideas) -> финальный промпт для generate_image."""
    return f"{design['prompt']} {_chroma_bg(design['chroma'])}"
