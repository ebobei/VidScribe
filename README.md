# VidScribe / ВидСкрайб

**VidScribe** — локальная CLI-утилита для сбора транскриптов YouTube-видео по поисковому запросу.

Ты задаёшь тему исследования, например:

```text
TP-Link Tapo C225 review
```

VidScribe находит подходящие видео, скачивает **только субтитры**, очищает текст, режет его на чанки и собирает архив, который удобно загрузить в AI для дальнейшего анализа.

Проект сделан для личного research-сценария: быстро собрать мнения, обзоры, ошибки, советы и повторяющиеся выводы из нескольких YouTube-видео, не просматривая часы контента вручную.

---

## VidScribe уже умеет:

- искать YouTube-видео по запросу;
- получать публичную metadata по видео через `yt-dlp`;
- фильтровать видео по длительности, live/upcoming, shorts и дублям;
- выбирать лучшую дорожку субтитров:
  - ручные русские;
  - авто русские;
  - ручные английские;
  - авто английские;
- скачивать только выбранный subtitle-файл;
- очищать `.vtt` / `.srt` в обычный текст;
- сохранять чистые транскрипты в `transcripts_clean/`;
- создавать RAG-ready чанки в `chunks/chunks.jsonl`;
- собирать `summary_input.md` для загрузки в AI;
- собирать итоговый `research_pack.zip`.

---

## Установка

Пример для Windows PowerShell:

```powershell
cd path\to\VidScribe

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Проверить, что CLI доступен:

```powershell
python -m app.main --help
```

---

## Первый запуск

Запуск с готовым примером конфига:

```powershell
python -m app.main collect --config ./requests/tapo_c225.yaml
```

Для быстрой проверки лучше начать с маленького лимита:

```powershell
python -m app.main collect `
  --config ./requests/tapo_c225.yaml `
  --limit 3 `
  --candidate-pool-size 10
```

Пример с сортировкой по дате:

```powershell
python -m app.main collect `
  --config ./requests/tapo_c225.yaml `
  --limit 3 `
  --candidate-pool-size 10 `
  --order date
```

Лучше всего сейчас поддерживаются `relevance` и `date`. 

---

## Конфиги запросов

Примеры лежат в папке `requests/`:

```text
requests/
  example_research.yaml
  sample_ru_en.yaml
  sample_short.yaml
```

Для нового исследования проще скопировать существующий YAML и поменять:

- `project_name`;
- `query`;
- `limit`;
- `candidate_pool_size`;
- минимальную и максимальную длительность;
- предпочитаемые языки;
- output-директорию.

Пример идеи:

```yaml
project_name: "tapo_c225_audio_research"

query: "TP-Link Tapo C225 review"

limit: 20
candidate_pool_size: 60

duration:
  min_seconds: 240
  max_seconds: 3600

languages:
  preferred:
    - "ru"
    - "en"
  allow_auto_subtitles: true
  allow_manual_subtitles: true
```

---

## Что появится после запуска

После запуска VidScribe создаёт отдельную папку в `output/`.

Пример:

```text
output/tapo_c225_audio/tapo_c225_audio_research_2026-05-22_1840/
  run.json
  manifest.json
  videos.csv
  summary_input.md
  research_pack.zip

  metadata/
    001__VIDEO_ID.json

  subtitles_raw/
    001__VIDEO_ID.ru.manual.vtt

  transcripts_clean/
    001__VIDEO_ID.ru.manual.txt

  chunks/
    chunks.jsonl

  logs/
    collect.log
```

Главные файлы:

- `summary_input.md` — файл, который удобно загрузить в AI;
- `research_pack.zip` — архив со всеми результатами запуска;
- `videos.csv` — таблица видео, статусов и причин пропуска;
- `transcripts_clean/*.txt` — очищенные транскрипты;
- `chunks/chunks.jsonl` — чанки для дальнейшей обработки;
- `manifest.json` — техническое описание пакета;
- `run.json` — параметры конкретного запуска.

---

## Как использовать результат

Обычно сценарий такой:

1. Запустить VidScribe по нужному YAML-конфигу.
2. Открыть папку результата в `output/`.
3. Взять `summary_input.md` или `research_pack.zip`.
4. Загрузить файл в AI.
5. Попросить проанализировать:
   - какие мнения повторяются;
   - какие проблемы чаще всего называют;
   - какие выводы полезны для проекта;
   - какие видео были наиболее информативными;
   - что стоит проверить отдельно.

`summary_input.md` специально сделан человекочитаемым: его можно открыть, быстро просмотреть и понять, что именно было собрано.

---

## Статусы в `videos.csv`

В таблице `videos.csv` видно, что произошло с каждым видео.

Основные статусы:

- `processed` — видео прошло фильтры, субтитры скачаны, текст очищен, чанки построены;
- `skipped / no_subtitles` — подходящих субтитров нет;
- `skipped / duration_too_short` — видео слишком короткое;
- `skipped / duration_too_long` — видео слишком длинное;
- `skipped / live_video` — live/upcoming-видео;
- `skipped / shorts` — видео похоже на Shorts;
- `skipped / duplicate` — дубль;
- `failed / yt_dlp_error` — `yt-dlp` не смог скачать subtitle-файл;
- `failed / subtitle_cleaning_error` — subtitle-файл скачан, но не удалось очистить его в текст.

---

## Что лежит в `research_pack.zip`

Архив содержит результаты конкретного запуска:

- `run.json`;
- `manifest.json`;
- `videos.csv`;
- `summary_input.md`;
- `metadata/`;
- `subtitles_raw/`;
- `transcripts_clean/`;
- `chunks/`;
- `logs/`.

Архив не включает сам себя и временные директории `yt-dlp`.

---

## Ограничения и возможные проблемы

### YouTube может временно ограничивать запросы

Иногда `yt-dlp` может получить ошибку от YouTube, например HTTP 429. Обычно это означает, что YouTube временно ограничил частоту запросов.

Что можно сделать:

- уменьшить `limit`;
- уменьшить `candidate_pool_size`;
- повторить запуск позже;
- не запускать много больших исследований подряд.

### Не у всех видео есть субтитры

Если у видео нет подходящих ручных или автоматических субтитров, оно будет пропущено со статусом:

```text
skipped / no_subtitles
```

Это нормальное поведение.

### Таймкоды в чанках best-effort

`chunks.jsonl` пытается сохранить `start_time` и `end_time` из raw subtitle-файла. Если таймкоды не удалось надёжно сопоставить, поля могут быть `null`.

---

