# Article Graph — P03

Минимальный API на обычном CPython **3.15.2**. Для будущих Docker/CI использовать
ту же ветку 3.15, без free-threaded/JIT. Реализованы каркас API, DTO и схема Neo4j.

Нужен uv **0.13.6**. Если установлен старый uv, команды можно запускать через
`uvx uv==0.13.6 ...`; в созданном окружении доступен `.venv/Scripts/uv.exe` (Windows).

```powershell
uv sync --frozen
Copy-Item .env.example .env
# Задать в .env свой ARTICLE_GRAPH_APP_TOKEN (не менее 32 символов).
uv run --frozen article-graph
```

Приложение слушает `127.0.0.1:8000`. Фабрика — `article_graph.app:create_app`;
альтернативный запуск: `uv run --frozen uvicorn article_graph.app:create_app --factory`.
Публичный `GET /health/live` возвращает `{"status":"ok"}` и проверяет только
работоспособность процесса. Отсутствующий/короткий App Token останавливает создание
приложения; ошибки настроек скрывают входные значения. В P01 App Token только
валидируется и хранится в настройках; авторизация добавляется в P04.
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
