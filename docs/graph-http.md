# Чтение графа (P18)

Все четыре GET требуют Access JWT и возвращают `Page<T>` с `Cache-Control:
no-store`. Обработчики используют синхронный репозиторий в thread pool FastAPI.
Cypher использует фиксированные labels/relationships и параметры значений;
пользовательские UUID и типы не интерполируются в запросы.

| Endpoint | DTO | Порядок |
| --- | --- | --- |
| `/characters` | `Page<Character>` | `character_id ASC` |
| `/characters/by_article/{article_id}` | `Page<Character>` | `character_id ASC` |
| `/relations/types` | `Page<RelationType>` | `kind ASC` |
| `/relations/connected/{character_id}?type=PARENT_OF` | `Page<Relation>` | `relation_id ASC` |

Чтение ограничено `Article.active_revision_id` и успешной (`completed`)
ревизией. Новая queued/processing/failed ревизия не скрывает прежнюю активную.
Персонаж включается через `MENTIONS`, даже если у него нет связей. Исторический
персонаж без активных упоминаний исключён из общего списка, но его UUID остаётся
известным: connected возвращает пустую страницу. Известная статья без active
также возвращает пустую страницу. Неизвестные UUID дают безопасный 404;
неверный UUID, тип или пагинация — безопасный 422.

Для directed connected возвращает исходящие утверждения, для symmetric —
утверждения с любым из концов, включая self-relation один раз. `type` — точное
каноническое имя (`[A-Z][A-Z0-9_]*`); отсутствие фильтра возвращает все типы,
корректный неизвестный тип — пустую страницу. Relation сохраняет UUID статьи,
ревизии, обоих персонажей, confidence и полный массив text/visual evidence.
Утверждения разных статей сохраняют свои `relation_id`. Повторы evidence не
размножают строки; DISTINCT применяется до пагинации. Реестр исторических типов
не определяет список `/relations/types`: в нём только типы активных утверждений.

`limit` по умолчанию равен `limits.page_default`, диапазон — от 1 до
`limits.page_max`. `offset` по умолчанию 0, допустим до `2**63 - 1` (целое Neo4j).
Переполнение отвергается до вызова драйвера. Запрашивается `limit + 1` строка:
`next_offset = offset + limit` только при наличии следующей страницы. За концом
возвращается пустая страница. Snapshot между HTTP-запросами не гарантирован.
Проверка существования и чтение выполняются в одной read transaction.

Проверки:

```powershell
.venv/Scripts/uv.exe sync --frozen
docker compose -f docker-compose.test.yml up -d --wait
.venv/Scripts/uv.exe run --frozen ruff check .
.venv/Scripts/uv.exe run --frozen ruff format --check .
.venv/Scripts/uv.exe run --frozen pyrefly check
.venv/Scripts/uv.exe run --frozen pytest tests/api/test_graph_http.py tests/processing/test_publication.py tests/api/test_article_http.py tests/api/test_auth.py tests/api/test_app.py tests/unit/test_domain.py tests/db/test_database.py --neo4j
docker compose -f docker-compose.test.yml stop
```

Новые HTTP-тесты используют настоящий GraphPublisher и изолированную Neo4j,
включая directed/symmetric/self, повторные evidence и графовые пути, несколько
статей, pending/failed update, удаление последней активной связи, исторических
персонажей, упоминания без связей, пустые страницы, auth/refresh rejection,
404/422, порядок и границы пагинации. Тесты очищают только созданные ими UUID.
Внешний HTTP запрещён; платные API не вызываются. PEP 810 проверяется отдельным
процессом: роуты/DTO зарегистрированы eagerly, Neo4j загружается при обращении.
Для типизированного повторного использования тестового publisher helper tests
оформлен как пакет, корень проекта добавлен в Pyrefly search-path; strict
продолжает охватывать весь source/tests без исключений.
