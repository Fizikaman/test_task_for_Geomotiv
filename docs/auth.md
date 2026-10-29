# Авторизация P04

`POST /auth/token` принимает JSON `{"app_token":"<secret>"}` и возвращает
`{"access_token":"<jwt>","refresh_token":"<jwt>","token_type":"bearer","expires_in":900}`.
`POST /auth/refresh` принимает `{"refresh_token":"<jwt>"}` без Access и возвращает
новую пару. Оба ответа имеют `Cache-Control: no-store` и `Pragma: no-cache`.

Обязательны `ARTICLE_GRAPH_APP_TOKEN`, `ARTICLE_GRAPH_JWT_ACCESS_KEY` и
`ARTICLE_GRAPH_JWT_REFRESH_KEY`: три разных случайных секрета минимум по
32 символа. Приложение не генерирует запасные ключи. Настройки issuer/audience
и стабильного субъекта приложения: `ARTICLE_GRAPH_JWT_ISSUER`,
`ARTICLE_GRAPH_JWT_AUDIENCE`, `ARTICLE_GRAPH_APP_ID`; примеры в `.env.example`.
App Token сравнивается через constant-time сравнение SHA-256 digest.

Алгоритм фиксирован сервером: HS256, независимо от заголовка JWT. Ключи Access
и Refresh разделены; дополнительно проверяется `purpose`. Обязательны и
проверяются `exp`, `iat`, `nbf`, `iss`, `aud`, `sub`, `jti`, `family_id`, `purpose`.
Время — целые числа, UUID — канонические строки, audience — одна строка.
TTL задаётся `Settings.limits`: Access 900, Refresh 86400 секунд по умолчанию.
Каждая ротация начинает новый Refresh TTL.

В Neo4j `RefreshSession` хранит только `session_id`, `family_id`, `app_id`,
SHA-256 `token_hash`, `expires_at` (Unix seconds), `used`, `revoked`.
Используются constraints/indexes P03, новый init-schema не требуется.
Первый session_id равен family_id и служит корнем для блокировки всех поколений.
Ротация в одной managed write transaction получает write lock корня через
временное свойство, затем отдельным запросом читает состояние, погашает старый
токен и создаёт новую сессию. Ошибка создания откатывает погашение.

Повтор валидного погашенного Refresh фиксирует отзыв **всех** сессий семейства
в БД и лишь после commit возвращает `401 refresh_reused`. Grace period нет.
Отзыв и ротация потомков используют один корень, поэтому потомок не может
появиться после отзыва. Два конкурентных refresh одного токена дают не более
одного успеха; Refresh победителя также отзывается проигравшим запросом.
Невалидный JWT не используется для поиска/отзыва семейства. Неизвестная,
истёкшая или отозванная сессия возвращает `401 unauthorized`.
Access остаётся действительным до exp: мгновенного отзыва Access нет.
Автоматическая очистка сессий не реализована. Корень нельзя удалять, пока
могут использоваться его потомки; удаление истории требует отдельной retention-политики.

Для будущих бизнес-роутеров подключать общую dependency из `auth_routes.py`:

```python
from fastapi import APIRouter, Depends
from article_graph.auth_routes import Access, require_access

router = APIRouter(dependencies=[Depends(require_access)])
# Если обработчику нужен app_id для Idempotency-Key, принять access: Access
# и использовать access.sub. JWT не проверять повторно собственным кодом.
```

Публичны `/auth/token`, `/auth/refresh`, `/health/live`, `/health/ready`,
Swagger/OpenAPI. Бизнес-роутеров в P04 ещё нет. Проверка dependency выполняется
на маршруте, зарегистрированном только тестом.

401 имеет `WWW-Authenticate: Bearer`. Auth-ошибки, ошибки DTO/JSON и ошибки
драйвера возвращаются в форме `ErrorResponse`, без исходных значений или
исключений. Отказ хранения auth-сессии возвращает `503 service_unavailable`.
JWT и ключи не пишутся в БД/логи; неправильные запросы не отражаются в ответах.

29 октября 2026, обычный CPython 3.15.2, uv 0.13.6, PyJWT 2.15.1:

- Windows: `uv sync --frozen`, Ruff check/format check, Pyrefly strict (0 errors),
  `uv run --frozen pytest --neo4j` — **152 passed**, включая 28 integration на
  изолированной Neo4j 5.26.0 Community. Из них 79 тестов P04, 10 с реальной БД.
- Проверены неверный App Token, обязательные/невалидные claims, expired,
  tampered, wrong-purpose, wrong-key, HS384/none, ротация, replay, rollback,
  пять гонок двух HTTP refresh и гонка replay предка с ротацией потомка.
- Проверены отсутствие секретов в ответах/логах, безопасные 422/503, публичный
  health/OpenAPI и runtime lazy import PyJWT в чистом процессе.
- Linux `python:3.15.2-slim`: frozen sync, Ruff check/format check, Pyrefly strict
  и **124 passed, 28 skipped**. Integration в Linux-клиенте не запускались;
  реальные транзакции проверены Windows-клиентом против Linux Neo4j.
  В Linux-контейнер передавались только код, тесты и файлы зависимостей.

Команды изолированного стека — в README. Тесты P04 создают уникальный app_id
и очищают только его RefreshSession в жёстко заданной тестовой БД.
Платные API и рабочая БД не используются.

Первичные источники: [PyJWT decode и обязательные claims](https://pyjwt.readthedocs.io/en/latest/api.html),
[Neo4j write locks и временное свойство](https://neo4j.com/docs/operations-manual/current/database-internals/concurrent-data-access/).
