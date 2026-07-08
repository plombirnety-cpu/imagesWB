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
    "(6) В КОНЦЕ промпта ЯВНО запрети текст на самой картинке: no letters, no words, "
    "no typography, no lettering, no watermarks, no signature in the artwork — весь текст "
    "накладывается кодом отдельно, диффузия текст не рисует. "
    "ЦВЕТ ФОНА-ХРОМАКЕЯ в сам художественный промпт НЕ включай (код добавит отдельным "
    "явным куском текста) — только опиши поля/пространство вокруг фигуры. "
)

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

SYSTEM_CUTOUT = (
    _COMMON_RULES +
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
    _TEXT_MODE_V3_SCHEMA.rstrip(",") +
    "}. "
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
    _TEXT_MODE_V3_SCHEMA.rstrip(",") +
    "}. "
    "Отвечай СТРОГО JSON-массивом таких объектов, без markdown и пояснений."
)

_SYSTEMS = {"cutout": SYSTEM_CUTOUT, "diecut": SYSTEM_DIECUT}


def _ask_claude(theme: str, n: int, fmt: str) -> str:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    user = (f"Запрос: {theme}. Дай ровно {n} разных дизайн(ов). JSON-массив из {n} "
            f"объектов {{\"prompt\":..., \"chroma\":..., \"slogan\":..., "
            f"\"slogan_color\":..., \"kana\":..., \"character_en\":..., \"title_en\":..., "
            f"\"signature_props\":..., \"text_mode\":..., \"text_modes_v3\":..., "
            f"\"quote\":..., \"name_jp\":..., \"mood\":...}}.")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=_SYSTEMS.get(fmt, SYSTEM_CUTOUT),
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def make_ideas(theme: str, n: int, fmt: str = "cutout") -> list:
    """N дизайнов: список dict {prompt, chroma, slogan, slogan_color, kana, character_en,
    title_en, signature_props, text_mode, text_modes_v3, quote, name_jp, mood}.

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

        # quote: короткая цитата в кавычках для quote_bottom/pop_trash-подвала (раздел
        # 3.1/4.3). Санация той же схемой, что slogan, но длиннее (до 70 символов —
        # антиправило 4 стайлгайда: не ужимать кегль ниже порога ради длинной строки,
        # вместо этого код переносит на 2-ю строку/использует другой размер).
        quote = re.sub(r"[^A-Za-z0-9 !?'\-]", "", str(x.get("quote") or "")).strip()[:70]

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

        out.append({"prompt": prompt, "chroma": chroma,
                    "slogan": slogan, "slogan_color": scolor, "kana": kana,
                    "character_en": character_en, "title_en": title_en,
                    "signature_props": signature_props, "text_mode": text_mode,
                    "text_modes_v3": text_modes_v3, "quote": quote,
                    "name_jp": name_jp, "mood": mood})
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
    """Идея (dict из make_ideas) -> финальный промпт для generate_image.

    Если design['signature_props'] непусто — вставляется явное предложение, что
    фирменное оружие/атрибут персонажа должно совпадать с каноном ТОЧНО (форма/цвет/
    отделка), а не обобщаться до "a sword" — вставляется ПЕРЕД хромакей-хвостом, сразу
    после художественного промпта, где Claude уже описал сцену."""
    parts = [design["prompt"]]
    signature_props = str(design.get("signature_props") or "").strip()
    if signature_props:
        parts.append(
            f"The character's signature weapon/prop must match canon exactly: "
            f"{signature_props}."
        )
    parts.append(_chroma_bg(design["chroma"]))
    return " ".join(parts)
