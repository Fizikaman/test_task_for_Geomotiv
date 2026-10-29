# Neo4j P03

Используется официальный Python driver 6.4.1 и тестовый сервер
`neo4j:5.26.0-community`, без Enterprise, APOC и других плагинов.
`init-schema` идемпотентен, ждёт индексы ограниченное время и затем проверяет
их метаданные. Несовместимая существующая схема не удаляется и не мигрируется
автоматически. DDL выполняется отдельными транзакциями: после частичного сбоя
допустим повторный запуск. При конфликте требуется явная миграция оператором.

Уникальны `Article.article_id`, `ArticleRevision.revision_id`,
`Character.character_id`, `RelationAssertion.relation_id`, `Job.job_id`,
`Outbox.outbox_id`, `RefreshSession.session_id`, `RefreshSession.token_hash`,
`IdempotencyRecord.idempotency_id`. Составные uniqueness constraints:
`ArticleRevision(article_id, number)` и `IdempotencyRecord(app_id, key)`.
Таким образом один ключ разрешён разным приложениям, а одинаковый номер —
разным статьям. Имена персонажей не уникальны; retry допускает несколько Job
для одной revision. Outbox хранит `job_id`, но не ограничивает число доставок.

Range indexes: `Article.latest_source_hash` (хеш последнего входа, не активной
успешной версии), `Job(status, lease_expires_at)`, `Outbox(status, available_at)`,
`RefreshSession.family_id`, `ArticleRevision.article_id`,
`RelationAssertion.revision_id`. Имена свойств — контракт для будущих репозиториев.
Уникальные constraints имеют собственные backing range indexes.

P06 добавляет uniqueness `EmbeddingSpace.index_name`: привязка vector index
к модели, digest, размерности и версии подготовки входа. После обновления нужно
повторить `init-schema`; затем выполнить embedding probe/init. Подробности и
отказ миграции неизвестных векторов — [embeddings.md](embeddings.md).

`Character.embedding` — обычный список чисел, не тип `VECTOR`.
`character_embedding` — настоящий VECTOR index, cosine, размерность из
`Neo4jSettings.embedding_dimensions` (1024 для bge-m3). `validate_embedding`
проверяет длину, числовой тип, конечность и ненулевую норму и возвращает
`list[float]`; будущие записи должны использовать эту проверку. Community
uniqueness constraints не гарантируют существование/тип свойств: обязательные
поля проверяются приложением. Прямой Cypher может обойти эту валидацию.

Lifecycle открывает driver, проверяет соединение и readiness до запуска API,
закрывает его при shutdown и ошибке readiness. Сессии создаются для операций,
не разделяются между потоками. Синхронный драйвер вызывается через threadpool,
не блокируя async event loop. `/health/ready` проверяет текущую схему; ошибки
возвращаются без сообщений драйвера и содержимого БД. Обработка заданий ещё не
реализована: в P15/P16 необходимо применить тот же `require_ready` на старте.

Проверки на реальной БД: повторный init и CLI, все uniqueness constraints,
составная уникальность, rollback всей транзакции, ONLINE, top-k по синтетическим
1024-мерным векторам со scores 1.0/0.9, несовпадение размерности, отсутствие
индекса и runtime readiness/lifecycle. Векторный тест использует bounded polling
для видимости committed данных в ANN-индексе. LLM и Ollama не вызываются.

Источники: [uniqueness constraints](https://neo4j.com/docs/cypher-manual/5/constraints/create-constraints/),
[vector indexes и LIST embeddings](https://neo4j.com/docs/cypher-manual/5/indexes/search-indexes/vector-indexes/),
[официальный драйвер](https://github.com/neo4j/neo4j-python-driver).
