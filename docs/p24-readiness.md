# P24: CI и готовность к сдаче

Проверка 30 октября 2026, Windows x86_64 и Linux Docker. Обычный CPython
3.15.2, GIL включён, free-threaded/JIT/global lazy mode не используются;
uv 0.13.6 и неизменённый frozen lock. Production-код и зависимости в P24
не менялись; исправлен один обнаруженный дефект E2E-теста. Рабочий `.env`
не читался, платных запросов не было.

## CI

[Workflow](../.github/workflows/ci.yml): push, pull request и ручной запуск,
Ubuntu 24.04, `contents: read`, checkout без сохранения credentials, Actions
закреплены полными SHA. Два job с timeout 10/25 минут:

- `quality`: `uv sync --frozen`, проверка CPython/GIL/JIT, Ruff lint/format,
  Pyrefly strict, unit/runtime и проверка запрета live smoke даже с opt-in flag.
- `infrastructure`: frozen install, отдельная настоящая Neo4j и 181 DB-тест,
  затем последовательно P20 и P21 с настоящими Redis/worker/dispatcher/API.
  Удаление только тестовых проектов выполняется также после ошибок/отмены.

В CI нет provider secrets, загрузки моделей или paid API: AI обслуживают
HTTP doubles. Worker/dispatcher тестовых стеков имеют только внутреннюю сеть.
P20 проверяет recovery/duplicates/outage, P21 — настоящий HTTP, refresh,
Markdown/no-op/update и PDF image bytes до публикации графа.

