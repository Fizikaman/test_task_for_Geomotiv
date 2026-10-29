# P09: подготовка текста

`prepare_markdown(text, revision_id=..., limits=...)` возвращает immutable
`PreparedDocument`: revision, version `text-v1`, исходные `SourceText` и
адресуемые `TextChunk`. IO, HTTP, LLM и чтения ссылок нет. Исходные UTF-8 байты
остаются в BlobStore P07; здесь сохраняется точная Unicode-строка без trim,
нормализации CRLF, BOM или Unicode. Разметка, HTML, URL, alt text, подписи,
таблицы и fenced code остаются текстом: это вход extraction, не HTML-renderer.
Содержимое текста исключено из repr объектов и ошибок подготовки.

`start` включителен, `end` исключителен: `chunk.text == source.text[start:end]`.
Offsets — индексы Unicode code points Python, не UTF-8 bytes и не UTF-16 units.
Для Markdown `source_id=markdown`, `page=null`. `prepare_sources` принимает
упорядоченный tuple уникальных источников с `source_id`, текстом и optional
1-based page. Каждый фрагмент принадлежит одному источнику: overlap не пересекает
страницы, offsets локальны странице. Пустые страницы сохраняются в sources;
документ без текста этот интерфейс отклоняет по умолчанию. PDF-preprocessor P10
явно передаёт `allow_empty=True`, сохраняя страницы сканов с PNG и без chunks.
Артефакты страниц и бюджеты описаны в [PDF-preprocessor](pdf.md).

Границы выбираются детерминированно: последний конец абзаца в допустимом
окне, затем конец строки (включая строку таблицы), затем whitespace.
Окно начинается с половины chunk_chars и после предыдущего end, чтобы
продвигаться вперёд. Без подходящих границ используется точный hard split;
очень длинная строка таблицы или код могут пересечь несколько chunks, но
ни один символ не удаляется. Следующий start берётся не раньше
`end - chunk_overlap_chars`, при возможности сдвигается вперёд к whitespace.
Overlap ограничен сверху, может быть короче или нулевым. Последний chunk
всегда доходит до конца своего source. Полные таблицы внутри одного chunk
при произвольном размере не гарантируются.

ID — `chunk-` плюс SHA-256 от version, revision UUID, source identity с длиной,
page, SHA-256 полного исходного текста и offsets. Повтор того же источника
в той же revision и с теми же границами даёт те же IDs между процессами.
Смена revision, source, page, текста или границ меняет ID; смена алгоритма
требует новой version. ID пригоден для `Evidence.fragment_id` из P02.

Новые `Settings.limits` (доступны через `ARTICLE_GRAPH_LIMITS__...`):

| Поле | Default | Значение |
| --- | ---: | --- |
| chunk_chars | 8000 | Максимум code points в одном фрагменте |
| chunk_overlap_chars | 800 | От 0 до chunk_chars − 1 |
| prepared_chars | 160000 | Сумма длин всех chunks, включая overlap |

Существующий `chunks=20` ограничивает число фрагментов всего документа.
До разбиения проверяется также сумма исходных длин против prepared_chars.
Это бюджет текста, а не обещание числа токенов конкретной модели; бюджет
LLM-вызовов и объединение extraction относятся к P11.
Превышение любого бюджета даёт безопасный `DocumentError(limit_exceeded)`
без частичного результата. Пустой/пробельный Markdown — validation_error,
бинарные control characters — invalid_document. Хвост не обрезается.

Проверки: `uv run --frozen pytest tests/test_preparation.py tests/test_domain.py
tests/test_app.py`. Проверяются RU/EN, CRLF/BOM/emoji/combining characters,
таблицы, границы, hard splits, восстановление всего текста, overlap, общие
лимиты страниц, ошибки, ID в отдельных процессах и отсутствие сетевого IO.
Runtime-проверка подтверждает, что подготовка не загружает PDFium/httpx/Neo4j;
новых тяжёлых зависимостей и lazy imports в P09 нет.
