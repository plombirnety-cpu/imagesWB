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
   batch_print.render_design, логирует предупреждение)."""
import shutil
import subprocess
import threading
import time
from pathlib import Path

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


def upscale_to_print_min(png_in, png_out, min_side: int = None,
                          scale: int = DEFAULT_SCALE, model: str = DEFAULT_MODEL,
                          timeout: int = None) -> dict:
    """Адаптивный апскейл до печатного минимума (пятнадцатый заход, задача лида) —
    гарантирует, что БОЛЬШАЯ сторона png_out >= min_side (дефолт config.PRINT_MIN_SIDE,
    3800px), независимо от исходного размера png_in (nano-banana отдаёт 768..1408px на
    разных дизайнах — x4 realesrgan один в один не даёт единого печатного минимума).

    Алгоритм:
      1. upscale() как раньше (x4 realesrgan-ncnn-vulkan, GPU через Vulkan).
      2. Если exe отсутствует/сбой/ТАЙМАУТ — ЛАНCZOS-ФОЛБЭК: PIL Lanczos НАПРЯМУЮ с
         png_in до min_side по большей стороне (хуже качеством, чем ESRGAN, но печатный
         размер гарантирован) — result["print_fallback"]=True.
      3. Если realesrgan отработал ok, но большая сторона результата ВСЁ РАВНО < min_side
         (raw был совсем мелкий, например 768px * 4 = 3072 < 3800) — ДОСЧИТЫВАЕТ PIL
         Lanczos ПРЯМО ПОВЕРХ результата x4 (не второй проход realesrgan — второй x4
         запрещён по времени, аниме-графика с плоскими заливками после ESRGAN тянется
         чисто дополнительным Lanczos-пассом) — result["print_fallback"] остаётся False
         (это НЕ фолбэк-путь, x4 realesrgan успешно отработал, просто добавлен адаптивный
         досчёт до минимума).

    Возвращает тот же формат, что upscale(), плюс result["print_fallback"]: bool (True
    только когда realesrgan НЕ отработал вовсе и картинка получена целиком через
    Lanczos с исходника, см. пункт 2) — вызывающий код (batch_print.render_design)
    обязан честно предупредить в лог при print_fallback=True (хуже качество), но НЕ
    ронять дизайн (тот же принцип, что и полный пропуск апскейла раньше)."""
    if min_side is None:
        min_side = config.PRINT_MIN_SIDE
    png_in = Path(png_in)
    png_out = Path(png_out)

    result = upscale(png_in, png_out, scale=scale, model=model, timeout=timeout)
    result["print_fallback"] = False

    if not result["ok"]:
        # exe отсутствует / модель не найдена / сбой subprocess / ТАЙМАУТ — Lanczos
        # напрямую с исходника до min_side, печатный размер гарантирован даже без ESRGAN.
        esrgan_error = result["error"]
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
            result["error"] = f"realesrgan упал ({esrgan_error}); Lanczos-фолбэк тоже упал: {e}"
            return result

        result["ok"] = True
        result["out_size"] = resized.size
        result["error"] = f"realesrgan недоступен/упал ({esrgan_error}) — Lanczos-фолбэк до {min_side}px"
        result["print_fallback"] = True
        return result

    # realesrgan отработал успешно — проверяем, дотягивает ли x4 до печатного минимума.
    out_w, out_h = result["out_size"]
    if max(out_w, out_h) >= min_side:
        return result  # x4 realesrgan уже достаточен, адаптивный досчёт не нужен

    try:
        with Image.open(png_out) as im:
            im = im.convert("RGBA")
            cur_max = max(im.size)
            factor = max(1.0, min_side / cur_max)
            new_size = (max(1, round(im.width * factor)), max(1, round(im.height * factor)))
            resized = im.resize(new_size, Image.LANCZOS)
            resized.save(png_out)
        result["out_size"] = resized.size
    except Exception as e:  # noqa: BLE001 — досчёт не должен уничтожать уже готовый x4-результат
        result["error"] = f"адаптивный Lanczos-досчёт до {min_side}px упал: {e} " \
                          f"(x4-результат realesrgan сохранён как есть)"

    return result


def tools_dir_exists() -> bool:
    """Есть ли вообще папка tools/realesrgan/ на диске (для диагностических сообщений)."""
    return REALESRGAN_DIR.exists()
