# -*- coding: utf-8 -*-
"""upscale.py — апскейл диеката до печатных 300 DPI через portable realesrgan-ncnn-vulkan
(Windows exe, Vulkan-рендер — работает на встроенной GPU без CUDA, см. GOTCHAS проекта:
машина владельца Ryzen 5 4600H + встроенная Vega, ncnn-vulkan/CPU — единственный вариант
для тяжёлого ML на этом железе).

Инструмент — portable-архив с офиц. релиза xinntao/Real-ESRGAN (v0.2.5.0, 2022-04-24,
realesrgan-ncnn-vulkan-20220424-windows.zip), НЕ качается pip-пакетом — уже распакован в
tools/realesrgan/ (exe + vcomp140[d].dll + models/realesrgan-x4plus[-anime].{bin,param}),
tools/ добавлен в .gitignore (бинарники в git не кладём, ~43MB архива).

Публичная точка входа: upscale(png_in, png_out, scale=4, model="realesrgan-x4plus-anime").

ЗАМЕР (2026-07-09, эта машина — AMD Radeon встроенная GPU через Vulkan, подтверждено
verbose-выводом exe): 3 реальных diecut из out_batch/ten_more_styles/ (843x1264..1376x768,
x4plus-anime, scale=4) — 54.8с / 60.2с / 64.4с, среднее ~60с/шт, разрешение x4 корректное
(843x1264 -> 3372x5056). В пределах порога 90с/шт из задачи лида — UPSCALE=on остаётся
дефолтом (см. config.UPSCALE).

Пятнадцатый заход — 3 доработки (задача лида, живой брак 0007_payback):

1. АДАПТИВНЫЙ АПСКЕЙЛ ДО ПЕЧАТНОГО МИНИМУМА (upscale_to_print_min): nano-banana отдаёт
   исходники РАЗНОГО размера (768..1408px наблюдалось на живых raw) — x4 realesrgan НЕ
   гарантирует единый печатный минимум config.PRINT_MIN_SIDE по большей стороне на всех
   дизайнах (например raw 768px * 4 = 3072px < 3800 дефолт). upscale_to_print_min() зовёт
   upscale() как раньше (x4 realesrgan), затем ЕСЛИ большая сторона результата всё ещё
   меньше min_side — досчитывает PIL Lanczos ПРЯМО ПОВЕРХ результата x4 (аниме-графика с
   плоскими заливками после ESRGAN тянется чисто дополнительным Lanczos-пассом, второй
   проход x4 realesrgan запрещён по времени — уже ~60с/шт на первом проходе).
2. СЕРИАЛИЗАЦИЯ (_UPSCALE_LOCK, threading.Lock): параллельные вызовы realesrgan-ncnn-
   vulkan на встроенной Vega ДУШАТ друг друга (замер лида: соло ~55-70с/шт, при
   WORKERS=4 без сериализации 94-115с/шт с риском таймаута — GPU не шардится корректно
   между процессами exe). upscale() держит ГЛОБАЛЬНЫЙ lock модуля на время ВСЕГО вызова
   subprocess — воркеры (daily_prints.py ThreadPoolExecutor) выстраиваются в очередь на
   апскейл вместо конкуренции за GPU напрямую.
3. ТАЙМАУТ + LANCZOS-ФОЛБЭК (upscale_to_print_min): timeout читается из
   config.UPSCALE_TIMEOUT (дефолт 300с) вместо старого хардкода 180. При таймауте/сбое
   realesrgan — Lanczos-апскейл diecut НАПРЯМУЮ до min_side (хуже качеством, чем ESRGAN,
   но печатный размер гарантирован) + result["print_fallback"]=True (вызывающий код,
   batch_print.render_design, логирует предупреждение).

Шестнадцатый заход (задача лида, план на 800: мега-батч на этой машине) — БЭКПОРТ
Replicate-апскейла из content-factory-saas/engine/print_factory/upscale.py (там он был
написан как облачная замена ЛОКАЛЬНОГО realesrgan для Linux-прода БЕЗ GPU). На ЭТОЙ
машине GPU (Vulkan) ЕСТЬ, но upscale_to_print_min() держит ГЛОБАЛЬНЫЙ _UPSCALE_LOCK на
время всего subprocess-вызова realesrgan (см. пункт 2 выше) — при WORKERS=4 воркеры
мега-батча реально сериализуются на апскейле (~60с/шт * N вместо параллели), это
самое узкое место всего конвейера на объёме 800 принтов. Задача лида: сделать Replicate
(nightmareai/real-esrgan, HTTP API без SDK — requests, тот же приём, что providers.py)
ПЕРВЫМ путём в upscale_to_print_min ПРИ НАЛИЧИИ REPLICATE_API_TOKEN — облачный вызов НЕ
делит одну GPU между воркерами (сеть, не локальный Vulkan-девайс), поэтому реальный
параллелизм WORKERS=4 не душит сам себя. Порядок ОБРАТНЫЙ по сравнению с content-
factory-saas (там Replicate был fallback ПОСЛЕ локального realesrgan, потому что на
проде локального exe вообще нет): здесь Replicate — ПЕРВЫЙ путь (есть токен -> сеть,
параллельно), локальный realesrgan-ncnn-vulkan.exe — ВТОРОЙ путь (Replicate не задан
токеном/сбой сети/таймаут -> локальный GPU-путь как раньше, серийно через
_UPSCALE_LOCK), Lanczos — финальный фолбэк (оба апскейлера недоступны/упали).
Сериализация _UPSCALE_LOCK НЕ трогается (единственная GPU на машине по-прежнему одна) —
для Replicate вместо полного Lock используется _REPLICATE_SEMAPHORE (см. ниже),
ограничивающий параллельность облачных вызовов потолком (дефолт 4, = WORKERS), а не
полностью серийно (сеть — не общий физический ресурс, как GPU, реальная параллельность
допустима и нужна, задача лида: "параллельность 4 ок")."""
import os
import shutil
import subprocess
import threading
import time
from base64 import b64encode
from pathlib import Path