Версии и inputs Actions сверены с первичными источниками:
[setup-uv](https://github.com/astral-sh/setup-uv/releases/tag/v10.3.3),
[checkout](https://github.com/actions/checkout/releases/tag/v7.0.1),
[uv GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/).
Workflow проверен actionlint 1.7.12. Сам GitHub Actions runner не запускался:
репозиторий не публиковался, коммиты/ветки/PR не создавались.

## Фактические проверки

На Windows использован проектный `.venv/Scripts/uv.exe`, `UV_CACHE_DIR=.cache/uv`.
Pytest/Docker выполнялись вне sandbox: внутри него pytest не может создать
временный каталог, а Docker API запрещён. Это ограничение среды, не успешный
тестовый результат. Один запуск uv без перенаправления cache также упёрся
в sandbox; обязательные команды с проектным cache проходят.

| Команда | Результат |
| --- | --- |
| `uv sync --frozen` | 52 packages checked |
| `uv run --frozen ruff check .` | Passed |
| `uv run --frozen ruff format --check .` | Passed |
| `uv run --frozen pyrefly check` | 0 errors; одно прежнее предупреждение |
| `uv run --frozen pytest -q` | Финальный: 458 passed, 194 skipped; 35.44 с |
| `uv run --frozen pytest --neo4j -q` | 639 passed, 13 skipped; 120.65 с |
| `uv run --frozen pytest -m infrastructure --infrastructure -q` | 6 passed, 646 deselected; 168.38 с |
| `uv run --frozen pytest -m e2e --e2e -q` | После исправления: 2 passed, 650 deselected; 54.61 с |
| Linux: frozen sync, Ruff lint/format, Pyrefly | Passed, 0 errors; то же предупреждение |
| Linux: `pytest -m 'not integration and not e2e and not live_provider' -q` через frozen uv | 459 passed, 1 skipped, 192 deselected; 217.42 с |
| Linux: `pytest tests/test_live_provider.py --live-provider -q` через frozen uv, `CI=true` | 3 skipped; 3.20 с |
| `actionlint .github/workflows/ci.yml` | Passed |
| `uv run --frozen pytest tests/test_documentation.py -q` | 3 passed; 0.53 с |

Linux quality-команды выполнены в `python:3.15.2-slim`, с лимитами 2 CPU/1 GiB,
на копии только исходников, тестов и публичной документации. Единственный skip —
Windows-only junction test; Linux symlink и RLIMIT_AS выполнены. В Windows
полном DB-наборе пропущены 11 opt-in process/live случаев и два платформенных.
Оба синтаксиса PEP 810 проверены Ruff/Pyrefly и subprocess-тестами, включая
фактическое отложенное исполнение. Роутеры/OpenAPI, задачи и обязательная
startup validation остаются eager. Warning Pyrefly — прежняя deprecated
аннотация `AsyncIterator` у `@asynccontextmanager` в `article_routes.py:67`.
Отдельная диагностическая команда `pyrefly check --min-severity=warn` возвращает
exit 1 на этом warning; обязательная команда и CI используют стандартный порог.

## Чистый Compose

Production Compose с публичным acceptance override запущен на пустом проекте:

```powershell
$p24 = @('--env-file', 'tests/compose.env', '-p', 'geomotiv-p24-acceptance',
  '-f', 'docker-compose.yml', '-f', 'docker-compose.acceptance.yml')
docker compose @p24 config --quiet
docker compose @p24 up -d --build --wait --wait-timeout 1800
docker compose @p24 ps -a
```

До старта проверено отсутствие контейнеров и томов с этим project label.
Созданы четыре новых named volumes; существующий model volume P19 не подключался.
Оба init завершились с exit 0, все шесть постоянных сервисов healthy.
Автоматически загружен настоящий `bge-m3:567m`, проверены полный digest,
1024-мерный embedding и Neo4j vector index. API runtime подтвердил CPython
3.15.2/GIL/no JIT/UID 10001 и отсутствие Celery/PDF renderer при импорте API.
Provider URL — закрытый loopback-порт из тестового env; live LLM не вызывается.

Затем worker/dispatcher остановлены, выполнен существующий
[compose_smoke.py](../tests/compose_smoke.py) в режимах `seed` и `verify`
до и после stop/up всех процессов. Оба прошли: HTTP upload, общий immutable
source, реальный embedding/index, fixture публикации через lease/GraphPublisher,
активный граф при queued update и реальное сообщение Celery/Kombu в Redis
сохранились. Публикация fixture не выдаётся за извлечение настоящим LLM.
После проверки `down --volumes --remove-orphans --timeout 10` удалил только
новый P24 проект и четыре его тома, включая повторно загруженную модель.

## Найденный дефект

Первый HTTP E2E: 1 passed, 1 failed за 55.37 с. PDF pipeline завершился успешно,
но тест искал исходящую `PARENT_OF` у `characters[0]`, то есть первого UUID,
которым мог оказаться Bob. По контракту направленная связь возвращается только
для subject Alice. [Тест](../tests/test_e2e.py) теперь выбирает Alice по имени
и проверяет оба конца Alice → Bob; production-запрос не менялся.
Повторный полный прогон после исправления: 2 passed за 54.61 с. Нет retry или
ослабления проверок в CI, исходное падение не скрыто.

## Состав файлов и ограничения

Проверяются текущие tracked-файлы и новые deliverables через `git ls-files`,
`git status`, staged/unstaged diff и прямое чтение новых файлов. Исходные
staged additions `AGENTS.md`/`IMPLEMENTATION_PROMPTS.md` сохранены; индекс не менялся.
Нет private env/key files, временных PDF/DOCX/log/archive файлов, bytecode или
моделей в проверяемом составе; максимальный файл меньше 100 KiB.

Gitleaks 8.30.1 с `--redact` сканирует только копию этих файлов, не рабочий
каталог/`.env`. Первичный scan: четыре `generic-api-key` в `.env.example`
(строки 14–16, 23), вручную сверенные с явно публичными demo-credentials.
После triage повторный scan с временным baseline ровно этих четырёх находок
вернул exit 0 / no leaks; целые файлы/каталоги не исключались.
Неожиданных секретов не найдено. Это проверка текущей сдаваемой версии,
не аудит всей истории Git. Инструменты/отчёты проверки находятся только в
игнорируемом `.cache`; в приложение зависимости не добавлены.

`git diff --check` и `git diff --cached --check` прошли; новые workflow/отчёт
прочитаны отдельно, поскольку обычный diff не показывает untracked-файлы.
После P20/P21 тестовые контейнеры/тома очищены fixtures; существующая P03
остановлена с сохранением её тома. После `compose stop` у P03 exit 137,
`OOMKilled=false`; восстановление её сохранённого тома после этой остановки
повторно не проверялось. Рабочие/BroLango контейнеры и данные
не изменялись. P24 завершён; текущая сдаваемая версия содержит 121 tracked/новый
файл, самый большой — `IMPLEMENTATION_PROMPTS.md` (меньше 100 KiB).

**P22 остаётся открытым**: нет отдельного разрешённого provider key, поэтому
реальные JSON mode, Vision, качество извлечения, embedding и persistence
результатов настоящего LLM не проверены. Проверка реальной Ollama выше не
заменяет этот smoke. Employer sample отсутствует, fixtures синтетические.
Нет новых гарантий throughput/p95, HA, backup/restore или exactly-once
оплачиваемых вызовов после crash. Следующий узкий шаг — завершить P22 с отдельным
ключом и явным разрешением; публикацию после review выполняет пользователь.
