# Собирать ИЗ РОДИТЕЛЬСКОЙ папки print-factory-nb/ (контекст нужен для модулей
# движка), Dockerfile лежит в panel/ — см. panel/docker-compose.yml (build.context: ..,
# build.dockerfile: panel/Dockerfile) и panel/PLAN.md "Деплой".
FROM python:3.12-slim

# Pillow (jpeg/png/tiff/webp) + opencv-python-headless (glib/gomp) системные либы.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g libtiff6 libwebp7 libopenjp2-7 \
        libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости движка (редко меняются — отдельный слой) + зависимости панели.
COPY requirements.txt ./engine-requirements.txt
COPY panel/requirements.txt ./panel-requirements.txt
RUN pip install --no-cache-dir -r engine-requirements.txt -r panel-requirements.txt

# Модули движка, которые реально импортирует панель: art_director -> llm_provider,
# franchise_scout -> llm_provider, batch_print -> art_director/character_ref/
# chroma_remove/meme_ref/providers/typography/typography_v3/upscale, все — config.
# bg_removal.py — лениво импортируется chroma_remove.cutout_* (сам путь вырезки НЕ
# выполняется в режиме green_only, но файл кладём для целостности модуля).
# НЕ копируем tools/realesrgan/, out_batch/, data/, D:\800 (см. panel/PLAN.md).
COPY art_director.py batch_print.py bg_removal.py character_ref.py chroma_remove.py \
     config.py franchise_scout.py greenkey_core.py greenkey_postprocess.py \
     llm_provider.py meme_ref.py palette.py providers.py typography.py \
     typography_v3.py upscale.py ./
COPY docs/STYLE_BANK.json docs/STYLE_BANK.json

# Сама панель (app.py/orchestrator.py/settings.py/static/).
COPY panel ./panel

ENV UPSCALE=off \
    IMAGE_PROVIDER=gemini \
    ART_DIRECTOR_PROVIDER=gemini

EXPOSE 8040
CMD ["uvicorn", "panel.app:app", "--host", "0.0.0.0", "--port", "8040"]