import requests
from PIL import Image

import config

HERE = Path(__file__).resolve().parent
REALESRGAN_DIR = HERE / "tools" / "realesrgan"
REALESRGAN_EXE = REALESRGAN_DIR / "realesrgan-ncnn-vulkan.exe"
REALESRGAN_MODELS_DIR = REALESRGAN_DIR / "models"

DEFAULT_MODEL = "realesrgan-x4plus-anime"
DEFAULT_SCALE = 4

# Модели, для которых в tools/realesrgan/models/ реально лежат .bin/.param (см. docstring
# модуля) — используется только для раннего честного предупреждения, не как жёсткий allow-
# list (exe сам провалится понятной ошибкой на отсутствующей модели, если список устареет).
_KNOWN_MODELS = ("realesrgan-x4plus-anime", "realesrgan-x4plus",
                  "realesr-animevideov3", "realesrnet-x4plus")

# Сериализация апскейлов между воркерами (пятнадцатый заход, см. докстринг модуля пункт
# 2) — ГЛОБАЛЬНЫЙ lock процесса, держится на время ОДНОГО subprocess-вызова realesrgan
# внутри upscale(). Модуль-уровня, не per-instance — единственный экземпляр GPU на
# машине, все воркеры одного Python-процесса (daily_prints.py ThreadPoolExecutor)
# обязаны реально выстроиться в очередь, а не запускать exe параллельно.
_UPSCALE_LOCK = threading.Lock()

# ── Replicate-апскейл (шестнадцатый заход, бэкпорт из content-factory-saas) ─────

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "").strip()

# Версия модели nightmareai/real-esrgan закреплена явно (Replicate требует конкретный
# version-хэш, "latest" API не отдаёт) — переопределяема через env.
# ГРАБЛЯ (живая проверка 2026-07-09): хэш "f121d640..." из content-factory-saas
# ("тот же хэш, что уже был подтверждён рабочим") на деле УСТАРЕЛ на момент бэкпорта —
# живой вызов упал HTTP 422 "Invalid version or not permitted" (Replicate периодически
# депрекейтит старые version-снимки модели). Актуальный хэш добыт запросом
# GET /v1/models/nightmareai/real-esrgan -> latest_version.id и подтверждён живым
# вызовом (см. передаточную записку разработчика) — если Replicate снова депрекейтит
# эту версию, тот же запрос покажет новую latest_version.id, обновить константу или
# задать REPLICATE_REAL_ESRGAN_VERSION в .env.
REPLICATE_REAL_ESRGAN_VERSION = os.getenv(
    "REPLICATE_REAL_ESRGAN_VERSION",
    "b3ef194191d13140337468c916c2c5b96dd0cb06dffc032a022a31807f6a5ea8",
)

