# Outbox dispatcher и recovery (P16)

Отдельный процесс с обычным CPython 3.15.2 и uv 0.13.6:

```powershell
uv sync --frozen
uv run --frozen python -m article_graph.dispatcher
```

Если системный `uv` другой версии, используйте проектный:
`.\.venv\Scripts\uv.exe run --frozen python -m article_graph.dispatcher`.

Обязательны `ARTICLE_GRAPH_DISPATCHER__BROKER_URL` и доступ к той же Neo4j,
что используют регистрация и worker. Broker URL должен совпадать с worker;
очередь — `article_graph`, JSON task — `article_graph.process_job(job_id)`.
Настройки и примеры — в `.env.example`. Dispatcher не создаёт pipeline и
не использует LLM/Ollama, поэтому provider settings ему не требуются.
В worker обязательные provider settings по-прежнему валидируются до старта.

По умолчанию poll — 1 с, до 100 доставок и 100 recovery-кандидатов за цикл,
delivery lease — 60 с, порог повторной доставки queued — 600 с. После ошибки
publish backoff = 2 × 2^(delivery_attempt − 1), максимум 60 с. Redis connect
и socket timeouts — 5 с, скрытые повторы publish в Celery отключены. Слишком
короткий delivery lease увеличивает число допустимых дубликатов.

## Доставка и падения

Короткая Neo4j transaction блокирует одну Outbox, повторно проверяет готовность
по времени БД и атомарно сохраняет `dispatching`, UUID claim token, expiry и
счётчик доставок. Две копии dispatcher могут работать одновременно. Сообщение
публикуется после commit claim, вне managed transaction: Neo4j transaction retry
не повторяет broker I/O. Outbox не удаляется.

После успешного publish отдельная transaction записывает `sent`, `sent_at`
и `Job.last_dispatched_at`. Она проверяет действующий claim token и expiry.
При ошибке publish тот же fenced update возвращает запись в `pending` с backoff.
Неоднозначный ответ брокера также считается ошибкой и допускает повтор.

При смерти процесса до publish или между publish и mark-sent запись остаётся
`dispatching`. После expiry следующий dispatcher получит её с новым token.
Старый владелец больше не может записать sent или вернуть pending. Ошибка БД
после publish даёт тот же результат. При недоступной БД процесс повторяет
подключение с паузой; SIGINT/SIGTERM останавливают цикл и закрывают клиенты.

Публикация не означает выполнение worker и не доказывает сохранность сообщения
в Redis. Если queued Job не начал обработку за 600 с после последнего sent,
recovery создаёт новый pending Outbox. Восстанавливается и отсутствие Outbox.
Уже существующий pending/dispatching Outbox предотвращает создание ещё одного.
Проверяются Job.available_at и время БД; retry backoff не сокращается.

## Processing recovery и границы гарантии

Recovery повторно читает Job под блокировками Article → Job, совместимыми с
worker/heartbeat/publication. При живом lease изменений нет, даже если scanner
ранее видел устаревшее состояние. Истёкшая попытка получает queued status и новый
Outbox в одной transaction, старые lease/token снимаются. Backoff обработки:
`limits.retry_backoff_seconds × 2^(attempt − 1)`. Следующий worker claim увеличит
attempt и создаст новый fencing token. Регистрация новой revision и supersession
проверяются под теми же блокировками. Mark-sent блокирует Article → Job → Outbox;
claim держит только Outbox и завершает transaction до обращения к Job.

Бюджет `limits.job_attempts` (3, включая первую попытку) ограничивает запуски
pipeline, в том числе после crash и lease expiry. После последней истёкшей
попытки Job/revision становятся failed с `processing_timeout`. Recovery не
расходует attempt до worker claim. Completed/failed/superseded Job не запускают
pipeline при повторной доставке. Старый владелец не может публиковать результат.

Попытки транспорта продолжаются с ограниченной паузой, пока брокер недоступен:
outage не расходует processing budget и не выбрасывает принятое задание.
Queued Job при отсутствии worker остаётся queued и получает периодические
повторные доставки. Успешное восстановление требует возвращения Neo4j, broker,
dispatcher и worker; сохранность данных самой Neo4j — предпосылка.
Outbox/sent history пока сохраняется без очистки; большой объём и нагрузка
не проверялись. Контракт — at-least-once с Job/lease fencing. Exactly-once для
сообщений и внешних платных вызовов не обещается.

## Проверка

```powershell
uv run --frozen pytest tests/test_dispatcher.py tests/test_worker.py tests/test_pipeline.py --neo4j
```

Тесты используют настоящую отдельную Neo4j 5.26.0 Community из
`docker-compose.test.yml`, управляемый broker adapter для точек сбоя и настоящий
Celery/Kombu memory transport для проверки JSON task/queue. Проверяются ошибки
до и после publish, отсутствие mark-sent при смерти процесса/ошибке БД,
параллельные dispatcher и recovery, fencing просроченной доставки, потерянные
queued сообщения, expired/live processing lease, backoff, exhaustion и terminal
duplicates. Runtime subprocess проверяет startup validation и PEP 810 lazy
imports. Платные API не вызываются. Настоящий Redis outage, Linux prefork,
OS hard kill и нагрузочный прогон этим набором не проверяются.
