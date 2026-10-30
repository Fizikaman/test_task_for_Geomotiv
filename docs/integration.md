# P20: интеграционная приёмка

Нужны Docker Compose v2, обычный CPython 3.15.2 и uv 0.13.6. Из корня
репозитория выполнить отдельную команду:

```powershell
uv sync --frozen
uv run --frozen pytest -m infrastructure --infrastructure
```

В текущем Windows-окружении закреплённый uv доступен как
`.venv/Scripts/uv.exe`. Повторить ту же команду можно сразу, без ручной чистки.
Обычный pytest пропускает эти тесты; `--neo4j` по-прежнему включает прежние
интеграционные тесты P03–P19, отдельно от P20.

Fixture сама собирает production Dockerfile и запускает
`docker-compose.integration.yml`: Neo4j Community 5.26.0, Redis 8.2.2 с AOF,
настоящие dispatcher и Linux prefork Celery worker с двумя процессами.
Регистрация, polling статуса и чтение графа идут через ASGI TestClient;
worker использует реальные Neo4j, Redis, source/checkpoint storage и HTTP.
Отдельный настоящий HTTP-сервер API относится к P21.

LLM Chat Completions и Ollama tags/embed обслуживает управляемый HTTP double
`tests/p20_doubles.py`: фиксированные model/digest/1024-мерные vectors,
валидное извлечение с evidence, пустые связи, HTTP BAD_REQUEST и gate первого
ответа. Счётчики доказывают отсутствие AI-вызовов при no-op и повторной доставке.
Тела документов и токены не выводятся в логи. Production pipeline, lease,
publication и broker adapters не подменяются.

Шесть pytest-сценариев покрывают:

- новую статью до активного графа, no-op без AI, update без прежней связи,
  сохранность другой статьи с общими персонажами и active revision после failed update;
- повторную доставку через Redis во время обработки и после завершения;
- stale revision с удержанным HTTP-ответом и публикацией более новой revision;
- stale attempt: принудительное истечение lease только тестового Job,
  восстановление настоящим dispatcher и завершение второй попыткой;
- регистрацию при остановленном Redis, durable ошибку публикации Outbox
  и автоматическое восстановление после запуска Redis;
- регистрацию и доставку при остановленном worker, затем его запуск
  и завершение сохранённой задачи.

Update удаляет старое утверждение из **активного** графа. Исторические assertions
остаются для provenance согласно P13; тест отдельно проверяет, что вытесненная
до публикации revision вообще не создала assertions.

Ожидания используют monotonic deadline и polling с интервалом до 100 мс,
без фиксированных sleep. Timeout показывает этап, безопасные статусы Job/Outbox
для ожидания завершения и состояние контейнеров. HTTP/Redis/control requests
имеют собственные timeouts; Compose startup ограничен 180 секундами.

Контур имеет принудительный project name `geomotiv-p20-test`, свой env-файл
`tests/infrastructure.env`, публичные тестовые credentials и loopback-порты
27687/26379/28080. Рабочая `.env` не загружается. Worker и dispatcher подключены
только к внутренней Docker-сети без внешнего маршрута: URL провайдеров закреплены
за double, оплачиваемые API недоступны. Дополнительная сеть публикует только
тестовые Neo4j/Redis/double для управляющего pytest на хосте.

Запускать последовательно: тесты намеренно останавливают свои Redis/worker.
Не использовать этот project name или `.cache/p20-blobs` для других данных.
В `finally` останавливаются и удаляются только контейнеры/анонимные тома этого
проекта, затем только его blob-каталог; рабочая БД и P03/P19 не затрагиваются.
На Linux процессы тестового Compose используют UID/GID запускающего pytest
для совместного доступа к bind-mounted blobs; на Windows — UID/GID 10001.

Если pytest принудительно завершён и `finally` не выполнился, повторный запуск
использует только тот же тестовый контур и очищает его в конце. Docker images
остаются в кэше. Схема и модель рабочего окружения не меняются.

## Проверено 30 октября 2026

На Windows-хосте с Linux-контейнерами финальный набор прошёл дважды:
6 passed / 620 deselected, затем 6 passed / 620 deselected без ручной очистки.
Frozen sync, Ruff check/format и Pyrefly strict — успешно, 0 ошибок типов;
прежнее предупреждение о deprecated AsyncIterator не относится к P20.
Обычный pytest: 437 passed, 189 skipped (181 прежний Neo4j opt-in,
6 P20 opt-in, Windows symlink без прав и Linux-only RLIMIT_AS).
Прежние P03 --neo4j тесты отдельно не запускались. Контейнеры, анонимные тома
и blob-каталог P20 удалены fixture. Платные API не вызывались.
Запуск pytest на Linux-хосте и настоящий HTTP API-сервер в этой приёмке
не проверялись; worker и dispatcher выполнялись в настоящем Linux runtime.
