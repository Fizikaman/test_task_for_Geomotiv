# Article Graph

Рабочий Docker Compose (P19): после настройки `.env` выполнить
`docker compose up -d --build`. Автоматические init схемы/модели, healthchecks,
сохранность данных и изолированная приёмка описаны в [docs/docker.md](docs/docker.md).

## Настройка LLM в `.env`

Скопируйте `.env.example` в `.env`, если локальный файл ещё не создан, и заполните
первые строки. Например, для DeepSeek:

```dotenv
ARTICLE_GRAPH_LLM__BASE_URL=https://api.deepseek.com
ARTICLE_GRAPH_LLM__API_KEY=your-provider-api-key
ARTICLE_GRAPH_LLM__TEXT_MODEL=deepseek-flash
ARTICLE_GRAPH_LLM__OUTPUT_MODE=json_object
```

`TEXT_MODEL` — точный идентификатор модели в API выбранного провайдера,
а `API_KEY` — ваш настоящий ключ, заменяющий placeholder. Для другого провайдера
нужно изменить URL и идентификатор модели и указать поддерживаемый JSON mode.
Это пример ручной настройки: сервис не выбирает DeepSeek автоматически.
URL/model и JSON mode сверены с [документацией DeepSeek](https://api-docs.deepseek.com/)
и [JSON Output](https://api-docs.deepseek.com/guides/json_mode/); успешный live-прогон
этого проекта с провайдером пока не выполнен.

Для PDF и изображений дополнительно задайте `ARTICLE_GRAPH_LLM__VISION_MODEL`:
точный идентификатор модели того же API, поддерживающей изображения через
`image_url` с data URL. Текстовая модель автоматически вместо неё не используется.
Реальные ключи храните только в локальном `.env`, который исключён из Git.
Ключи приложения и пароль Neo4j в `.env.example` уже заданы для локальной
демонстрации; для других развёртываний замените публичные значения своими.

Сквозные тесты отказов и обновлений P20 с настоящими Neo4j/Redis/dispatcher/worker:
`uv run --frozen pytest -m infrastructure --infrastructure`.
Контур и очистка создаются автоматически, LLM/Ollama заменены HTTP doubles;
команду можно повторять без ручной чистки. Подробнее: [docs/integration.md](docs/integration.md).

HTTP E2E P21: `uv run --frozen pytest -m e2e --e2e`.
Отдельный Compose, настоящий API и фоновые процессы, Markdown/update/refresh
и PDF с проверкой полученных image bytes: [docs/e2e.md](docs/e2e.md).

Минимальный API на обычном CPython **3.15.2**. Для будущих Docker/CI использовать
ту же ветку 3.15, без free-threaded/JIT. Реализованы каркас API, DTO, схема Neo4j
и авторизация с ротацией Refresh.

Нужен uv **0.13.6**. Если установлен старый uv, команды можно запускать через
`uvx uv==0.13.6 ...`; в созданном окружении доступен `.venv/Scripts/uv.exe` (Windows).

```powershell
uv sync --frozen
Copy-Item .env.example .env
# Заполнить настройки LLM, как в примере выше.
# Вне локальной демонстрации заменить публичные credentials своими.
uv run --frozen article-graph
```

Приложение слушает `127.0.0.1:8000`. Фабрика — `article_graph.app:create_app`;
альтернативный запуск: `uv run --frozen uvicorn article_graph.app:create_app --factory`.
Публичный `GET /health/live` возвращает `{"status":"ok"}` и проверяет только
работоспособность процесса. Отсутствующий/короткий App Token останавливает создание
приложения; оба JWT-ключа также обязательны. Ошибки настроек скрывают входные
значения. `POST /auth/token` обменивает App Token на пару JWT;
`POST /auth/refresh` ротирует Refresh без действующего Access.
Формат запросов, политика replay и общая dependency для будущих бизнес-роутеров:
[docs/auth.md](docs/auth.md).
Перед запуском API нужно настроить Neo4j и выполнить `init-schema` (см. ниже).
Startup проверяет доступность БД, constraints/indexes и ONLINE vector index;
при несовместимой размерности запуск завершается ошибкой. `/health/ready` повторяет
проверку и возвращает 503 при потере готовности. `/health/live` проверяет процесс.

```powershell
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyrefly check
uv run --frozen pytest
```

В ограниченном Windows sandbox временные файлы pytest можно направить в проект:
`uv run --frozen pytest --basetemp=.cache/pytest`. HTTP-проверки требуют разрешения
на запуск локального сервера. Обычный запуск вне sandbox не требует этой настройки.

Результаты проверок и ограничения будущих зависимостей: [docs/compatibility.md](docs/compatibility.md).

## Neo4j и изолированные тесты P03

Драйвер создаётся/закрывается в lifespan API; отдельные процессы используют
`open_database` и обязаны вызвать `require_ready` перед обработкой работ.
Воркеры появятся в P15. Схема не создаётся автоматически при старте API.
`init-schema` читает только переменные окружения `ARTICLE_GRAPH_NEO4J__*`,
не требует App Token и не загружает `.env`. API по-прежнему читает `.env`.
Для своей БД экспортируйте URI, username, password и database перед CLI.

Только тестовая БД, с публичным одноразовым тестовым паролем:

```powershell
docker compose -f docker-compose.test.yml config --quiet
docker compose -f docker-compose.test.yml up -d --wait
$env:ARTICLE_GRAPH_NEO4J__URI = 'bolt://127.0.0.1:17687'
$env:ARTICLE_GRAPH_NEO4J__PASSWORD = 'geomotiv-isolated-test-only'
uv run --frozen init-schema
uv run --frozen init-schema
uv run --frozen pytest --neo4j
```

Compose использует только `geomotiv-p03-test`, контейнер
`geomotiv-p03-neo4j-test`, порты 17474/17687 на loopback и volume
`geomotiv-p03-neo4j-test-data`. Fixtures подключаются только к этому адресу
с тестовыми credentials, игнорируя настройки рабочей БД. Без `--neo4j`
integration-тесты пропускаются; с флагом недоступная БД — ошибка, не skip.
Запускайте набор последовательно: проверки readiness временно удаляют и
восстанавливают только тестовый vector index. Очистка удаляет только узлы
с меткой `_GeomotivP03Test`; том сохраняется. Повторный прогон не требует очистки.
Остановить только тестовый контейнер: `docker compose -f docker-compose.test.yml stop`.

Схема и ограничения Community описаны в [docs/neo4j.md](docs/neo4j.md).
