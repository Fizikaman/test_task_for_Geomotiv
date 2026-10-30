# P11: извлечение документа

`article_graph.extraction.extract_document` принимает `PreparedDocument` для
Markdown или `PreparedPdf`, `ExtractionProvider`, `BlobStore` и `Limits`.
Возвращает `ExtractionArtifact` и immutable `Blob` сохранённого JSON.
HTTP/worker, глобальная идентичность и публикация графа здесь не подключаются:
это следующие шаги P12–P15.

Для каждого текстового chunk создаётся один вызов. PDF всегда отправляет
изображение всей соответствующей страницы вместе с локальным текстом chunk;
длинная страница повторяет image в каждом своём вызове. Пустая страница также
получает один вызов с image. Markdown отправляет только текст, URL не загружаются.
До обращения к провайдеру проверяются полное покрытие sources chunks,
соответствие текстовых срезов и PDF pages, уникальность fragment IDs и бюджеты.
Страница image имеет fragment_id `page-N`, текст сохраняет исходный chunk ID.

Промпты: [system v1](../src/article_graph/prompts/document-system.v1.txt) и
[user v1](../src/article_graph/prompts/document-user.v1.txt). Общая версия —
`document-v1`; формат артефакта — `extraction-v1`. Текст документа находится
в JSON envelope внутри user message; image — отдельный image block. Системные
правила требуют считать документ недоверенными данными, игнорировать его
инструкции, не использовать внешние знания и не превращать отрицания,
предположения или отсутствие информации в положительные связи.

Pydantic отклоняет неполный JSON, лишние поля, неизвестные endpoints и polarity,
отличную от `affirmed`. Дополнительно text evidence обязано содержать точную
подстроку отправленного chunk с его fragment_id и page. Visual evidence имеет
отдельный kind и description, указывает только отправленные page-N и page.
Цитата другой страницы или chunk, даже существующая в документе, отклоняется.
Проверки доказывают происхождение цитаты и привязку image; семантическая
корректность утверждений и визуального описания зависит от модели. Fake-тесты
проверяют контракт и размещение инструкций, а не качество реального LLM.

Объединение детерминировано порядком chunks, независимо от порядка ответов.
Локальные IDs провайдера перенумеровываются в c0/c1 и r0/r1. Два персонажа
объединяются только при точном совпадении имени и описания, отсутствии
неоднозначных дубликатов и общей уникальной текстовой цитате, связанной с этим
персонажем, по одинаковым координатам исходного source. Цитата должна содержать
имя или alias. Aliases объединяются без повторов. Совпадение имени/alias,
перекрытие chunks само по себе, image evidence или отсутствие общего текстового
evidence не дают автоматического слияния. Такие персонажи остаются для P12.

Связи объединяются по локальным концам, kind и directed; для симметричных
концы упорядочены. Сохраняются все разные evidence с их страницами и fragment IDs.
Confidence объединённой связи — минимум, либо null при неизвестном значении.
Если результат превышает characters/relations/evidence_per_relation, документ
отклоняется целиком; evidence не обрезается. Нормализация видов связей — P12.

Артефакт сохраняет revision/preparation/prompt version, итоговый Extraction,
исходный Extraction каждого вызова, fragment/page, requested_model, сообщённую
провайдером model и usage (prompt/completion/total tokens). Если провайдер не
сообщает model или usage, сохраняется null: значения не придумываются.
`ChatCompletionsLLM.generate_with_metadata` сохраняет прежний API generate/extract.
Дополнительные provider-specific usage fields игнорируются.

Новые ограничения с env override `ARTICLE_GRAPH_LIMITS__...`:

| Поле | Default | Смысл |
| --- | ---: | --- |
| llm_concurrency | 2 | Число одновременно выполняемых вызовов на документ |
| llm_input_bytes | 16000000 | UTF-8 system/user envelope + base64 images на вызов |
| llm_total_input_bytes | 64000000 | Сумма того же объёма всех вызовов документа |

Это бюджеты содержимого; транспортная JSON-обёртка и добавляемая адаптером
JSON schema в них не входят. Они не заменяют token/context limits провайдера.
Все изображения читаются с ограничением, без доверия размеру в blob metadata.
Существующие chunks, llm_calls, chunk_chars, prepared_chars ограничивают вход;
превышение обнаруживается до первого вызова, без частичного результата.
Каждый вызов делает один HTTP attempt, retries отсутствуют. processing_seconds
ограничивает фазу LLM-вызовов; timeout, ошибка и отмена отменяют и дожидаются
остальных задач. Общий retry/lease budget последующего worker остаётся P15.
Гарантия exactly-once платных вызовов при падении процесса не предоставляется.

Проверки запускаются через uv 0.13.6 с frozen lock на обычном CPython 3.15.2:
`uv run --frozen pytest tests/test_extraction.py tests/test_llm.py`.
Тесты используют только deterministic `httpx.MockTransport`; платных вызовов нет.
Проверяются также runtime lazy imports HTTP/PDF/Neo4j, сохранённый JSON,
metadata, overlap/ambiguity/aliases, symmetric endpoints, бюджеты, bounded
concurrency и cleanup после failure/timeout/cancellation.
