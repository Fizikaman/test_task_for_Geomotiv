# Docker Compose — P19

Скопировать `.env.example` в `.env` и заполнить первые строки: URL, API key и
text model своего LLM-провайдера; подтвердить поддерживаемый output mode.
Для PDF/изображений также явно задать vision model с поддержкой image data URLs.
Провайдер заранее не выбран. Ключ провайдера хранить только в локальном `.env`.

В примере уже заданы три независимых ключа приложения и пароль Neo4j:
это публичные демонстрационные значения для одноразовой локальной проверки.
Для других развёртываний сгенерировать собственные значения (ключи приложения —
минимум 32 символа), не использовать публичные credentials. `.env` исключён из
Git. Копирование примера вручную использует именно публичные значения.
Затем:

```powershell
docker compose up -d --build
docker compose ps -a
```

API доступен на `http://127.0.0.1:8000`. Redis, Neo4j и Ollama доступны только
во внутренней сети Compose. Сетевые адреса и пути внутри контейнеров Compose
переопределяет сам; localhost из `.env.example` предназначен для запуска вне Docker.
Другие переменные `ARTICLE_GRAPH_*` передаются из `.env`, включая лимиты.
`.env` не попадает в образ или build context. Для проверки конфигурации без вывода
секретов использовать `docker compose config --quiet`.

Образ использует обычный CPython 3.15.2, uv 0.13.6 и `uv sync --frozen`;
приложение запускается от UID/GID 10001. Python downloads отключены, глобальный
lazy-import mode и JIT не включаются. API регистрирует роутеры eager, но не
импортирует Celery worker или renderer при обычной загрузке модулей.

Версии образов: Python 3.15.2-slim, uv 0.13.6, Neo4j 5.26.0-community,
Redis 8.2.2-alpine, Ollama 0.9.6. Нет образов с плавающим `latest`.
На первом запуске требуется интернет для образов, frozen dependencies и модели.
CPU-режиму Ollama нужны место под модель (~1.2 GB) и несколько GB свободной RAM;
загрузка и первый embedding могут занять несколько минут.

`init-schema` ждёт healthy Neo4j, идемпотентно создаёт схему и ждёт ONLINE индексов.
`init-model` ждёт схему и Ollama, автоматически скачивает явный model tag,
сверяет полный SHA-256 manifest с настройками и получает настоящий embedding.
Проверяются конечность/ненулевой вектор, размерность и соответствие индексу;
embedding space привязывается в Neo4j. Только exit 0 init разрешает запуск
API/worker/dispatcher; также требуется healthy Redis. Ошибка digest, размерности
или загрузки блокирует запуск. Пример фиксирует `bge-m3:567m` и digest публичного
[manifest Ollama](https://registry.ollama.ai/v2/library/bge-m3/manifests/567m).
Смена модели/digest/размерности требует явной миграции существующего индекса;
init не перепривязывает существующие векторы к другой модели.

API startup повторяет проверку схемы и embedding space. `/health/ready` повторно
проверяет схему, digest и настоящий embedding; до готовности или при потере
модели возвращает 503. `/health/live` проверяет только процесс. Celery healthcheck
использует адресный ping worker, dispatcher — свежесть файла, обновляемого после
успешного цикла работы с БД. Одноразовые init завершаются с exit 0 и не имеют
постоянного healthcheck. Healthchecks показывают сбои; Compose сам по себе не
перезапускает процесс только из-за статуса unhealthy.

Тома с префиксом проекта `geomotiv`: `sources` (immutable исходники и checkpoints,
общие API/worker), `neo4j-data`, `redis-data`, `ollama-models`. Redis использует
AOF и `appendfsync always`. Neo4j хранит Job/Outbox и active revision; доставка
at-least-once восстанавливается dispatcher. Worker использует Linux prefork,
concurrency 1 и 420 секунд graceful shutdown. API отключает access log, чтобы
не выводить пользовательские параметры запросов.

```powershell
docker compose stop
docker compose up -d
```

Остановка/повторный запуск сохраняют тома. Не выполнять `down -v` для рабочих,
чужих или BroLango томов. Изменение пароля в `.env` не меняет автоматически пароль
уже инициализированной Neo4j. Секреты Docker environment видны администратору Docker.

## Изолированная приёмка

`docker-compose.test.yml` остаётся отдельной Neo4j P03 на 17474/17687.
Полный acceptance использует другой Compose project, собственную сеть, четыре
тома и API на 18000. Публичный `tests/compose.env` направляет LLM на закрытый
loopback-порт: платных вызовов нет. Рабочая `.env` не читается.
Нужен Docker Compose с поддержкой `!override` (2.24.4+).

```powershell
$p19 = @('--env-file', 'tests/compose.env', '-p', 'geomotiv-p19-acceptance',
  '-f', 'docker-compose.yml', '-f', 'docker-compose.acceptance.yml')
docker compose @p19 config --quiet
docker compose @p19 up -d --build --wait --wait-timeout 1800
# Все постоянные сервисы должны быть healthy, оба init — Exited (0).
docker compose @p19 ps -a
# Удержать принятую задачу в очереди во время проверки перезапуска.
docker compose @p19 stop worker dispatcher
Get-Content tests/compose_smoke.py -Raw | docker compose @p19 exec -T api python - seed
docker compose @p19 stop
docker compose @p19 up -d --wait --wait-timeout 300 --scale worker=0 --scale dispatcher=0
Get-Content tests/compose_smoke.py -Raw | docker compose @p19 exec -T api python - verify
docker compose @p19 up -d --wait --wait-timeout 300
docker compose @p19 stop
```

Smoke проверяет реальный HTTP 202, общий source volume, настоящий embedding,
публикацию через GraphPublisher с настоящим claim lease, непустой active graph
при queued update, доставку Celery/Kombu в Redis и сохранность всего этого после
остановки/повторного запуска процессов. Fixture публикации задана явно, LLM не
вызывается. Worker останавливается на время проверки queued; после его запуска
тестовое обновление может стать failed из-за намеренно недоступного LLM, сохраняя
старую active revision. Это не проверка полного успешного LLM pipeline (P20/P24).
Тестовые данные и модель остаются в выделенных томах для повторной проверки.

## Проверено 30 октября 2026

Frozen sync, Ruff check/format и Pyrefly strict (0 errors), 185 целевых pytest
с настоящей изолированной Neo4j, Compose config/build/startup/health прошли.
Автозагрузка проверена с пустого model volume; настоящий bge-m3 вернул
1024-мерные vectors, совместимые с индексом. Linux runtime подтвердил UID 10001,
обычный CPython 3.15.2 и отсутствие worker/renderer среди импортов API.
Smoke seed/verify прошёл до и после stop/up всех процессов. Реальная остановка
Ollama дала ready=503 и live=200; после восстановления все сервисы healthy.
После проверки контейнеры остановлены, тома сохранены.

Полный pytest, hard kill и успешный реальный LLM pipeline не запускались;
платные API не вызывались. Прежнее предупреждение Pyrefly о deprecated
AsyncIterator в `article_routes.py:67` сохранено без попутного изменения P17.
