# P10: текст и изображения PDF

`await prepare_pdf(path, revision_id=..., limits=..., store=...)` возвращает
`PreparedPdf`. В `pages` для каждой страницы доступны 1-based `page`, `text`,
`source`, `image` (Blob), `artifact_ref` вида `artifacts/<sha256>` и `metadata`:
размер в пикселях и PDF points, DPI, media type и renderer version.
`prepared` содержит P09 SourceText/TextChunk с локальными offsets страниц,
стабильными IDs и общими бюджетами текста/фрагментов. Пустые страницы и сканы
сохраняются с пустым текстом и полноценным PNG; OCR здесь не выполняется.
Текст и страницы исключены из repr там, где они могли бы раскрыть содержимое.

```python
from article_graph.documents.blob_store import BlobNamespace
from article_graph.documents.pdf_preparation import prepare_pdf

result = await prepare_pdf(
    immutable_source_path,
    revision_id=revision_id,
    limits=settings.limits,
    store=artifact_store,
)
for page in result.pages:
    with artifact_store.open(BlobNamespace.ARTIFACTS, page.image.content_hash) as image:
        pass  # Чтение/передачу image bytes также выполнять вне event loop.
```

Входной путь должен принадлежать приложению и оставаться immutable во время
обработки. Клиентские пути и имена сюда не передаются. Для artifacts нужен
экземпляр LocalBlobStore с подходящим `max_bytes`: PNG может быть больше
исходного PDF. Например, для default page_pixels достаточно 16 MiB на artifact.
Лимит загрузки исходника остаётся отдельным (`upload_bytes`).

## Выбор библиотеки и изображение страницы

Используется уже зафиксированный `pypdfium2==5.14.0`, без новых зависимостей.
PDFium извлекает текст и рендерит всю видимую страницу в RGB PNG: текст,
векторные линии, растровые изображения, аннотации и формы. Crop, извлечение
только embedded images и эвристика «есть текст — не нужен image» отсутствуют.
Поворот и дробные размеры учитываются; размеры округляются вверх до пикселя.
При превышении бюджета документ отклоняется целиком, без уменьшения DPI
или отбрасывания хвоста. PNG записывается через stdlib zlib/struct, без Pillow.

Отдельный pypdf не нужен: второй парсер добавил бы зависимость, а нужное
извлечение уже есть в PDFium. Его native wheel, текст и PNG проверены на обычном
CPython 3.15.2 в Windows и `python:3.15.2-slim` Linux x86_64.
Это проверка используемых функций, а не гарантия для любого PDF или платформы.
API и ограничения потоков описаны в
[официальной документации PDFium](https://pypdfium2.readthedocs.io/en/stable/python_api.html).

## Процесс и бюджеты

Parser, text extraction, chunking и renderer работают в новом subprocess.
PEP 810 lazy imports загружают PDFium только в дочернем процессе после установки
Linux `RLIMIT_AS`; импорт API/orchestrator его не загружает. `RLIMIT_CORE=0`
отключает core dumps. Stdout/stderr дочернего процесса не логируются; протокол
ошибок содержит только безопасный ErrorCode. Provider settings не читаются,
сетевых/LLM-вызовов нет.

| Limits | Default | Назначение |
| --- | ---: | --- |
| upload_bytes | 2097152 | Исходный файл |
| pdf_pages | 20 | Все страницы, без усечения |
| pdf_dpi | 144 | DPI, допускается 1–600 |
| page_pixels | 4000000 | На страницу, до выделения bitmap |
| pdf_total_pixels | 40000000 | На весь документ |
| pdf_memory_bytes | 536870912 | Linux virtual address space subprocess |
| pdf_timeout_seconds | 30 | Ожидание subprocess, включая импорт и обработку |
| prepared_chars / chunks | 160000 / 20 | Текст всего документа с overlap |

Все значения доступны через `ARTICLE_GRAPH_LIMITS__...`.
Wall timeout и отмена coroutine убивают и дожидаются завершения процесса.
Ожидание процесса асинхронное, файловый IO/публикация вынесены в thread.
Временные PNG/manifest удаляются после обработки. Только после успешной
обработки всего документа PNG публикуются immutable в BlobStore.
Ошибка публикации может оставить непривязанные полные artifacts; транзакции
между filesystem и БД и GC относятся к дальнейшей инфраструктуре.

Повреждённые и зашифрованные PDF (включая пустой user password) дают
`invalid_document`, переполнение бюджета — `limit_exceeded`, timeout —
`processing_timeout`. Native termination без error manifest рассматривается
как `limit_exceeded`; это безопасная классификация, а не диагностика причины
сигнала. Память на Windows не ограничивается RLIMIT_AS; целевая гарантия — Linux.
Лимиты ресурсов не заменяют контейнерную изоляцию от эксплуатации native parser.

PDF-проверка при ingestion в `FormatValidator` также выполняется в ограниченном
subprocess, без извлечения/рендеринга. `LocalBlobStore(..., limits=...)` передаёт
ей настроенные бюджеты. Синхронный BlobStore в будущем HTTP upload следует
вызывать вне event loop; сам endpoint относится к P16.

## Проверки

Windows: `uv sync --frozen`, `uv run --frozen ruff check .`,
`uv run --frozen ruff format --check .`, `uv run --frozen pyrefly check`,
`uv run --frozen pytest`. Использован локальный uv 0.13.6.

Целевой набор: `uv run --frozen pytest tests/documents/test_pdf_preparation.py
tests/documents/test_preparation.py tests/documents/test_blob_store.py`. Синтетические fixtures
включают текстовый PDF, скан, векторную стрелку с текстом, пустые страницы,
поворот, дробный/огромный размер, повреждение и настоящий encrypted R2 PDF.
Проверяются PNG CRC/размеры/пиксели, наличие вектора, номера страниц,
артефакты, offsets, лимиты, реальные kill/reap по timeout/cancel,
отзывчивость event loop и runtime lazy imports.

Повторяемый Linux-прогон из корня проекта (PowerShell), без `.env`, Git и томов БД:

```powershell
$projectRoot = (Get-Location).Path
docker run --rm --name geomotiv-p10-check --memory=1g --cpus=2 --pids-limit=128 `
  --mount "type=bind,source=$projectRoot/src,target=/input/src,readonly" `
  --mount "type=bind,source=$projectRoot/tests,target=/input/tests,readonly" `
  --mount "type=bind,source=$projectRoot/pyproject.toml,target=/input/pyproject.toml,readonly" `
  --mount "type=bind,source=$projectRoot/uv.lock,target=/input/uv.lock,readonly" `
  python:3.15.2-slim sh /input/tests/check_pdf_linux.sh
```

Скрипт копирует только эти файлы во временный filesystem контейнера, ставит
uv 0.13.6, выполняет frozen sync, Ruff/Pyrefly и целевые тесты. Linux-проверка
реально ограничивает address space и подтверждает MemoryError при превышении.
Полный стек, Neo4j, paid API и последующий Vision/extraction здесь не запускались.