_REPLICATE_API_BASE = "https://api.replicate.com/v1"
_REPLICATE_POLL_INTERVAL_SEC = 3
_REPLICATE_MAX_POLL_SEC = 180

# Ослабленная "сериализация" для Replicate (задача лида: "сеть, не GPU — параллельность
# 4 ок") — В ОТЛИЧИЕ от _UPSCALE_LOCK (полный Lock, ОДИН локальный GPU-вызов за раз),
# здесь Semaphore с потолком REPLICATE_MAX_CONCURRENT (дефолт 4, = config.WORKERS) —
# несколько облачных апскейлов реально идут ОДНОВРЕМЕННО (сеть — не общий физический
# ресурс машины), но не безгранично (не заваливаем API/не плодим случайный всплеск
# затрат при большом WORKERS). Оборачивает ТОЛЬКО сетевую часть upscale_via_replicate
# (создание предсказания + поллинг + скачивание), не проверки токена/чтение файла.
REPLICATE_MAX_CONCURRENT = int(os.getenv("REPLICATE_MAX_CONCURRENT", "4"))
_REPLICATE_SEMAPHORE = threading.Semaphore(max(1, REPLICATE_MAX_CONCURRENT))


class UpscaleUnavailable(Exception):
    """exe/модель не найдены — вызывающий код должен ПРЕДУПРЕДИТЬ и пропустить апскейл,
    не падать (см. batch_print.render_design, config.UPSCALE)."""


def is_available() -> bool:
    """exe присутствует на диске (минимальная проверка перед вызовом — не гоняем subprocess
    впустую, если инструмент вообще не установлен на этой машине)."""
    return REALESRGAN_EXE.exists()


