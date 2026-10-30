# Решения и границы проекта

Это обоснования текущего кода, а не сравнительные результаты производительности.
Фактические прогоны собраны в [P23 validation](p23-validation.md),
[Docker](docker.md), [P20](integration.md), [P21](e2e.md) и [P22](live-smoke.md).
Внешние источники ниже проверены 30 октября 2026; текущий deployment закрепляет
Neo4j 5.26.0 Community, а не автоматически обновляемую версию из документации.

## Neo4j, Memgraph, Kuzu

| Вариант | Почему подходит / компромисс для этого проекта |
| --- | --- |
| Neo4j Community | Реализованы и проверяются отдельный server, Python driver, parameterized Cypher, uniqueness/range/vector indexes и атомарная публикация. Один движок хранит граф, metadata задач и identity candidates. Community deployment одиночный. |
| Memgraph | Имеет Cypher и [native vector search](https://memgraph.com/docs/querying/vector-search). Это реальная альтернатива, но DDL, ANN procedure, locking/retry semantics и readiness текущего кода привязаны к Neo4j. Совместимость и ресурсы Memgraph здесь не измерены; перенос потребует адаптации и тех же failure-тестов. |
| Kuzu | [Embedded graph database](https://github.com/kuzudb/kuzu) с Cypher, vector search и MIT license. Embedded-вариант интересен для одного процесса/аналитики, но текущая схема имеет независимые API, worker и dispatcher. Multi-process write/recovery и CPython 3.15 совместимость не проверены. Исходный репозиторий архивирован 10 октября 2025; это дополнительный риск сопровождения, а не утверждение о непригодности всех forks. |

Выбор Neo4j уменьшает число непроверенных предположений в уже реализованном
контуре. Из него не следует превосходство по latency, памяти, стоимости или
скорости графовых запросов: одинакового workload и сравнительных измерений нет.

Для отказоустойчивого self-managed кластера Neo4j нужен переход к Enterprise:
[clustering](https://neo4j.com/docs/operations-manual/current/clustering/) относится
к Enterprise Edition. **Цена Enterprise — лицензионный компромисс кластерного
пути:** коммерческая лицензия/подписка добавляет затраты и условия эксплуатации;
конкретная сумма не оценивалась и должна запрашиваться у поставщика для выбранного
развёртывания. Наличие бесплатной Community не означает бесплатный production
кластер. См. [лицензирование Neo4j](https://neo4j.com/open-core-and-neo4j/).
Кластер в этом проекте не развёрнут и не протестирован.

## Зачем vector index и Ollama/bge-m3

`Character.embedding` и cosine index `character_embedding` дают top-k кандидатов
для resolver. ANN score — подсказка для поиска, не evidence тождества. Решение
проверяет имена/alias, контекст и вердикт; неоднозначность не разрешается одним
порогом similarity. Snapshot кандидата перепроверяется при publication.
См. [resolver](../src/article_graph/resolution.py) и
[candidate queries](../src/article_graph/candidate_repository.py).

Ollama предоставляет локальный HTTP lifecycle embeddings, не требует отдельного
платного embedding API и не связывает код с Python ML runtime. bge-m3 выбран как
многоязычная модель: свойства описаны в [model card BAAI](https://huggingface.co/BAAI/bge-m3).
Используется только dense 1024-vector через `/api/embed`, не все режимы model card.
Tag/digest/dimensions закреплены; init сверяет manifest и настоящий ненулевой finite
vector с индексом. Смена embedding space — миграция, не скрытая подмена модели.
Нет собственного retrieval benchmark, доказательства лучшего качества bge-m3
или гарантии достаточного CPU throughput. Реальный local embedding проверялся
в P19; полный real-provider quality smoke P22 остаётся открыт.

## Celery, outbox и lease

API сначала сохраняет immutable source, затем одной DB transaction регистрирует
ArticleRevision + Job + Outbox. Принятый HTTP 202 не зависит от доступности Redis.
Dispatcher отдельно claim-ит Outbox, отправляет JSON task с job UUID и отмечает
sent; recovery возвращает queued/expired Jobs к доставке. Между Neo4j и Redis
нет общей транзакции, поэтому контракт — at-least-once, а не exactly-once.

Celery даёт отдельные worker processes, broker delivery и bounded retries.
Durable Job claim, DB-time lease и attempt fencing token определяют владельца
обработки; heartbeat продлевает живую lease, старый владелец не может опубликовать
новый результат. Дубликаты terminal Jobs не запускают pipeline. Отдельный
processing budget защищает от бесконечных attempts. Это проверено fault-тестами,
но не даёт SLA восстановления при произвольной длительности outage.
Crash после платного запроса и до durable checkpoint может повторить оплату.
Результат timeout при уже начатой DB transaction требует сверки Job, а не
предположения о rollback. См. [worker](worker.md) и [dispatcher](dispatcher.md).

## Provenance и публикация версий

Связь представлена `RelationAssertion`, чтобы сохранить article/revision UUID,
оба endpoint UUID, canonical kind/direction и evidence. Text evidence содержит
fragment/page и точную цитату, visual — page/fragment и описание видимого.
LLM confidence не превращает отрицание или предположение в affirmed факт.
Assertions разных статей не уничтожаются при совпадении endpoints.

Подготовка/extraction/resolution сохраняют versioned, content-addressed checkpoints.
Ключи включают версии этапов, конфигурацию, hashes промптов/schema; API key исключён.
Публикация атомарно записывает graph, completed status и active pointer после
проверки latest revision/job/attempt/lease. Pending/failed update сохраняет старую
active версию. API читает только active assertions; история остаётся в БД.
Валидация provenance ограничивает структуру/координаты/цитаты, но не доказывает
семантическую истинность интерпретации LLM. BlobStore и Neo4j не имеют общей
transaction: возможны orphan files, автоматического GC пока нет.

## CPython 3.15 и lazy imports

Обычный CPython 3.15.2 и uv 0.13.6 закреплены по требованиям проекта, зависимости
устанавливаются через frozen lock. Явные PEP 810 lazy imports откладывают optional
Neo4j/HTTP/PDF/worker зависимости до использования; DTO/router/task registration
и обязательная startup validation остаются eager. Глобальный lazy mode, JIT и
free-threaded build не используются. Ruff/Pyrefly проверяют синтаксис и типы,
отдельные runtime subprocess tests — фактический состав импортов. Из этого не
следует измеренное ускорение старта: такого benchmark нет.

## Ограничения и следующий путь масштабирования

Текущий Compose — один API, один prefork worker с concurrency 1, один dispatcher,
одиночные Neo4j/Redis/Ollama и локальный общий blob volume. Это не HA-конфигурация;
нет cluster/load/chaos/power-loss benchmark, production backup/restore процедуры
или измеренной пропускной способности. Offset pagination не даёт snapshot.
Registry/история Jobs, Outbox, refresh и blobs требуют отдельной retention политики.
Авторизация рассчитана на настроенное приложение; multi-tenant isolation, quotas
и мгновенный отзыв выданных Access не реализованы. Данные уходят выбранному LLM;
его privacy, quota, стоимость и точность нужно оценивать отдельно.

Предлагаемый, **ещё не реализованный и не измеренный** путь:

1. Собрать latency/queue depth/error/retry/LLM cost метрики и replay workload,
   отдельно оценить semantic quality, ANN recall, память и узкие места транзакций.
2. Подобрать worker concurrency под provider quotas, CPU PDF/Ollama и DB locking;
   проверить несколько workers/dispatchers на тех же сценариях recovery.
3. Для нескольких хостов выбрать совместимое общее хранилище immutable blobs,
   добавить backup/restore и retention, затем проверить все crash windows.
4. Обосновать Redis/Neo4j availability требования, стоимость Enterprise и cluster
   topology; отдельно измерить read replicas/routing и write contention.
   Кластер сам по себе не обещает линейного масштабирования записи.
5. Если измерения покажут необходимость, сравнить альтернативные движки/индексы
   на том же workload и только после этого планировать миграцию.
