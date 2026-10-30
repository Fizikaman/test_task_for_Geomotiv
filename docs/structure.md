# Структура исходников и тестов — P25

Корень `src/article_graph` содержит `app.py` (FastAPI factory), `settings.py`,
`domain.py`, `__main__.py` и пустой package initializer. Остальное сгруппировано
по назначению; `__init__.py` подпакетов не импортируют реализации.

| Пакет | Содержимое |
| --- | --- |
| `api` | HTTP routes авторизации, статей и графа |
| `security` | JWT/App Token и durable refresh sessions |
| `documents` | Blob storage, проверка форматов, Markdown/PDF preparation, PDF subprocess |
| `ai` | LLM/embeddings adapters, extraction, identity resolution, model initialization |
| `db` | Neo4j driver/schema, repositories, publication, leases, checkpoints и outbox |
| `processing` | Registration, pipeline, Celery worker/entry point и dispatcher |
| `prompts` | Тексты промптов на прежнем resource path; содержимое не менялось |

Тесты находятся в `tests/api`, `tests/ai`, `tests/documents`, `tests/processing`,
`tests/db`, `tests/deployment`, `tests/unit`, `tests/infrastructure`, `tests/e2e`
и `tests/live`. Общие fixtures — в `tests/conftest.py`; публичные env-файлы и
операционные helper scripts — в корне tests. HTTP doubles и document fixtures
находятся рядом с соответствующими E2E/infrastructure/live сценариями.
Деление по папкам тематическое: opt-in infrastructure/DB/live по-прежнему
управляются pytest markers и флагами; платные API обычным pytest не вызываются.

Обновлены обычные/lazy импорты, строковые импорты в subprocess-тестах,
PDF subprocess command, Celery/dispatcher/model-init команды Compose,
console script `init-schema`, CI и пути в документации. Celery task name
`article_graph.process_job`, HTTP-контракт, JSON checkpoints и resource paths
промптов сохранены. Старые модули-переадресации не добавлялись.

Основные точки входа:

```text
uvicorn article_graph.app:create_app --factory
python -m article_graph
init-schema
python -m article_graph.ai.model_init
celery -A article_graph.processing.celery_app:app worker
python -m article_graph.processing.dispatcher
```

## Проверено 30 октября 2026

Перенесены 29 application modules и 30 test/helper modules; добавлены пустые
initializers подпакетов. AST-сравнение 67 прежних Python-файлов с HEAD подтвердило,
что изменения ограничены переназначением путей/импортов, сортировкой импортов
и глубиной `parents` у перемещённых тестов. Бизнес-логика не менялась.

| Проверка | Результат |
| --- | --- |
| `uv sync --frozen` | Passed; editable package пересобран для нового console entry point |
| Ruff lint / format check | Passed |
| Pyrefly strict source/tests | 0 errors; прежний warning AsyncIterator |
| actionlint | Passed |
| `pytest -q` через frozen uv | 458 passed, 194 skipped; 33.56 с |
| `pytest --neo4j -q` через frozen uv | 639 passed, 13 skipped; 106.80 с |
| `pytest -m infrastructure --infrastructure -q` через frozen uv | 6 passed, 646 deselected; 108.41 с |
| `pytest -m e2e --e2e -q` через frozen uv | 2 passed, 650 deselected; 51.26 с |
| CI=true, новый путь live-набора с opt-in flag | 3 skipped; 0.20 с |
| `tests/deployment/test_documentation.py` | 3 passed; 0.37 с |

Windows CPython 3.15.2 / uv 0.13.6, immutable lock; runtime lazy imports проверены
subprocess-тестами. Pytest/Docker выполнялись вне ограниченного sandbox;
frozen rebuild внутри sandbox не смог разрешить DNS PyPI и успешно выполнен
снаружи. Neo4j P03 настоящая и изолированная. P20/P21 проверили новую Linux-сборку
production Dockerfile, console init-schema, API/worker/dispatcher, PDF subprocess
и обновлённые пути doubles/fixtures. Paid API не вызывались.

P20/P21 контейнеры/тома удалены fixtures, P03 остановлен с exit 0, том сохранён.
Рабочие контейнеры и ранее загруженные документы не изменялись; новый рабочий
образ нужно применить обычной командой Compose при следующем обновлении.
Повторный live LLM/Vision и отдельный полный Linux-host pytest не запускались.

По отдельному явному запросу пользователя добавлен `origin`:
`https://github.com/Fizikaman/test_task_for_Geomotiv.git`. Проверена локальная
настройка remote; сетевой доступ/права на push не проверялись. Commit/push,
смена ветки и изменения индекса не выполнялись. При просмотре Git без stage
переносы отображаются удалёнными старыми и новыми untracked-файлами.
