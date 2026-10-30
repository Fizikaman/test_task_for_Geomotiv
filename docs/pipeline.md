# Обработка revision (P14)

`RevisionPipeline.run(Attempt)` соединяет загрузку immutable source, подготовку
Markdown/PDF, extraction P11, resolver/embeddings P12 и публикацию P13.
Сервис получает зависимости явно и не импортирует Celery. `Attempt` содержит
article/revision/job ID, номер попытки и fencing token; token исключён из repr.
Вызов возвращает `PublicationResult.COMPLETED` / `SUPERSEDED` либо поднимает
`PipelineError` с безопасными `code`, `stage`, `retryable`. Cancellation
пробрасывается вызывающему коду.

## Подключение

Вызывающий код передаёт `CheckpointRepository`, `GraphPublisher`, BlobStore,
LLM с `generate_with_metadata` и `generate`, Embeddings, CandidateStore,
проверенные `LLMSettings`, `OllamaSettings`, `Limits`. Рабочие адаптеры:
`ChatCompletionsLLM`, `OllamaEmbeddings` и `Neo4jCandidates`. Владелец процесса
открывает и закрывает HTTP/Neo4j-клиенты, выполняет readiness и embedding
probe/bind P06 до обработки задач. Pipeline не создаёт fake-провайдеров
автоматически и не читает настройки или credentials из соседних проектов.

Claim, heartbeat, запись failed/retry и bounded retry scheduling реализованы
в [worker P15](worker.md). Вызывающий код обязан сохранять ошибки только с проверкой текущего
владельца lease, чтобы ошибка старой попытки не затронула новую. Сам pipeline
не переводит Job в failed и не продлевает lease. Job/revision становятся
completed атомарно с графом и active pointer через P13.

## Checkpoints

Полные результаты preparation, extraction и resolution сериализуются в
versioned JSON-конверт `checkpoint-v1`: cache key, SHA-256 payload и payload.
BlobStore сначала публикует immutable content-addressed файл. Затем короткая
Neo4j transaction сохраняет `{key, blob_hash}` в JSON-свойстве
`ArticleRevision.pipeline_checkpoints`. Отдельный schema migration не нужен.
Проверка владельца и запись указателя выполняются под Article → Job locks;
lease проверяется по `datetime.realtime()` также перед записью указателя.
Поэтому checkpoints переживают новую попытку, перезапуск процесса и явный
retry, создающий новый job_id той же revision.

Ключи включают исходный SHA-256, article/revision ID (артефакты содержат
revision-local координаты/UUID), формат, версии pipeline/этапов и Limits.
Следующие этапы включают ключ и hash результата предыдущего этапа.
Extraction дополнительно учитывает URL, text/vision model, output mode,
параметры LLM, хеши document/JSON prompts и output schema. Resolution учитывает
identity/JSON prompts и schema, embedding endpoint/model/digest/dimensions/
input version, top-k и переданный реестр типов. API key не сохраняется и не
участвует в ключе. Кэш ограничен revision; одинаковые исходные байты другой
revision не переиспользуют чужие fragment IDs.

При изменении алгоритма подготовки, объединения extraction, нормализации
или resolution нужно повысить соответствующее поле `Versions`. Изменение
embedding/identity конфигурации сохраняет возможность переиспользовать
extraction. Изменение upstream версии инвалидирует downstream checkpoints,
даже если сериализованный результат случайно совпал.

Чтение ограничено по размеру и проверяет hash файла, версию конверта, ключ,
hash payload, Pydantic schema, revision ID и версии extraction. Для PDF
дополнительно проверяются наличие, namespace, размер и hash изображений.
Отсутствующий, несовместимый или некорректный checkpoint считается cache miss.
Повреждённая metadata указателей также не используется. Storage с физически
повреждённым файлом по существующему hash требует восстановления: immutable
BlobStore P07 не перезаписывает такой файл молча.

Resolution checkpoint сохраняет candidate snapshots. Публикация повторно
проверяет их в транзакции. При candidate/character/embedding-space/direction
конфликте pipeline удаляет только resolution pointer; следующий retry сможет
повторить resolver с сохранённым extraction. Изменившийся реестр или embedding
конфигурацию владелец процесса должен передать заново; бесконечных retries нет.

## Fencing, бюджеты и ошибки

Перед чтением source, каждым LLM/embedding request, сохранением checkpoint
и публикацией проверяются принадлежность job/revision/article, latest revision,
latest job, processing status, attempt/token и живой lease. Вытесненная revision
под действующим владельцем завершается как superseded. Повтор completed той же
попытки возвращается без чтения source и без вызовов AI.

SHA-256 и размер source проверяются до обработки. PDF проходит реальную
подготовку P10 с PNG каждой страницы; vision model требуется явно до вызова
провайдера. Для всего run действует один processing timeout. Вызовы extraction,
identity и embeddings вместе расходуют `llm_calls`, `llm_total_input_bytes`;
cache hits бюджет не расходуют. Учитываются UTF-8 текст, JSON instruction/schema
и base64 изображения, без HTTP framing. На каждый вызов действует
`provider_timeout_seconds`; внутренние ограничения P11/P12 также сохраняются.

Transient LLM/embedding ошибки, timeout и временные ошибки Neo4j/файлового I/O
классифицируются retryable. Некорректные данные, исчерпанные бюджеты,
ошибки конфигурации/валидации и утрата fencing — permanent для данной попытки.
Сообщения исключений провайдеров/драйвера и содержимое документов не передаются
в `PipelineError`. При сбое до успешной публикации прежняя active не меняется.

Проверка fencing перед внешним запросом ограничивает лишнюю работу, но не
атомарна с внешним API. Crash после принятия запроса/ответа провайдера и до
durable checkpoint может привести к повторному вызову и повторной оплате.
Сбой между BlobStore и Neo4j оставляет допустимый неподключённый файл.
Нет общей транзакции между API, файловой системой и БД.

Отмена `asyncio.to_thread` не убивает уже начатую синхронную DB transaction:
при timeout/cancellation во время публикации её результат может оказаться
committed. Worker P15 сверяет durable Job status и соблюдает fencing при
записи failed/retry; повтор completed уже поддержан P13/P14. Нельзя считать
timeout доказательством rollback или отсутствия оплаты.

## Проверки

`uv run --frozen pytest tests/processing/test_pipeline.py --neo4j` использует только явные
fake LLM/embeddings и настоящую изолированную Neo4j из
`docker-compose.test.yml`. Проверяются публикация и повтор completed, сбои
этапов, сохранение прежней active, возобновление после создания нового сервиса,
ручной retry с новым Job, несовместимость конфигурации/версий, повреждённые
checkpoints, fencing до/после вызова, общий бюджет, timeout/cancellation,
PDF с image и обязательной vision model, invalidation resolution после
конфликта и crash window. Отдельный subprocess проверяет lazy imports Neo4j,
httpx/PDFium и отсутствие Celery при импорте pipeline.

Реальные LLM/Ollama, Linux-прогон P14, нагрузка, аварийное отключение питания
и настоящий worker/heartbeat не проверялись в этом шаге.
