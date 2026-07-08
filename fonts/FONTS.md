# FONTS.md — шрифты print-factory-nb

Все шрифты — Google Fonts, лицензия **OFL (Open Font License) 1.1**, если не указано
иное. Скачаны как есть с `raw.githubusercontent.com/google/fonts/main/...` (без
модификаций), лежат в этой папке рядом с этим файлом.

Проверены реальным рендером PIL (строка на прозрачный канвас, непустая альфа) перед
включением в проект.

## v1/v2 (уже были в проекте, не менялись)

| Файл | Семейство | Лицензия | Роль в коде |
|---|---|---|---|
| `Anton-Regular.ttf` | Anton | OFL 1.1 | `caps` — капс-гротеск (v1 `anton`, v2 `under`/`punch`, v3 подвал/цитата дефолт) |
| `ArchivoBlack-Regular.ttf` | Archivo Black | OFL 1.1 | `archivo` — v1 `tag`, v2 `under` |
| `Bangers-Regular.ttf` | Bangers | OFL 1.1 | `bangers` — v1 `comic` |
| `NotoSansJP[wght].ttf` | Noto Sans JP (variable) | OFL 1.1 | `notojp`/`jp` — катакана/кандзи, ось `wght` фиксируется на 900 (Black) кодом (`font.set_variation_by_axes([900])`) |

## v3 (новые, этот заход)

| Файл | Семейство | Лицензия | Источник (raw) | Роль в коде |
|---|---|---|---|---|
| `PermanentMarker-Regular.ttf` | Permanent Marker | Apache License 2.0 | `apache/permanentmarker/PermanentMarker-Regular.ttf` | `quote` — цитата, кистевой/маркерный характер (дуотон-цитатный mood) |
| `CaveatBrush-Regular.ttf` | Caveat Brush | OFL 1.1 | `ofl/caveatbrush/CaveatBrush-Regular.ttf` | `quote_alt` — запасной вариант цитаты, более "кисть" |
| `PlayfairDisplay[wght].ttf` | Playfair Display (variable) | OFL 1.1 | `ofl/playfairdisplay/PlayfairDisplay[wght].ttf` | `display` — фэшн-эдиториал заголовок, ось `wght` фиксируется на 900 (Black) кодом |
| `Cinzel[wght].ttf` | Cinzel (variable) | OFL 1.1 | `ofl/cinzel/Cinzel[wght].ttf` | `gothic` — трэш/хоррор-подвал/цитата, ось `wght` фиксируется на 900 (Black) кодом |
| `UnifrakturCook-Bold.ttf` | UnifrakturCook | OFL 1.1 | `ofl/unifrakturcook/UnifrakturCook-Bold.ttf` | `gothic_heavy` — запасной явно готический вариант (только крупные акцентные слова, нечитаем мелко — НЕ используется в подвале) |

### Важное решение по Cinzel/Playfair Display

В стайлгайде указаны имена файлов `PlayfairDisplay-Black.ttf`/`Cinzel-Black.ttf`
(статический Black-вес). На момент скачивания в `google/fonts` для ОБОИХ семейств в
репозитории лежит только **variable font** (`ofl/cinzel/Cinzel[wght].ttf`,
`ofl/playfairdisplay/PlayfairDisplay[wght].ttf`) — папок `static/` со статическими
вариантами в этих двух семействах в текущем срезе репозитория нет (проверено
`raw.githubusercontent.com` напрямую, оба статических пути дали 404). Решение: скачаны
variable-файлы, вес Black (900) фиксируется в коде (`typography.py::_font`) тем же
приёмом, что уже используется для `NotoSansJP[wght].ttf` — `set_variation_by_axes([900])`
в try/except (не критично, если у конкретного файла нет оси `wght`).

### UnifrakturCook — зачем оставлен, но не используется в подвале

Стайлгайд явно предупреждает: UnifrakturCook — "только для акцентных крупных слов,
нечитаем мелко". Подвал (`collection_footer`) в v3 всегда мелкий (`0.028 fig_w`) — для
него используется `Cinzel` (роль `gothic`), не `UnifrakturCook`. Файл скачан и
проверен рендером про запас (в стайлгайде упомянут как допустимая альтернатива), но
`typography_v3.py` его пока нигде не вызывает — только `Cinzel` под ролью `gothic`.

## Проверка (реальный рендер PIL)

Каждый шрифт проверен скриптом: строка текста на прозрачный RGBA-канвас через
`ImageDraw.text`, затем `getchannel("A").getbbox()` — bbox должен быть не `None`, и
количество непрозрачных пикселей > 50. Все 10 шрифтов (v1/v2 + v3) прошли проверку.