def upscale(png_in, png_out, scale: int = DEFAULT_SCALE,
            model: str = DEFAULT_MODEL, timeout: int = None) -> dict:
    """Апскейл PNG (RGBA, альфа-канал сохраняется — подтверждено на реальных diecut)
    через portable realesrgan-ncnn-vulkan.exe. png_in/png_out — путь (str|Path) к входному/
    выходному PNG.

    scale: 2|3|4 (см. exe -h). model: имя модели из tools/realesrgan/models/ (дефолт
    x4plus-anime — специализирована под аниме-арт, лучше держит чистые контуры/заливки
    цветом, чем общая x4plus). timeout — секунд на один вызов subprocess; None (дефолт) —
    читает config.UPSCALE_TIMEOUT НА МОМЕНТ ВЫЗОВА (не константа на импорте — важно для
    тестов с monkeypatch), дефолт .env 300с (замер на этой машине ~55-65с/шт соло, до
    ~115с/шт при 4 параллельных воркерах БЕЗ сериализации — запас на случай, что
    _UPSCALE_LOCK ниже всё равно достанется не сразу).

    СЕРИАЛИЗАЦИЯ (пятнадцатый заход, см. докстринг модуля пункт 2): вызов subprocess.run
    держит _UPSCALE_LOCK (threading.Lock, модуль-уровня) на всё время работы exe — при
    параллельных вызовах из нескольких воркеров (ThreadPoolExecutor, daily_prints.py)
    реально исполняется ОДИН апскейл в момент времени, остальные ждут своей очереди
    (не конкурируют за GPU напрямую, что душит производительность каждого — см. замер).

    Возвращает {"ok": bool, "elapsed_sec": float, "out_size": (w,h)|None, "error": str|None}.
    НЕ бросает исключение при отсутствии exe/модели или сбое subprocess — вызывающий код
    (batch_print.render_design, через upscale_to_print_min) обязан честно предупредить в
    лог и ПРОПУСТИТЬ апскейл (печатный <tag>_print.png просто не создаётся), не ронять
    весь дизайн."""
    if timeout is None:
        timeout = config.UPSCALE_TIMEOUT
    t0 = time.time()
    result = {"ok": False, "elapsed_sec": 0.0, "out_size": None, "error": None}

    if not REALESRGAN_EXE.exists():
        result["error"] = (
            f"realesrgan-ncnn-vulkan.exe не найден ({REALESRGAN_EXE}) — апскейл "
            f"пропущен, скачать portable-архив: https://github.com/xinntao/Real-ESRGAN/"
            f"releases (realesrgan-ncnn-vulkan-*-windows.zip) в tools/realesrgan/")
        return result

    model_bin = REALESRGAN_MODELS_DIR / f"{model}.bin"
    model_param = REALESRGAN_MODELS_DIR / f"{model}.param"
    if not model_bin.exists() or not model_param.exists():
        result["error"] = (
            f"модель {model!r} не найдена в {REALESRGAN_MODELS_DIR} "
            f"(ожидались {model}.bin/{model}.param) — апскейл пропущен")
        return result

    # resolve() ОБЯЗАТЕЛЕН: subprocess.run(cwd=REALESRGAN_DIR) ниже меняет рабочую
    # директорию exe — относительный путь png_in/png_out (например "out_batch/x.png")
    # интерпретировался бы относительно REALESRGAN_DIR, не относительно текущей cwd
    # Python-процесса, exe падал с "decode image ... failed" (реальный баг, пойман на
    # живом замере). Абсолютный путь работает одинаково независимо от cwd subprocess.
    png_in = Path(png_in).resolve()
    png_out = Path(png_out).resolve()
    png_out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [str(REALESRGAN_EXE), "-i", str(png_in), "-o", str(png_out),
           "-s", str(scale), "-n", model]
    try:
        # _UPSCALE_LOCK: держим ГЛОБАЛЬНЫЙ lock только на время самого subprocess-вызова
        # (не на всю функцию — проверки exe/модели/путей выше не трогают GPU, не имеет
        # смысла блокировать другие потоки на них). См. докстринг функции/модуля пункт 2.
        with _UPSCALE_LOCK:
            proc = subprocess.run(cmd, cwd=str(REALESRGAN_DIR), capture_output=True,
                                  text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        result["error"] = f"апскейл превысил таймаут {timeout}с — пропущен"
        result["elapsed_sec"] = round(time.time() - t0, 1)
        return result
    except Exception as e:  # noqa: BLE001 — любой сбой subprocess не должен ронять пайплайн
        result["error"] = f"апскейл упал: {e}"
        result["elapsed_sec"] = round(time.time() - t0, 1)
        return result

    result["elapsed_sec"] = round(time.time() - t0, 1)
    if proc.returncode != 0 or not png_out.exists():
        stderr_tail = (proc.stderr or "")[-400:]
        result["error"] = f"realesrgan-ncnn-vulkan вернул код {proc.returncode}: {stderr_tail}"
        return result

    try:
        with Image.open(png_out) as im:
            result["out_size"] = im.size
    except Exception as e:  # noqa: BLE001 — файл создан, но битый/не читается PIL
        result["error"] = f"выходной PNG не читается PIL: {e}"
        return result

    result["ok"] = True
    return result


def _lanczos_boost_in_place(png_out: Path, min_side: int, result: dict) -> None:
    """Если result["out_size"] (уже сохранённый png_out) меньше min_side по большей
    стороне — досчитывает PIL Lanczos ПРЯМО ПОВЕРХ файла (не второй проход платного
    апскейлера — см. упоминания в докстрингах upscale_to_print_min). Мутирует result
    ("out_size" на новый размер, либо "error" при сбое досчёта — сбой НЕ отменяет уже
    готовый апскейл-результат, файл остаётся как есть). result["print_fallback"]
    ВЫЗЫВАЮЩИЙ КОД не трогает (досчёт поверх успешного апскейла — не фолбэк-путь)."""
    out_w, out_h = result["out_size"]
    if max(out_w, out_h) >= min_side:
        return
    try:
        with Image.open(png_out) as im:
            im = im.convert("RGBA")
            cur_max = max(im.size)
            factor = max(1.0, min_side / cur_max)
            new_size = (max(1, round(im.width * factor)), max(1, round(im.height * factor)))
            resized = im.resize(new_size, Image.LANCZOS)
            resized.save(png_out)
        result["out_size"] = resized.size
    except Exception as e:  # noqa: BLE001 — досчёт не должен уничтожать уже готовый результат
        result["error"] = f"адаптивный Lanczos-досчёт до {min_side}px упал: {e} " \
                          f"(апскейл-результат сохранён как есть)"


def upscale_to_print_min(png_in, png_out, min_side: int = None,
                          scale: int = DEFAULT_SCALE, model: str = DEFAULT_MODEL,
                          timeout: int = None) -> dict:
    """Адаптивный апскейл до печатного минимума — гарантирует, что БОЛЬШАЯ сторона
    png_out >= min_side (дефолт config.PRINT_MIN_SIDE, 3800px), независимо от исходного
    размера png_in (nano-banana отдаёт 768..1408px на разных дизайнах — x4 один в один
    не даёт единого печатного минимума).

    Алгоритм (шестнадцатый заход, задача лида — план на 800, см. докстринг модуля):
      1. Replicate (upscale_via_replicate, nightmareai/real-esrgan) — ПЕРВЫЙ путь, ЕСЛИ
         REPLICATE_API_TOKEN задан (replicate_available()). Облачный вызов — сеть, не
         делит единственную локальную GPU между воркерами мега-батча (см. модульный
         докстринг про _REPLICATE_SEMAPHORE vs _UPSCALE_LOCK). Токен не задан — путь
         пропускается БЕЗ сетевого вызова, сразу пункт 2 (как будто Replicate вообще
         не существует — обратная совместимость с пятнадцатым заходом).
      2. Локальный realesrgan-ncnn-vulkan.exe (upscale(), как в пятнадцатом заходе) —
         ВТОРОЙ путь, если Replicate не задан токеном ИЛИ сетевой вызов Replicate упал
         (таймаут/ошибка API/нет сети).
      3. Если ОБА апскейлера недоступны/упали — ЛАНCZOS-ФОЛБЭК: PIL Lanczos НАПРЯМУЮ с
         png_in до min_side по большей стороне (хуже качеством, но печатный размер
         гарантирован) — result["print_fallback"]=True.
      4. Если сработавший апскейлер (Replicate ИЛИ локальный) дал результат МЕНЬШЕ
         min_side (raw был совсем мелкий) — ДОСЧИТЫВАЕТ PIL Lanczos ПРЯМО ПОВЕРХ его
         результата (_lanczos_boost_in_place) — result["print_fallback"] остаётся False
         (это НЕ фолбэк-путь, платный апскейлер успешно отработал, просто добавлен
         адаптивный досчёт до минимума).

    Возвращает тот же формат, что upscale()/upscale_via_replicate(), плюс
    result["print_fallback"]: bool (True только когда НИ ОДИН апскейлер не отработал
    и картинка получена целиком через Lanczos с исходника, см. пункт 3) — вызывающий
    код (batch_print.render_design) обязан честно предупредить в лог при
    print_fallback=True (хуже качество), но НЕ ронять дизайн (тот же принцип, что и
    полный пропуск апскейла раньше)."""
    if min_side is None:
        min_side = config.PRINT_MIN_SIDE
    png_in = Path(png_in)
    png_out = Path(png_out)

    replicate_error = None
    if replicate_available():
        # ПУТЬ 1 (первый): облачный Replicate — токен задан, реально пробуем сеть
        # ДО локального GPU-пути (см. докстринг функции/модуля).
        rep_result = upscale_via_replicate(png_in, png_out, scale=scale, timeout=timeout)
        rep_result["print_fallback"] = False
        if rep_result["ok"]:
            _lanczos_boost_in_place(png_out, min_side, rep_result)
            return rep_result
        replicate_error = rep_result["error"]

    # ПУТЬ 2 (второй): локальный realesrgan-ncnn-vulkan (пятнадцатый заход, как раньше)
    # — Replicate не задан токеном ИЛИ сетевой вызов упал.
    result = upscale(png_in, png_out, scale=scale, model=model, timeout=timeout)
    result["print_fallback"] = False

    if not result["ok"]:
        # exe отсутствует / модель не найдена / сбой subprocess / ТАЙМАУТ / Replicate
        # тоже не отработал (если пробовали) — Lanczos напрямую с исходника до
        # min_side, печатный размер гарантирован даже без обоих апскейлеров.
        esrgan_error = result["error"]
        combined_error = (f"Replicate: {replicate_error}; realesrgan: {esrgan_error}"
                          if replicate_error else esrgan_error)
        try:
            with Image.open(png_in) as src:
                src = src.convert("RGBA")
                w, h = src.size
                cur_max = max(w, h)
                if cur_max <= 0:
                    raise ValueError(f"нулевой размер исходника {png_in}")
                factor = max(1.0, min_side / cur_max)
                new_size = (max(1, round(w * factor)), max(1, round(h * factor)))
                resized = src.resize(new_size, Image.LANCZOS)
                png_out.parent.mkdir(parents=True, exist_ok=True)
                resized.save(png_out)
        except Exception as e:  # noqa: BLE001 — Lanczos-фолбэк тоже не должен ронять пайплайн
            result["error"] = f"апскейл упал ({combined_error}); Lanczos-фолбэк тоже упал: {e}"
            return result

        result["ok"] = True
        result["out_size"] = resized.size
        result["error"] = f"апскейл недоступен/упал ({combined_error}) — Lanczos-фолбэк до {min_side}px"
        result["print_fallback"] = True
        return result

    # локальный realesrgan отработал успешно — проверяем, дотягивает ли x4 до минимума.
    _lanczos_boost_in_place(png_out, min_side, result)
    return result


def tools_dir_exists() -> bool:
    """Есть ли вообще папка tools/realesrgan/ на диске (для диагностических сообщений)."""
    return REALESRGAN_DIR.exists()


# ---------------------------------------------------------------------------
# REPLICATE-АПСКЕЙЛ (шестнадцатый заход, бэкпорт из content-factory-saas)
# ---------------------------------------------------------------------------
#
# upscale_via_replicate() — облачная замена x4-прохода realesrgan через модель
# nightmareai/real-esrgan на Replicate (HTTP API, БЕЗ replicate-SDK — тот же принцип
# "requests, без лишних зависимостей", что providers.py). На ЭТОЙ машине (в отличие от
# content-factory-saas прода) локальный GPU-путь реально доступен и остаётся рабочим
# как раньше — Replicate здесь не "замена отсутствующему GPU", а способ ИЗБЕЖАТЬ
# сериализации на единственной локальной GPU при WORKERS>1 (см. upscale_to_print_min).
# REPLICATE_API_TOKEN не задан -> replicate_available() возвращает False, функция ниже
# вообще не вызывается из upscale_to_print_min (см. её код выше) — без сетевого вызова.


def replicate_available() -> bool:
    """REPLICATE_API_TOKEN задан в окружении — облачный апскейл в принципе доступен
    (сетевой вызов ещё может упасть отдельно, это только наличие ключа)."""
    return bool(REPLICATE_API_TOKEN)


def _image_to_data_uri(png_path: Path) -> str:
    """PNG-файл -> data URI (base64) — Replicate принимает как inline-вход, так и
    обычный URL; для приватных исходников (принт до публикации) inline data URI
    безопаснее — файл не должен светиться на публичном URL."""
    data = Path(png_path).read_bytes()
    b64 = b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def upscale_via_replicate(png_in, png_out, scale: int = 4, timeout: int = None) -> dict:
    """Апскейл PNG через Replicate (nightmareai/real-esrgan) — облачный путь,
    параллелизуется между воркерами через _REPLICATE_SEMAPHORE (см. модульный
    докстринг), в отличие от _UPSCALE_LOCK у локального realesrgan.

    png_in/png_out — путь (str|Path) к входному/выходному PNG (RGBA, альфа сохраняется
    — модель поддерживает прозрачность на входе/выходе). scale: кратность апскейла
    (модель принимает 2/4, дефолт 4 — как локальный путь). timeout — секунд НА ВЕСЬ
    цикл создания+поллинга предсказания; None (дефолт) читает config.UPSCALE_TIMEOUT
    (тот же порог, что у локального upscale(), унифицированный конфиг для обеих веток).

    Возвращает {"ok": bool, "elapsed_sec": float, "out_size": (w,h)|None,
    "error": str|None} — ТОТ ЖЕ контракт, что upscale(), чтобы upscale_to_print_min
    могла звать любую из двух веток взаимозаменяемо.

    НЕ бросает исключение при отсутствии токена/сбое сети/таймауте API — вызывающий
    код (upscale_to_print_min) обязан откатиться на локальный realesrgan, как и при
    сбое любого другого апскейлера (тот же принцип деградации)."""
    if timeout is None:
        timeout = config.UPSCALE_TIMEOUT
    t0 = time.time()
    result = {"ok": False, "elapsed_sec": 0.0, "out_size": None, "error": None}

    if not REPLICATE_API_TOKEN:
        result["error"] = "REPLICATE_API_TOKEN не задан — облачный апскейл (Replicate) пропущен"
        return result

    png_in = Path(png_in)
    png_out = Path(png_out)
    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        data_uri = _image_to_data_uri(png_in)
    except Exception as e:  # noqa: BLE001 — файл не читается/не найден
        result["error"] = f"Replicate: не смог прочитать входной PNG: {e}"
        result["elapsed_sec"] = round(time.time() - t0, 1)
        return result

    body = {
        "version": REPLICATE_REAL_ESRGAN_VERSION,
        "input": {"image": data_uri, "scale": scale, "face_enhance": False},
    }

    # _REPLICATE_SEMAPHORE: только сетевую часть (создание + поллинг + скачивание)
    # ограничиваем потолком REPLICATE_MAX_CONCURRENT (дефолт 4) — НЕ полный Lock, как
    # у локального GPU (_UPSCALE_LOCK), несколько воркеров реально идут параллельно
    # (см. докстринг модуля, задача лида "параллельность 4 ок").
    with _REPLICATE_SEMAPHORE:
        try:
            r = requests.post(f"{_REPLICATE_API_BASE}/predictions", headers=headers,
                              json=body, timeout=60)
        except Exception as e:  # noqa: BLE001 — сеть недоступна
            result["error"] = f"Replicate: создание предсказания упало (сеть): {e}"
            result["elapsed_sec"] = round(time.time() - t0, 1)
            return result

        if r.status_code not in (200, 201):
            result["error"] = f"Replicate: HTTP {r.status_code} при создании предсказания: {r.text[:300]}"
            result["elapsed_sec"] = round(time.time() - t0, 1)
            return result

        prediction = r.json()
        get_url = (prediction.get("urls") or {}).get("get")
        if not get_url:
            result["error"] = f"Replicate: ответ без urls.get: {str(prediction)[:300]}"
            result["elapsed_sec"] = round(time.time() - t0, 1)
            return result

        # Поллинг статуса предсказания — Replicate асинхронный API, апскейл занимает
        # обычно 5-20с на 4x, но модель может быть "холодной" (cold start до ~60с).
        poll_deadline = min(timeout, _REPLICATE_MAX_POLL_SEC)
        poll_start = time.time()
        output_url = None
        while time.time() - poll_start < poll_deadline:
            try:
                pr = requests.get(get_url, headers=headers, timeout=30)
            except Exception as e:  # noqa: BLE001
                result["error"] = f"Replicate: опрос статуса упал (сеть): {e}"
                result["elapsed_sec"] = round(time.time() - t0, 1)
                return result
            if pr.status_code != 200:
                result["error"] = f"Replicate: HTTP {pr.status_code} при опросе статуса: {pr.text[:300]}"
                result["elapsed_sec"] = round(time.time() - t0, 1)
                return result
            data = pr.json()
            status = data.get("status")
            if status == "succeeded":
                out = data.get("output")
                # Модель обычно отдаёт один URL строкой; на некоторых версиях — список.
                output_url = out[0] if isinstance(out, list) and out else out
                break
            if status in ("failed", "canceled"):
                err_detail = data.get("error") or "нет деталей"
                result["error"] = f"Replicate: предсказание завершилось status={status}: {err_detail}"
                result["elapsed_sec"] = round(time.time() - t0, 1)
                return result
            time.sleep(_REPLICATE_POLL_INTERVAL_SEC)

        if not output_url:
            result["error"] = f"Replicate: не дождались результата за {poll_deadline}с (таймаут поллинга)"
            result["elapsed_sec"] = round(time.time() - t0, 1)
            return result

        try:
            img_resp = requests.get(output_url, timeout=60)
            img_resp.raise_for_status()
            png_out.parent.mkdir(parents=True, exist_ok=True)
            png_out.write_bytes(img_resp.content)
        except Exception as e:  # noqa: BLE001
            result["error"] = f"Replicate: не смог скачать результат: {e}"
            result["elapsed_sec"] = round(time.time() - t0, 1)
            return result

    try:
        with Image.open(png_out) as im:
            result["out_size"] = im.size
    except Exception as e:  # noqa: BLE001 — файл скачан, но битый/не читается PIL
        result["error"] = f"Replicate: результат скачан, но не читается PIL: {e}"
        result["elapsed_sec"] = round(time.time() - t0, 1)
        return result

    result["ok"] = True
    result["elapsed_sec"] = round(time.time() - t0, 1)
    return result
