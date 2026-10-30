# P23: проверка документации

Проверка 30 октября 2026: Windows-хост, Linux Docker containers, обычный CPython
3.15.2 и uv 0.13.6. Использовался `.venv/Scripts/uv.exe`, потому что системный uv
другой версии; `UV_CACHE_DIR` направлен в `.cache/uv`. Docker, HTTP subprocess
и pytest выполнялись вне ограниченного Windows sandbox. Рабочий `.env` не
просматривался, отдельный ключ провайдера не запрашивался и не использовался.

## Локальные команды

| Команда (с проектным uv 0.13.6) | Фактический результат |
| --- | --- |
| `uv sync --frozen` | Passed, 52 packages checked |
| `uv run --frozen ruff check .` | Passed |
| `uv run --frozen ruff format --check .` | Passed |
| `uv run --frozen pyrefly check` | 0 errors; прежнее предупреждение AsyncIterator в article_routes |
| `uv run --frozen pytest -q` | 458 passed, 194 skipped; 69.90 с |
| `uv run --frozen pytest --neo4j -q` | 639 passed, 13 skipped; 200.01 с |
| `uv run --frozen pytest -m infrastructure --infrastructure -q` | 6 passed, 646 deselected; 152.56 с |
| `uv run --frozen pytest -m e2e --e2e -q` | 2 passed, 650 deselected; 73.11 с |
| `uv run --frozen pytest tests/deployment/test_documentation.py -q` | 3 passed |
| `CI=true`, `uv run --frozen pytest tests/live/test_live_provider.py --live-provider -q` | 3 skipped, даже с явным live flag |
| `git diff --check` | Passed; только предупреждения LF/CRLF |

P03 поднят/остановлен командами README с `--env-file tests/e2e.env`;
использовалась только отдельная БД на 17687. Её существующий volume сохранён.
Пропуски в полном DB-наборе: opt-in infrastructure/E2E/live, Windows symlink и
Linux-only RLIMIT_AS. Обычный набор включает runtime PEP 810 проверки.
P20 проверяет outage/crash/recovery на настоящих Neo4j/Redis/worker/dispatcher;
AI endpoints — только fixtures. Это функциональные тесты, не нагрузочный прогон.

Production Compose проверен через `config --quiet` с публичным acceptance
override: `--env-file tests/compose.env -p geomotiv-p19-acceptance
-f docker-compose.yml -f docker-compose.acceptance.yml`. Сервисы этого проекта
в P23 не запускались. Таким образом service env_file также заменён тестовым,
рабочий `.env` не требуется для проверки конфигурации.

## Curl, OpenAPI и ссылки

Стек поднят ровно командой ручного тестового setup из README:

```powershell
docker compose --env-file tests/e2e.env -p geomotiv-p23-docs -f docker-compose.e2e.yml up -d --build --wait --wait-timeout 180
& ./tests/check_readme.ps1
docker compose --env-file tests/e2e.env -p geomotiv-p23-docs -f docker-compose.e2e.yml down --volumes --remove-orphans --timeout 10
```

Сборка production Dockerfile, init-schema, API и фоновые процессы прошли.
Worker/dispatcher/Neo4j/Redis настоящие; LLM/Ollama — HTTP doubles. Использованы
только публичные тестовые credentials из тестового Compose и синтетические
Markdown/PDF fixtures. Порты — 38000/38080; рабочий 8000 уже был занят чужим
стеком, его контейнеры не изменялись.

[check_readme.ps1](../tests/check_readme.ps1) извлекает и выполняет сами помеченные
PowerShell-блоки README, заменяя лишь base URL и placeholders. Перед выполнением
проверяется наличие отдельного `geomotiv-p23-docs` API. Проверены:

- Все 11 API operations, `openapi.json`, Swagger и ReDoc.
- Token issuance/rotation и чтение с новым Access; JWT не выводятся.
- Upload Markdown text/file и настоящий PDF renderer + передача изображения
  HTTP double, polling до completed и совпадение active revision.
- Hash no-op: неизменные job/revision, `unchanged=true`, дополнительная проверка
  HTTP 200. Update: тот же article, новая revision и опубликованный новый граф.
- Idempotency replay: первоначальные job/revision воспроизводятся.
- Все четыре GET графа и фильтр `type=FRIEND_OF`.
- Для negative fixture без стрелки получен failed; retry дал тот же revision
  и новый job, затем снова failed из-за неизменённого некорректного для double
  изображения. Retry completed статьи дал HTTP 409.

Тестовый Compose, его сети и blob volume удалены после проверки; временные
synthetic files helper удалены. Ни ключи, ни auth response, ни тела документов
не сохранялись в отчёт. PDF содержит только имена и векторную стрелку;
это проверка transport/pipeline, а не semantic quality реального LLM.

[test_documentation.py](../tests/deployment/test_documentation.py) проверяет:

- Существование всех локальных файлов по Markdown-ссылкам README, decisions
  и этого отчёта, включая полные prompt texts; выход за корень проекта запрещён.
- Равенство method/path из таблицы README и фактически сгенерированного OpenAPI;
  Access security, основные response codes, multipart fields, pagination/type.
- Placeholder-значения ключей в env-примере README.

Mermaid-схемы сверены с schema/publication/registration кодом. Standalone узлы
RelationType/EmbeddingSpace/RefreshSession не изображены как несуществующие
Cypher relationships. Отдельный Mermaid renderer в этом прогоне не запускался.
Внешние сведения об альтернативах/лицензиях ссылаются на primary sources в
[decisions](decisions.md); сравнительных benchmarks не проводилось.

## Границы результата

Не выполнялись production запуск с пользовательским `.env`, новая загрузка
настоящей Ollama модели, real-provider JSON/Vision/extraction quality smoke,
оценка employer sample, кластер, нагрузка и backup/restore. Автоматический
pull/digest/probe реального bge-m3 описан по коду и ранее проверенному P19,
см. [Docker report](docker.md); этот результат не выдаётся за новый P23 прогон.
Live-команда проверена здесь только в отключённом CI-режиме. **P22 остаётся
открытым** до явного отдельного ключа и разрешённого live-запуска.

Offline-проверки P23 завершены. P20/P21 удалили свои тестовые контейнеры/тома,
P03 остановлен. P23 не добавляет CI и не публикует репозиторий. Следующий шаг —
P24; ограничения P22 должны оставаться видимыми.
