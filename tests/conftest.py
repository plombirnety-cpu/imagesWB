# -*- coding: utf-8 -*-
"""conftest.py — общие фикстуры pytest для всего tests/.

Дневная квота Gemini на этой машине живая (реальный GEMINI_API_KEY лежит в .env,
config.load_dotenv() подхватывает его в том числе при прогоне тестов — cwd при
`cd print-factory-nb && python -m pytest tests/...` = print-factory-nb/, там и лежит
.env). БЕЗ этой фикстуры новый vision-QC-гейт анатомии рук (batch_print._verify_anatomy
/ providers.verify_anatomy_in_image, восемнадцатый заход, 2026-07-11) попытался бы
РЕАЛЬНО стучаться в сеть на каждом вызове batch_print.render_design во ВСЕХ
существующих тестах (test_print_quality.py, test_chroma_qc_and_text_strength.py,
test_green_only_mode.py, test_style_bank.py и т.п.), которые ничего не знают о новом
гейте и не мокают его — design.get("has_human_figure", True) дефолтит в True, а
config.ANATOMY_QC дефолтит в "on", так что гейт технически применим к любому render_design
из существующих тестов. Та же проблема уже была решена точечно для REPLICATE_API_TOKEN
в tests/test_print_quality.py::_no_real_replicate_token (см. докстринг там) — здесь то
же самое, но ОДНОЙ фикстурой на ВЕСЬ каталог tests/, а не в одном файле.

config.ANATOMY_QC форсится в False автоматически для КАЖДОГО теста (autouse=True) —
офлайн по умолчанию, как и весь остальной проект (все существующие докстринги файлов
tests/ утверждают "Полностью офлайн — никакой сети"). Тесты, которые ЦЕЛЕНАПРАВЛЕННО
проверяют сам гейт анатомии (tests/test_anatomy_qc.py), включают его явно через
monkeypatch.setattr(config, "ANATOMY_QC", True) ПОВЕРХ этой фикстуры — тот же объект
monkeypatch, последний setattr в рамках теста побеждает; сеть там ВСЁ РАВНО замокана
(providers.verify_anatomy_in_image подменяется fake-функцией), просто гейт логически
активен для проверки retry/best-effort-логики самого batch_print."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

import config  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_anatomy_qc_by_default(monkeypatch):
    monkeypatch.setattr(config, "ANATOMY_QC", False)
