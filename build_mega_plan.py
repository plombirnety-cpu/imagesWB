#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_mega_plan.py — строит mega_plan_800.json: план на 800 принтов для
mega_batch_run.py, СТРОГО по раскладке владельца (см. передаточную записку
разработчика/докстринг ниже) — не тематизатор Claude, а детерминированный
Python-генератор плана: персонажи/моменты аниме-франшиз взяты из измеренных досье
franchise_scout.py (out_batch/analytics_2026-07-09/franchise_dossiers/*.json,
поле print_moment/why — вес favourites/favorites), не-аниме ниши — из ресёрча
двух отчётов сканирования (out_batch/analytics_2026-07-09/SCALING_REPORT.md,
out_batch/analytics_nonanime_2026-07-09/NONANIME_SCALING_REPORT.md).

Раскладка (сумма ПРОВЕРЯЕТСЯ кодом в main(), падает при расхождении с 800):

  АНИМЕ-ЯДРО 545: one_piece 105, naruto 90, chainsaw_man 90, jujutsu_kaisen 60,
    re_zero 105, detective_conan 45, mushoku_tensei 50.
  НИШЕВЫЕ АНИМЕ-СТАВКИ 25: ghost_in_the_shell 8, sakamoto_days 8, slime 5, jojo_p7 4.
  НЕ-АНИМЕ 230: drift_jdm 40, taro_zodiac 40, meme_animals 40, professions 30,
    fishing_hunting 25, gym 25, rock_aesthetic 15, squid_game 15.
  ИТОГО 545 + 25 + 230 = 800.

Каждая запись плана — {seq, category, theme, format, style_pref, filename_base}:
  - category: путь ОТНОСИТЕЛЬНО D:\\800\\, слэш-разделённый ("anime/one_piece",
    "nonanime/taro_zodiac") — mega_batch_run.py разбивает по "/" и создаёт
    вложенные папки (mega_batch_run.OUTROOT / *category.split("/")).
  - theme: РУССКИЙ текст для art_director.make_ideas (Claude сам пишет английский
    промпт-прозу из этого описания — конвенция всего проекта, см. theme_scout.py/
    daily_prints.py, art_director.make_ideas принимает тему на любом языке).
  - format: "diecut"|"cutout", детерминированный микс ~75%/25% по глобальному seq
    (см. _FORMAT_CUTOUT_EVERY), не рандом — воспроизводимость плана.
  - style_pref: None (авторотация банка docs/STYLE_BANK.json, см. art_director.py
    style_pref-приоритет) ИЛИ форсированный style_id из банка. Форсировано для
    taro_zodiac (чередование "19_tarot"/"09_ring_medallion" по всей теме) и части
    profession/gym-записей (26_warning_sign/22_heraldry/28_metal_cover — по смыслу
    профессии, см. _PROFESSION_STYLE/_GYM_STYLE_CYCLE ниже).
  - filename_base: осмысленный транслит без кириллицы, "<subject>_<variant>_<NN>"
    (напр. "luffy_gear5_storm_01", "zodiac_leo_tarot_01").

Комбинаторика тем (см. _variant_items): у каждого субъекта (персонаж/машина/
знак/профессия) — короткий рукописный список канон-форм/сцен (variants, англ.
транслит в slug + русский текст в тему, взятый из print_moment/why досье там, где
применимо, и из общих знаний о франшизе там, где досье не хватает на нужный объём).
Тема = "{субъект} — {вариант}, {универсальный модификатор}" — вариант выбирается
round-robin (i % len(variants)), модификатор — на цикл ПОСЛЕ прохода всех вариантов
((i // len(variants)) % len(UNIVERSAL_MODIFIERS)), поэтому пары (вариант,
модификатор) НЕ повторяются, пока count <= len(variants) * len(UNIVERSAL_MODIFIERS)
(проверено с запасом на всех бакетах: минимум 3 варианта * 24 модификатора = 72,
максимальный count в плане — 40).

Запуск:
    cd print-factory-nb && python build_mega_plan.py
    python build_mega_plan.py --out mega_plan_800.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

TOTAL_EXPECTED = 800

# ── Универсальный банк модификаторов сцены (ракурс/свет/настроение) — одинаково ─
# уместен и для аниме-персонажей, и для машин/животных/профессий (см. докстринг
# модуля "Комбинаторика тем"). 24 фразы — с запасом покрывает максимальный count
# бакета (40) даже при минимуме 2 вариантов субъекта (2*24=48 > 40).
UNIVERSAL_MODIFIERS = [
    "крупным планом, драматичное освещение",
    "в динамичном ракурсе три четверти",
    "на фоне закатного неба",
    "в атмосфере ночного города с неоновыми огнями",
    "в момент максимального напряжения действия",
    "спокойная уверенная поза",
    "с эффектным контровым светом",
    "силуэтом на ярком фоне",
    "под дождём, отражения на поверхности",
    "в снежную погоду, морозный воздух",
    "с частицами и искрами вокруг",
    "в культовой узнаваемой позе",
    "крупным символичным жестом",
    "на фоне символа своей темы",
    "в момент триумфа",
    "с драматичной тенью на половину фигуры",
    "в ярком контрастном студийном свете",
    "среди дыма и тумана",
    "с лёгкой уверенной усмешкой",
    "в героический полный рост",
    "на фоне звёздного неба",
    "в винтажной ретро-эстетике",
    "с сильным цветовым акцентом",
    "в спокойной созерцательной сцене",
]

_FORMAT_CUTOUT_EVERY = 4  # каждая 4-я запись (по финальному глобальному seq) — cutout


def _variant_items(category: str, subject_slug: str, subject_ru: str,
                    variants: list, count: int, style_pref=None) -> list:
    """count записей для ОДНОГО субъекта (персонаж/машина/животное/знак/профессия) —
    см. докстринг модуля "Комбинаторика тем". variants — список (form_ru, form_slug).
    style_pref — либо None, либо строка (форсируется на ВСЕ count записей этого
    субъекта), либо список строк (чередуется по индексу — taro_zodiac/gym)."""
    n_mod = len(UNIVERSAL_MODIFIERS)
    items = []
    for i in range(count):
        form_ru, form_slug = variants[i % len(variants)]
        mod_ru = UNIVERSAL_MODIFIERS[(i // len(variants)) % n_mod]
        theme = f"{subject_ru} — {form_ru}, {mod_ru}"
        filename_base = f"{subject_slug}_{form_slug}_{i + 1:02d}"
        if isinstance(style_pref, list):
            sp = style_pref[i % len(style_pref)]
        else:
            sp = style_pref
        items.append({"category": category, "theme": theme,
                      "style_pref": sp, "filename_base": filename_base})
    return items


def _fixed_items(category: str, entries: list) -> list:
    """Рукописные "roster"-бакеты (один субъект = один пункт, не round-robin) —
    капсула one_piece (5 самостоятельных сюжетов), старшие арканы таро (7 карт).
    entries: список (theme_ru, filename_base)."""
    return [{"category": category, "theme": t, "style_pref": None, "filename_base": fb}
            for t, fb in entries]


# ═══════════════════════════════════════════════════════════════════════════════
# АНИМЕ-ЯДРО 545 — персонажи/моменты взвешены по favourites/favorites из досье
# franchise_scout.py (out_batch/analytics_2026-07-09/franchise_dossiers/*.json)
# ═══════════════════════════════════════════════════════════════════════════════

def _build_one_piece() -> list:
    cat = "anime/one_piece"
    out = []
    out += _variant_items(cat, "luffy", "Манки Д. Луффи", [
        ("Гир 5 — смеющаяся форма, белые волосы, эпизод 1100", "gear5_awakening"),
        ("Гир 5 против Кайдо, молниеносный удар кулаком", "gear5_vs_kaido"),
        ("Гир 4 Боундмен, татуированное тело, барабанный ритм", "gear4_boundman"),
        ("Гир 2 — розовая от пара кожа, скоростной рывок", "gear2_speed"),
        ("Гир 3 — гигантский кулак Gomu Gomu no Elephant Gun", "gear3_giant_fist"),
        ("соломенная шляпа на фоне восходящего солнца, символ Джой Боя", "strawhat_sunrise"),
    ], 30)
    out += _variant_items(cat, "zoro", "Ророноа Зоро", [
        ("три меча, техника Асура — три лица шесть рук", "asura_three_faces"),
        ("одноглазая повязка снята, момент Короля Конкистадора", "eye_reveal_conquerors"),
        ("рассекает воздух техникой Санторю, зелёная аура клинков", "santoryu_slash"),
        ("медитация с тремя мечами перед боем, тёмный фон додзё", "meditation_swords"),
        ("шрам через глаз, решительный взгляд перед схваткой", "scar_determination"),
    ], 25)
    out += _variant_items(cat, "sanji", "Винсмок Санджи", [
        ("невозмутимое лицо, момент 'didn't give away the plan'", "didnt_give_away_plan"),
        ("огненная нога, техника Дьябль Жамб в прыжке", "diable_jambe_kick"),
        ("элегантная поза с сигаретой, чёрный костюм повара-бойца", "suit_cigarette_pose"),
    ], 15)
    out += _variant_items(cat, "law", "Трафальгар Ло", [
        ("жёлтая подводная лодка, синий ореол сферы Room", "room_submarine"),
        ("операция ROOM — синяя сфера рассекает пространство", "room_ability"),
    ], 12)
    out += _variant_items(cat, "robin", "Нико Робин", [
        ("цветы-руки техники Mil Fleur расцветают, тёмный плащ, улыбка", "mil_fleur_bloom"),
    ], 10)
    out += _variant_items(cat, "shanks", "Шанкс", [
        ("поднятый меч, шрам через глаз, плащ на ветру — момент 'aura'", "aura_unmatched"),
    ], 8)
    out += _fixed_items(cat, [
        ("Нами вызывает молнию техникой Клима-Такт, буря вокруг", "nami_climatact_01"),
        ("Портгас Д. Эйс с огненными крыльями Хикен, татуировка ASCE", "ace_hiken_wings_01"),
        ("Рокс Д. Ксебек — силуэт с тёмной аурой, манга-стиль легенды прошлого", "rocks_xebec_legend_01"),
        ("Эдвард Ньюгейт (Белоус) с бисентой на вершине Марафорда, трещины в воздухе", "whitebeard_marineford_01"),
        ("Пираты Соломенной шляпы, обложка свежей главы манги, эпичная битва", "manga_current_arc_01"),
    ])
    return out


def _build_naruto() -> list:
    cat = "anime/naruto"
    out = []
    out += _variant_items(cat, "itachi", "Итачи Утиха", [
        ("активация Мангекё Шарингана, силуэт Сусано'о за спиной", "mangekyou_susanoo"),
        ("вороны разлетаются от плаща Акацуки", "akatsuki_crows"),
        ("Цукиёми — красный мир иллюзий, вращающееся небо", "tsukuyomi_realm"),
        ("прощальный взгляд перед братом Саске, дождь", "farewell_rain_brother"),
        ("Аматэрасу — чёрное неугасимое пламя из глаза", "amaterasu_black_flame"),
    ], 30)
    out += _variant_items(cat, "kakashi", "Какаши Хатаке", [
        ("открывает повязку — Шаринган светится, Чидори заряжена в руке", "sharingan_chidori"),
        ("Камуи — искажение пространства вокруг противника", "kamui_warp"),
    ], 20)
    out += _variant_items(cat, "naruto_pain", "Наруто против Пейна", [
        ("форма Девятихвостого, оранжевое чакра-пламя рвётся наружу", "kyuubi_vs_pain"),
        ("Шесть Путей Пейна, пронзающий взгляд Ринненгана", "pain_tendo_rinnegan"),
        ("Наруто разбивает тела Пейна голыми кулаками", "naruto_smash_pain"),
        ("Пейн призывает Чибаку Тэнсэй — гравитационная сфера над Конохой", "pain_chibaku_tensei"),
        ("Наруто в Режиме Мудреца, глаза с поперечным зрачком", "naruto_sage_mode_pain"),
        ("Нагато, истинный Пейн, в инвалидном кресле, корни чакры вокруг", "nagato_true_pain"),
    ], 15)
    out += _variant_items(cat, "sasuke", "Саске Утиха", [
        ("активирует Сусано'о — меч Тоцука, рёбра синего чакра-пламени", "susanoo_totsuka"),
        ("Чидори пронзает тьму, Шаринган горит красным", "chidori_dark"),
    ], 15)
    out += _variant_items(cat, "shikamaru", "Шикамару Нара", [
        ("тень растянута по земле, техника Тень-Имитация сковывает врага", "shadow_bind"),
    ], 10)
    return out


def _build_chainsaw_man() -> list:
    cat = "anime/chainsaw_man"
    out = []
    out += _variant_items(cat, "reze", "Резе", [
        ("Дьявол Бомбы, взрыв за спиной, нежная обманчивая улыбка", "bomb_devil_smile"),
        ("Резе с ножом на первом свидании, скрытая угроза во взгляде", "date_scene_knife"),
        ("постер арки Резе, дождливый ночной Токио — премьера фильма в прокате", "reze_arc_poster"),
    ], 25)
    out += _variant_items(cat, "denji", "Дэндзи", [
        ("трансформация в Бензопильного Человека, кровавый силуэт на тёмном фоне", "chainsaw_transform"),
        ("Дэндзи с Почитой, простая детская мечта в глазах", "denji_pochita_dream"),
    ], 20)
    out += _variant_items(cat, "power", "Пауэр", [
        ("боевой молот из крови против Катаны Мэна", "blood_hammer_katana"),
        ("дерзкая ухмылка, кровавые рога дьявола крови", "blood_devil_grin"),
    ], 20)
    out += _variant_items(cat, "makima", "Макима", [
        ("цепь Контроля, подчиняющий взгляд алых глаз", "control_chain_gaze"),
    ], 15)
    out += _variant_items(cat, "aki", "Аки Хаякава", [
        ("Пистолетный Дьявол в финальной битве, снег и пустой взгляд", "gun_devil_snow"),
    ], 10)
    return out


def _build_jujutsu_kaisen() -> list:
    cat = "anime/jujutsu_kaisen"
    out = []
    out += _variant_items(cat, "gojo", "Сатору Годжо", [
        ("Gojo Returns — выход из Бесконечной Пустоты, сезон 3", "gojo_returns"),
        ("Domain Expansion — расширение Безграничной Пустоты", "unlimited_void"),
        ("Hollow Purple — синяя и красная энергия сливаются в фиолетовый взрыв", "hollow_purple"),
    ], 20)
    out += _variant_items(cat, "itadori", "Юдзи Итадори", [
        ("Black Flash против Сукуны, момент пробуждения", "black_flash_awaken"),
    ], 12)
    out += _variant_items(cat, "sukuna", "Рёмэн Сукуна", [
        ("четырёхрукая форма, шокирован возвращением Годжо", "four_arms_shocked"),
    ], 10)
    out += _variant_items(cat, "megumi", "Мегуми Фусигуро", [
        ("призыв Восьминогой собаки, юная форма с тенями", "shikigami_dog"),
    ], 8)
    out += _variant_items(cat, "nanami", "Кенто Нанами", [
        ("удар 7:3 Ratio, костюм с галстуком, финальная схватка", "ratio_technique"),
    ], 6)
    out += _variant_items(cat, "todo", "Аой Тодо", [
        ("техника Boogie Woogie, обмен позициями в бою", "boogie_woogie"),
    ], 4)
    return out


def _build_re_zero() -> list:
    cat = "anime/re_zero"
    out = []
    out += _variant_items(cat, "rem", "Рем", [
        ("боевая форма они-демона, разбитый рог, арка Бруталии", "oni_form_brutalia"),
        ("«я буду любить тебя снова и снова» — нежный решающий момент", "love_again_moment"),
        ("ледяной боевой молот демона, синие волосы развеваются", "ice_hammer_battle"),
        ("сон в саду цветов, спокойная тёплая улыбка", "flower_garden_dream"),
        ("горничная особняка Розвааля, уверенный прямой взгляд", "maid_uniform_pose"),
    ], 40)
    out += _variant_items(cat, "emilia", "Эмилия", [
        ("ледяная магия арки Священного леса Элиор, парящие кристаллы льда", "ice_magic_elior"),
        ("полу-эльфийка, серебряные волосы на ветру", "silver_hair_wind"),
    ], 25)
    out += _variant_items(cat, "subaru", "Субару Нацуки", [
        ("арка «Возвращение к нулю», крест-накрест шрамы, Return by Death", "return_by_death"),
        ("решительный взгляд перед новой петлёй времени", "loop_determination"),
    ], 20)
    out += _variant_items(cat, "beatrice", "Беатриче", [
        ("библиотека Ройяль с открытой книгой, магический щит Кор Леони", "cor_leonis_shield"),
    ], 10)
    out += _variant_items(cat, "ram", "Рам", [
        ("боевая стойка с ветровой магией, особняк Розвааля", "wind_magic_stance"),
    ], 10)
    return out


def _build_detective_conan() -> list:
    cat = "anime/detective_conan"
    out = []
    out += _variant_items(cat, "conan", "Эдогава Конан / Кудо Синъити", [
        ("снимает очки, трансформация в Кудо Синъити, двойной облик спиной к спине", "double_identity"),
        ("галстук-голосовой синтезатор активирован, момент раскрытия дела", "bowtie_reveal"),
    ], 15)
    out += _variant_items(cat, "kaito_kid", "Кайто Куроба / Кайто Кид", [
        ("белый плащ на фоне полной луны, монокль блестит, карта в руке", "moonlight_heist"),
    ], 12)
    out += _variant_items(cat, "haibara", "Хайбара Аи", [
        ("момент трансформации обратно в Сихо Миано, взрослый силуэт проступает", "shiho_transform"),
    ], 8)
    out += _variant_items(cat, "akai", "Акаи Сюити", [
        ("снайпер на крыше, красная нить через прицел", "sniper_rooftop"),
    ], 6)
    out += _variant_items(cat, "heiji", "Хаттори Хэйдзи", [
        ("деревянный меч кендо, закат над Осакой", "kendo_osaka_sunset"),
    ], 4)
    return out


def _build_mushoku_tensei() -> list:
    cat = "anime/mushoku_tensei"
    out = []
    out += _variant_items(cat, "roxy", "Рокси Мигурдия", [
        ("дорожный плащ, посох учителя магии воды, момент первого урока", "water_mage_teacher"),
    ], 15)
    out += _variant_items(cat, "rudeus", "Рудеус Грейрат", [
        ("магический доспех, заряженное пятиуровневое заклинание, битва в лабиринте", "battle_mage_armor"),
    ], 12)
    out += _variant_items(cat, "eris", "Эрис Бореас Грейрат", [
        ("двуручный меч, боевая стойка возвращения в сезоне 3", "sword_stance_return"),
    ], 10)
    out += _variant_items(cat, "sylphiette", "Сильфиетта", [
        ("зелёные волосы после трансформации, момент воссоединения", "green_hair_reunion"),
    ], 8)
    out += _variant_items(cat, "ruijerd", "Руйерд Супердиа", [
        ("зелёный третий глаз, копьё наперевес, защита в Бездне", "third_eye_spear"),
    ], 5)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# НИШЕВЫЕ АНИМЕ-СТАВКИ 25
# ═══════════════════════════════════════════════════════════════════════════════

def _build_niche_anime() -> list:
    out = []
    out += _variant_items("anime/ghost_in_the_shell", "motoko", "Мотоко Кусанаги", [
        ("оптический камуфляж исчезает, кибернетическое тело проступает", "optical_camo_reveal"),
        ("прыжок с крыши мегаполиса, дождливый неоновый Нью-Порт-Сити", "rooftop_leap_neon"),
        ("погружение в киберпространство, синий цифровой поток данных", "cyberspace_dive"),
    ], 8)
    out += _variant_items("anime/sakamoto_days", "sakamoto", "Таро Сакамото", [
        ("бывший легендарный убийца за прилавком магазина, спокойный взгляд", "shop_counter_calm"),
        ("молниеносный бросок бытовых предметов вместо оружия, экшн-сцена", "everyday_object_throw"),
        ("постер к фильму (в прокате 23.07) — контраст фартука и смертоносной точности", "movie_poster_contrast"),
    ], 8)
    out += _variant_items("anime/slime", "rimuru", "Римуру Темпест", [
        ("форма демона-повелителя, синие волосы, спокойная властная поза", "demon_lord_form"),
        ("слизень — момент нового рождения, простая добрая улыбка", "slime_origin_smile"),
    ], 5)
    out += _variant_items("anime/jojo_p7", "steelball", "Джонни Джостар и Джайро Цеппели", [
        ("верхом на лошадях, гонка Steel Ball Run через пустыню", "steel_ball_run_race"),
        ("вращающийся стальной шар, техника Spin в полёте", "spin_technique"),
    ], 4)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# НЕ-АНИМЕ 230
# ═══════════════════════════════════════════════════════════════════════════════

_CAR_SCENES = [
    ("дрифт-занос в облаке дыма на горном перевале тогэ (峠) ночью", "touge_drift"),
    ("статичный портрет на закате, JDM-стикербомбинг на кузове", "sunset_sticker_portrait"),
    ("тандем-дрифт двух силуэтов бок о бок в повороте", "tandem_drift"),
    ("крупный план на капот и фары в неоновом гараже", "garage_closeup"),
    ("ночная доставка по горному серпантину в стиле Initial D", "mountain_delivery"),
    ("вид сзади три четверти со шлейфом резинового дыма", "rear_smoke_trail"),
    ("гоночная трасса, боковой занос на пределе сцепления шин", "track_slide"),
    ("ночная стоянка JDM-встречи, подсветка дисков", "night_meet_glow"),
]


def _build_drift_jdm() -> list:
    cat = "nonanime/drift_jdm"
    out = []
    out += _variant_items(cat, "ae86", "Toyota AE86 Trueno", _CAR_SCENES, 8)
    out += _variant_items(cat, "supra", "Toyota Supra MK4", _CAR_SCENES, 7)
    out += _variant_items(cat, "gtr", "Nissan Skyline GT-R R34", _CAR_SCENES, 7)
    out += _variant_items(cat, "rx7", "Mazda RX-7 FD", _CAR_SCENES, 6)
    out += _variant_items(cat, "s15", "Nissan Silvia S15", _CAR_SCENES, 6)
    out += _variant_items(cat, "generic", "Дрифт-культура", [
        ("силуэт безымянного дрифт-купе, обобщённый JDM-профиль", "generic_touge_silhouette"),
        ("группа дрифтующих машин на тогэ, ночная гонка команды", "touge_team_pack"),
    ], 6)
    return out


_ZODIAC_SIGNS_3X = [
    ("Овен", "aries"), ("Телец", "taurus"), ("Близнецы", "gemini"), ("Лев", "leo"),
    ("Скорпион", "scorpio"), ("Стрелец", "sagittarius"), ("Козерог", "capricorn"),
    ("Водолей", "aquarius"), ("Рыбы", "pisces"),
]
_ZODIAC_SIGNS_2X = [("Рак", "cancer"), ("Дева", "virgo"), ("Весы", "libra")]

_ZODIAC_FORMS = [
    ("знак зодиака в медальоне-гербе, символ созвездия в центре", "medallion_sigil"),
    ("знак зодиака в карте таро, мистический орнамент рамки", "tarot_card_frame"),
    ("аллегорическая фигура знака зодиака среди звёзд", "starfield_figure"),
]

_MAJOR_ARCANA = [
    ("Старший Аркан «Шут» — беспечный странник на краю обрыва", "arcana_fool_01"),
    ("Старший Аркан «Маг» — фигура с поднятым жезлом над алтарём", "arcana_magician_01"),
    ("Старший Аркан «Верховная Жрица» — таинственная хранительница завесы", "arcana_high_priestess_01"),
    ("Старший Аркан «Колесо Фортуны» — вращающееся мистическое колесо судьбы", "arcana_wheel_of_fortune_01"),
    ("Старший Аркан «Смерть» — фигура с косой, символ перемен и обновления", "arcana_death_01"),
    ("Старший Аркан «Звезда» — фигура под ночным звёздным небом, покой и надежда", "arcana_star_01"),
    ("Старший Аркан «Мир» — фигура в венке, триумфальное завершение пути", "arcana_world_01"),
]


def _build_taro_zodiac() -> list:
    cat = "nonanime/taro_zodiac"
    combined = []
    for name_ru, slug in _ZODIAC_SIGNS_3X:
        combined += _variant_items(cat, f"zodiac_{slug}", f"Знак зодиака {name_ru}",
                                   _ZODIAC_FORMS, 3)
    for name_ru, slug in _ZODIAC_SIGNS_2X:
        combined += _variant_items(cat, f"zodiac_{slug}", f"Знак зодиака {name_ru}",
                                   _ZODIAC_FORMS, 2)
    combined += _fixed_items(cat, _MAJOR_ARCANA)
    # style_pref форсируется (задача лида): чередование 19_tarot/09_ring_medallion
    # по ВСЕЙ теме taro_zodiac (не по знаку) — индекс внутри объединённого списка.
    style_cycle = ["19_tarot", "09_ring_medallion"]
    for i, item in enumerate(combined):
        item["style_pref"] = style_cycle[i % len(style_cycle)]
    assert len(combined) == 40, f"taro_zodiac должен дать 40, дал {len(combined)}"
    return combined


_MEME_VARIANTS = [
    ("невозмутимо сидит в горячем источнике", "hot_spring_calm"),
    ("в костюме бизнесмена с портфелем", "business_suit"),
    ("с короной короля мемов на голове", "meme_king_crown"),
    ("среди уточек в пруду, полная безмятежность", "duck_pond_zen"),
    ("стоическое каменное выражение морды", "stoic_stone_face"),
    ("в вязаной шапке с помпоном", "knit_hat"),
    ("под дождём с крошечным зонтиком", "tiny_umbrella_rain"),
    ("медитирует в позе лотоса на камне", "lotus_meditation"),
    ("с чашкой чая, невозмутимо балансирующей на морде", "tea_cup_balance"),
    ("мем «всё нормально» в горящей комнате", "this_is_fine_meme"),
    ("верхом на плоту сплавляется по реке", "river_raft_ride"),
    ("в крошечном вязаном свитере", "tiny_sweater"),
    ("подмигивает камере с хитрым прищуром", "sly_wink"),
    ("сидит на троне из фруктов, король джунглей", "fruit_throne"),
    ("завтракает бутербродом, деловито и сосредоточенно", "breakfast_sandwich"),
]


def _build_meme_animals() -> list:
    cat = "nonanime/meme_animals"
    out = []
    out += _variant_items(cat, "capybara", "Капибара", _MEME_VARIANTS, 15)
    out += _variant_items(cat, "cat", "Кот-мемчик", _MEME_VARIANTS, 15)
    out += _variant_items(cat, "raccoon", "Енот-полоскун", _MEME_VARIANTS, 5)
    out += _variant_items(cat, "wolf", "Волк", _MEME_VARIANTS, 5)
    return out


def _build_professions() -> list:
    cat = "nonanime/professions"
    out = []
    out += _variant_items(cat, "svarshik", "Сварщик", [
        ("искры фонтаном от сварочного шва, надпись 'СВАРКА - ИСКУССТВО' на гербе", "gerb_sparks"),
        ("маска-хамелеон поднята, довольная усмешка, надпись 'ГОРЮ НА РАБОТЕ'", "mask_up_grin"),
        ("силуэт с горелкой на фоне заводского цеха, надпись 'СВАРЩИК' крупно", "factory_silhouette"),
    ], 8, style_pref="28_metal_cover")
    out += _variant_items(cat, "vrach", "Врач", [
        ("стетоскоп на плечах, уверенный взгляд, надпись 'СПАСАЮ ЖИЗНИ' на гербе", "stethoscope_gerb"),
        ("хирургическая маска, спокойная решимость, надпись 'БЕЗ ПАНИКИ, Я ВРАЧ'", "surgical_calm"),
        ("белый халат развевается, надпись 'ДИАГНОЗ: ГЕРОЙ'", "coat_hero"),
    ], 8, style_pref="22_heraldry")
    out += _variant_items(cat, "programmist", "Программист", [
        ("капюшон худи, свет монитора на лице, надпись 'КОД РАБОТАЕТ - НЕ ТРОГАЙ'", "hoodie_monitor_glow"),
        ("клавиатура как оружие, надпись 'БАГ ИЛИ ФИЧА?' юмористически", "keyboard_weapon"),
        ("чашка кофе и строки кода вокруг фигуры, надпись 'ПРОГРАММИСТ'", "coffee_code_swirl"),
    ], 7)
    out += _variant_items(cat, "elektrik", "Электрик", [
        ("молнии-разряды вокруг рук, дорожный знак-ромб 'ПОД НАПРЯЖЕНИЕМ'", "voltage_arcs"),
        ("отвёртка-индикатор в руке, надпись 'ЭЛЕКТРИК' крупным капсом", "screwdriver_pose"),
    ], 4, style_pref="26_warning_sign")
    out += _variant_items(cat, "uchitel", "Учитель", [
        ("указка у доски с формулами, надпись 'ГЕРОЙ У ДОСКИ' на гербе", "chalkboard_hero"),
        ("книга в руке, строгий но добрый взгляд, надпись 'ТИШИНА, ИДЁТ УРОК'", "book_strict_kind"),
    ], 3, style_pref="22_heraldry")
    return out


def _build_fishing_hunting() -> list:
    cat = "nonanime/fishing_hunting"
    out = []
    out += _variant_items(cat, "rybak", "Рыбак", [
        ("удочка на плече, силуэт на закатной реке, надпись 'РЫБАЛКА - ЭТО ОБРАЗ ЖИЗНИ'", "sunset_riverbank"),
        ("трофейная рыба в руках, довольная улыбка, герб 'БРИГАДА РЫБАКОВ'", "trophy_catch_gerb"),
        ("лодка на туманном рассветном озере, надпись 'КЛЁВОГО НАСТРОЕНИЯ'", "misty_lake_boat"),
        ("зимняя рыбалка, лунка во льду, надпись 'ТИШЕ ЕДЕШЬ - БОЛЬШЕ РЫБЫ'", "ice_fishing_hole"),
    ], 15)
    out += _variant_items(cat, "ohotnik", "Охотник", [
        ("силуэт с ружьём на плече на фоне зимнего леса, герб 'БРИГАДА ОХОТНИКОВ'", "winter_forest_silhouette"),
        ("на привале у костра, надпись 'ОХОТА - ЭТО СВЯЩЕННО'", "campfire_rest"),
        ("следопыт крадётся сквозь туман, надпись 'ТИХАЯ ОХОТА'", "stealth_fog_tracker"),
    ], 10)
    return out


def _build_gym() -> list:
    cat = "nonanime/gym"
    style_cycle = ["28_metal_cover", "26_warning_sign"]
    out = []
    out += _variant_items(cat, "gorilla", "Горилла-качок", [
        ("разрывает цепи бицепсами, надпись 'BEAST MODE' крупно", "chain_break_beast_mode"),
        ("становая тяга штанги, надпись 'ЖЕЛЕЗО НЕ ЖДЁТ'", "deadlift_pose"),
        ("флексит бицепс перед зеркалом зала, надпись 'НИКАКИХ ОТМАЗОК'", "flex_mirror"),
    ], 13, style_pref=style_cycle)
    out += _variant_items(cat, "bear", "Медведь-атлет", [
        ("жим штанги лёжа, пар от разогретых мышц, надпись 'BEAST MODE'", "bench_press_steam"),
        ("тащит покрышку через двор зала, надпись 'ДЕНЬ НОГ'", "tire_drag"),
        ("рычит перед стойкой со штангой, надпись 'КАЧАЛКА - МОЙ ХРАМ'", "roar_rack"),
    ], 12, style_pref=style_cycle)
    return out


def _build_rock_aesthetic() -> list:
    cat = "nonanime/rock_aesthetic"
    return _variant_items(cat, "rock", "Рок-эстетика", [
        ("силуэт электрогитары в лучах прожекторов сцены, без лиц и текстов песен", "guitar_spotlight"),
        ("виниловая пластинка крутится, дымчатая атмосфера студии", "vinyl_smoke"),
        ("панк-звезда — булавки, кожаная куртка, генерик-силуэт без лица", "punk_star_silhouette"),
        ("усилитель и колонки на фоне неонового клуба", "amp_stack_neon"),
        ("гитарный медиатор и струны крупным планом", "pick_strings_closeup"),
    ], 15)


def _build_squid_game() -> list:
    cat = "nonanime/squid_game"
    return _variant_items(cat, "squidgame", "Маски-фигуры игры на выживание", [
        ("фигура в маске-квадрате, генерик-дальгона узор без логотипов", "square_mask_dalgona"),
        ("фигура в маске-круге на фоне розового геометрического коридора", "circle_mask_corridor"),
        ("фигура в маске-треугольнике, силуэт на красном фоне", "triangle_mask_red"),
        ("три фигуры (треугольник/круг/квадрат) выстроены в ряд, генерик", "trio_shapes_lineup"),
        ("детская игра 'красный свет — зелёный свет', генерик-кукла силуэтом", "red_light_green_light"),
    ], 15)


# ═══════════════════════════════════════════════════════════════════════════════
# СБОРКА + ФОРМАТ-МИКС + ВАЛИДАЦИЯ
# ═══════════════════════════════════════════════════════════════════════════════

def build_all_items() -> list:
    """Собирает ВСЕ бакеты в одном порядке (аниме-ядро -> нишевые аниме -> не-аниме),
    сверяет промежуточные суммы (честная ошибка при расхождении — не тихий брак
    плана) и итоговые 800."""
    anime_core = (
        _build_one_piece() + _build_naruto() + _build_chainsaw_man()
        + _build_jujutsu_kaisen() + _build_re_zero() + _build_detective_conan()
        + _build_mushoku_tensei()
    )
    assert len(anime_core) == 545, f"АНИМЕ-ЯДРО должно дать 545, дало {len(anime_core)}"

    niche_anime = _build_niche_anime()
    assert len(niche_anime) == 25, f"нишевые аниме-ставки должны дать 25, дали {len(niche_anime)}"

    nonanime = (
        _build_drift_jdm() + _build_taro_zodiac() + _build_meme_animals()
        + _build_professions() + _build_fishing_hunting() + _build_gym()
        + _build_rock_aesthetic() + _build_squid_game()
    )
    assert len(nonanime) == 230, f"НЕ-АНИМЕ должно дать 230, дало {len(nonanime)}"

    all_items = anime_core + niche_anime + nonanime
    assert len(all_items) == TOTAL_EXPECTED, (
        f"ИТОГО должно быть {TOTAL_EXPECTED}, получилось {len(all_items)}")
    return all_items


def finalize_plan(items: list) -> list:
    """Присваивает seq (1..N) и format (детерминированный микс cutout/diecut по
    ГЛОБАЛЬНОМУ индексу — см. _FORMAT_CUTOUT_EVERY) в порядке сборки."""
    plan = []
    for i, item in enumerate(items):
        seq = i + 1
        fmt = "cutout" if seq % _FORMAT_CUTOUT_EVERY == 0 else "diecut"
        plan.append({
            "seq": seq,
            "category": item["category"],
            "theme": item["theme"],
            "format": fmt,
            "style_pref": item["style_pref"],
            "filename_base": item["filename_base"],
        })
    return plan


def plan_stats(plan: list) -> dict:
    """Сводка для передаточной записки/смоука — не часть самого плана."""
    by_top_category = {}
    by_format = {"diecut": 0, "cutout": 0}
    styled = 0
    for rec in plan:
        top = rec["category"].split("/", 1)[0]
        by_top_category[top] = by_top_category.get(top, 0) + 1
        by_format[rec["format"]] += 1
        if rec["style_pref"]:
            styled += 1
    filename_bases = [rec["filename_base"] for rec in plan]
    return {
        "total": len(plan),
        "by_top_category": by_top_category,
        "by_format": by_format,
        "with_forced_style_pref": styled,
        "unique_filename_bases": len(set(filename_bases)),
    }


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "mega_plan_800.json"),
                    help="куда сохранить план (дефолт mega_plan_800.json рядом со скриптом)")
    args = ap.parse_args()

    items = build_all_items()
    plan = finalize_plan(items)

    # Дубликаты filename_base ломают резюмируемость mega_batch_run.py (журнал по
    # filename_base) и перезапись файлов на диске — честная остановка, не тихий брак.
    bases = [rec["filename_base"] for rec in plan]
    dupes = {b for b in bases if bases.count(b) > 1}
    if dupes:
        raise RuntimeError(f"дубликаты filename_base в плане (первые 10): "
                           f"{sorted(dupes)[:10]}")

    out_path = Path(args.out)
    out_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = plan_stats(plan)
    print(f"план сохранён -> {out_path} ({stats['total']} записей)", flush=True)
    print(f"по верхней категории: {stats['by_top_category']}", flush=True)
    print(f"по формату: {stats['by_format']}", flush=True)
    print(f"с форсированным style_pref: {stats['with_forced_style_pref']}", flush=True)
    print(f"уникальных filename_base: {stats['unique_filename_bases']} "
          f"(должно совпасть с total)", flush=True)


if __name__ == "__main__":
    main()
