# Ollama embeddings — P06

`article_graph.embeddings.OllamaEmbeddings` реализует типизированный контракт
`Embeddings.embed(Sequence[str]) -> EmbeddingBatch`. Порядок результатов совпадает
с порядком входного batch. Metadata содержит полное имя модели с tag, digest
манифеста, размерность и `input_version=raw-text-v1`. Текст не нормализуется.
Metadata и векторы в результате неизменяемы; содержимое векторов скрыто из repr.

Конфигурация `OllamaSettings`: адрес, модель, digest и размерность обязательны,
без автоматического выбора модели. Начальный пример — `bge-m3:latest`, 1024.
Digest берётся из установленной модели через `/api/tags`; он не выдумывается
и не заменяется именем tag. `.env.example` содержит только placeholder digest.
`timeout_seconds=30`, `max_response_bytes=4000000`, `max_batch_size=100` ограничены
положительными значениями. Некорректная конфигурация отклоняется до HTTP.

Для потребителей БД обязателен lifecycle-контекст:

```python
async with open_embeddings(ollama_settings, driver, neo4j_settings) as embeddings:
    batch = await embeddings.embed(["Алиса: врач", "Боб: учитель"])
```

Один HTTP-клиент живёт весь контекст процесса; enter проверяет установленный
digest и делает настоящий `/api/embed` probe. Поле `dimensions` в запрос
не передаётся: ожидаемая размерность подтверждается выходным вектором.
`input` всегда массив, `truncate=false` всегда явно задан. Каждый batch проверяет
имя модели в ответе, количество векторов, размерность каждого, числовые значения
без bool/строк/null, конечность и наличие ненулевой компоненты. Пустой batch,
пустые тексты и превышение размера batch отклоняются до HTTP.

Digest проверяется до и после каждого embed. Ollama не возвращает digest в
ответе `/api/embed`, поэтому эти запросы не дают атомарной защиты от изменения
tag и его обратной замены внутри одного вызова. Работающий экземпляр Ollama
должен обслуживать неизменяемую модель: pull/copy/delete и смена tag выполняются
при остановленных потребителях. Обнаруженная смена digest завершает вызов ошибкой,
а новый digest требует отдельного пространства/явной миграции. Загрузка модели
не выполняется адаптером и не входит в unit-тесты.

`open_embeddings` после probe проверяет настоящую Neo4j schema readiness и
атомарно привязывает singleton `EmbeddingSpace(index_name=character_embedding)`
к полным metadata. Unique constraint и запись lock сериализуют конкурентный init.
Другая модель, digest, версия подготовки или размерность отклоняются; существующие
векторы без регистрации пространства тоже отклоняются, metadata не угадываются.
Повторный init с теми же metadata безопасен. Изменений/пересоздания vector index
адаптер не делает. После ошибки транзакция откатывается, HTTP-клиент закрывается.

Реальная публикация Character будет реализована в P13: она должна сохранять
metadata результата и использовать только зарегистрированное пространство.
Прямые Cypher-записи могут обойти защиту приложения; Community не даёт property
constraints для проверки всех векторов. Нельзя удалять/менять EmbeddingSpace,
пока существуют соответствующие векторы или работают их потребители.

Отдельный init после `init-schema` (переменные из `.env.example` необходимо
явно экспортировать в окружение; команда не читает `.env`):

```powershell
uv run --frozen python -m article_graph.embeddings
```

Текущий HTTP API пока обслуживает auth/health и не использует embeddings.
`Settings.ollama` допускает отсутствие конфигурации для таких процессов, но при
наличии проверяется eagerly, включая совпадение размерности с Neo4j settings.
Embedding init и будущие worker-потребители требуют `OllamaSettings` и выполняют
probe/binding до приёма работы. Подключение pipeline/worker относится к P14/P15.

HTTP-клиент не следует redirect, не использует proxy из окружения и не повторяет
запросы. Timeout/transport, 408/429 и 5xx классифицируются retryable для будущего
worker. Остальные HTTP ошибки и некорректные ответы — permanent. Ошибки содержат
только фиксированный код, без response body, документов и HTTP exception chain.

Проверки:

```powershell
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyrefly check
docker compose -f docker-compose.test.yml up -d --wait
uv run --frozen pytest --neo4j
docker compose -f docker-compose.test.yml stop
```

HTTP mocks проверяют payload, probe, batch, ошибки, ограничения, смену модели,
закрытие и повторное использование клиента. Настоящая изолированная Neo4j
проверяет binding, повторный init, конкуренцию, rollback, существующие векторы
без metadata и сохранение 1024-мерных тестовых векторов. Регрессия P03 проверяет
настоящий ANN top-k и отказ изменения размерности существующего индекса.
Runtime-тест в чистом CPython проверяет отложенную загрузку httpx/Neo4j.
Настоящие bge-m3/Ollama не запускались; качество embeddings не оценивалось.

Официальный контракт: [embed](https://docs.ollama.com/api/embed),
[список моделей и digest](https://docs.ollama.com/api/tags).
