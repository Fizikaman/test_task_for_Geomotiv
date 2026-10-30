# Celery worker и processing lease (P15)

Запуск с обычным CPython 3.15.2 и uv 0.13.6:

```powershell
uv sync --frozen
uv run --frozen celery -A article_graph.celery_app:app worker --pool=prefork --concurrency=2 --loglevel=INFO
```

Prefork предназначен для Linux. Для локальной проверки на Windows используйте
`--pool=solo --concurrency=1`; этот pool не обеспечивает soft/hard time limits
prefork. Общий asyncio deadline и lease fencing работают независимо от pool.

Настройки загружаются и валидируются в `celery_app` до запуска worker. Обязательны
`WORKER__BROKER_URL`, `WORKER__BLOB_ROOT`, все обязательные поля `LLM` и `OLLAMA`
с префиксом `ARTICLE_GRAPH_`; пример находится в `.env.example`. Отсутствующий
text model, output mode, URL или API key не подменяется настройками провайдера.
Размерность Ollama должна совпадать с индексом Neo4j. Отдельные API JWT/App Token
worker не нужны. Каталог blobs должен быть тем же, что использует upload.
Клиенты и каталоги не открываются при импорте/регистрации задач в parent process.

`create_app` явно, eagerly регистрирует `article_graph.process_job(job_id)` через
`lazy=False`. Celery импортируется eagerly для регистрации; heavy pipeline,
HTTP/Neo4j и PDF-зависимости используют PEP 810 lazy imports. Внутри каждого
вызова задачи создаётся собственный driver, HTTP-клиенты и event loop, после
fork. Neo4j driver закрывается context manager; оба HTTP-клиента закрываются
при успехе, исключении и cooperative cancellation, включая сбой embedding
probe/bind. SIGKILL/hard timeout не позволяет выполнить finally: ресурсы ОС
освобождаются с процессом, а lease впоследствии истекает. Инициализация embeddings
выполняет настоящий probe/bind; автоматические тесты заменяют только HTTP.

## Владение и повторы

Claim читает Job после блокировок Article → Job, совместимых с P07/P13/P14.
Учитываются latest revision/job, durable status, время available_at и число
attempts. Только queued или processing с истёкшим lease могут начать новую
попытку. Каждая попытка увеличивает счётчик и получает новый UUID fencing token.
Живой lease, unknown job и terminal job завершают доставку без pipeline.
Вытесненная revision переводится в superseded. Все сравнения времени используют
`datetime.realtime()` Neo4j, а не часы worker.

Heartbeat и failed/retry updates проверяют attempt/token, текущие указатели,
processing status и живой lease после тех же блокировок. Истёкший lease нельзя
оживить heartbeat. При утрате владения или ошибке heartbeat pipeline отменяется;
его старый владелец не записывает failed. Успех сохраняется только атомарной
публикацией P13. Ошибка после committed публикации не меняет completed Job.

Permanent ошибка завершает Job/revision как failed. Для transient ошибки
действует один durable бюджет `limits.job_attempts` (3, включая первую попытку).
Backoff: `retry_backoff_seconds × 2^(attempt - 1)` (2 и 4 секунды по умолчанию).
В той же транзакции сохраняются queued status, available_at и pending Outbox.
После закрытия клиентов задача вызывает Celery retry с этим countdown.
`max_retries = job_attempts - 1`; HTTP-адаптеры не добавляют сетевых повторов.
До claim инфраструктурные ошибки имеют тот же ограниченный Celery retry budget;
они не расходуют durable attempt, пока работа не захвачена. Ошибки и результаты
не сохраняют документы, ответы провайдера или tokens в Celery result backend.

Celery retry publication не атомарна с Neo4j. Если публикация retry не удалась,
pending Outbox остаётся. Для доставки и восстановления queued/expired jobs
нужно запустить отдельный [dispatcher P16](dispatcher.md). Занятая lease
подтверждается как duplicate; если владелец затем погиб, dispatcher восстановит
задание после истечения lease. Уже отправленные дубликаты безопасны благодаря
durable status, available_at и fencing. Внешние LLM-вызовы не имеют exactly-once гарантии.

## Время и доставка

По умолчанию pipeline/инициализация ограничены 300 секундами, каждый provider
request — 30. Heartbeat выполняется отдельной asyncio task каждые 15 секунд,
lease — 120 секунд; LLM await не блокирует heartbeat. Lease должен покрывать
минимум четыре heartbeat интервала. Provider deadline не может превышать общий
processing deadline. Можно явно увеличить оба бюджета для долгой модели.

Soft limit = processing + 60 секунд (360), hard limit = processing + 90 (390).
Запас предназначен для отмены и закрытия клиентов. Redis visibility timeout =
hard limit + lease + 60 (570); он длиннее нормальной обработки/закрытия.
`acks_late=True`, `acks_on_failure_or_timeout=True`,
`reject_on_worker_lost=True`, prefetch multiplier = 1. Эти параметры согласованы
с [документацией Celery](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
и [настройкой prefetch](https://docs.celeryq.dev/en/stable/userguide/optimizing.html).
Сообщения и accepted content — только JSON; result backend не нужен.
Celery retries и брокерные redeliveries не обходят durable attempt budget.

## Проверка

```powershell
uv run --frozen pytest tests/test_worker.py tests/test_pipeline.py --neo4j
```

Используется настоящая изолированная Neo4j из `docker-compose.test.yml`.
Проверяются конкурентные duplicate claims, busy/expired lease, fencing старого
владельца, backoff, transient/permanent/crashed attempt exhaustion, supersession,
heartbeat во время вызова дольше lease, timeout/cancellation, закрытие реальных
HTTP-клиентов с MockTransport и настоящая публикация pipeline.
Настоящий Celery worker с pool=solo и memory transport получает JSON-сообщения,
выполняет retry через consumer и пропускает duplicate failed delivery.
Это не eager mode. Runtime subprocess проверяет eager task registration,
startup validation и lazy Neo4j/HTTP/PDF imports. Платных API тесты не вызывают.
Redis, Linux prefork, аварийное убийство процесса и нагрузочный прогон в этот
Windows-набор не входят.
