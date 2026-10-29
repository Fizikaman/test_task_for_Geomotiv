# P01: совместимость Python 3.15

Дата проверки: 29 октября 2026. Обычный CPython 3.15.2, Windows x86_64 и Linux
x86_64 в официальном `python:3.15.2-slim`. Free-threaded/JIT и глобальный lazy mode
не включались. Linux-образ использован только для одноразовой проверки, Dockerfile
и сервисы остального стека не создавались.

## Основной каркас

| Компонент | Версия | Фактическая проверка на Windows и Linux |
| --- | --- | --- |
| uv | 0.13.6 | Установка CPython, lock, `sync --frozen` |
| Ruff | 0.17.3 | Lint и format, target `py315`, оба вида lazy syntax |
| Pyrefly | 1.4.0 | `preset = "strict"`, исходники и тесты, оба вида lazy syntax |
| pytest | 9.2.0 | 6 тестов: health, настройки, .env/env, отложенные импорты |
| FastAPI / Starlette | 0.162.0 / 1.10.0 | Factory, OpenAPI, TestClient и настоящий HTTP-сервер |
| Pydantic / pydantic-core | 2.14.1 / 2.50.1 | Native wheels, runtime-валидация обязательных настроек |
| pydantic-settings | 2.16.2 | Обязательный SecretStr, .env, приоритет окружения |
| Uvicorn | 0.54.0 | Запуск factory и HTTP 200 от `/health/live` |
| httpx2 | 2.16.0 | Поддерживаемый клиент Starlette TestClient |

`lazy import uvicorn` в entry point откладывает загрузку сервера до запуска.
Тест в чистом дочернем процессе проверяет отсутствие модуля до обращения и
появление после обращения. Также исполняется `lazy from fractions import Fraction`
с вычислением результата; в самом тестовом модуле используется типизированный
`lazy from subprocess import run`. Регистрация роутеров и проверка настроек eager.

Основные версии и хеши зафиксированы в `uv.lock`. Установленный изначально
uv 0.11.23 видел только старую beta-сборку 3.15; использован локальный uv 0.13.6,
без изменения глобальной установки. Старый httpx заменён на httpx2, рекомендованный
использованным Starlette TestClient.

## Будущие зависимости: только короткая проверка

Установлены в отдельные временные окружения. В pyproject/lock приложения не
добавлены; адаптеры и подключения к сервисам не реализованы.

| Библиотека | Проверенная версия | Smoke на Windows и Linux |
| --- | --- | --- |
| Neo4j driver | 6.4.1 | Импорт, создание и закрытие driver без соединения |
| Celery / Kombu / Redis client | 5.6.3 / 5.6.2 / 6.4.0 | Импорт Redis transport, локальная eager-задача, создание клиента без I/O |
| PyJWT | 2.15.1 | HS256 encode/decode с проверкой срока и разрешённого алгоритма |
| pypdfium2 (PDF renderer) | 5.14.0 | Native wheel, создание PDF, повторное открытие, рендер страницы 72×72 |

Последний Redis client 8.1.0 несовместим с extra `kombu[redis]==5.6.2`, которому
нужен Redis `<6.5`. Без явного ограничения резолвер выбрал Kombu 5.7.0rc1.
Для smoke выбрана полностью стабильная связка Redis 6.4.0 + Kombu 5.6.2.
Эти ограничения нужно повторно проверить при реализации очереди.

Успех smoke подтверждает базовое выполнение на 3.15, но не полную поддержку
всех функций upstream. Не проверялись настоящие Neo4j/Redis, worker pool,
доставка задач, JWT-сессии, произвольные PDF и платформы ARM. Celery worker
в дальнейшем проверять в Linux. Серверы инфраструктуры для P01 не запускались.

Первичные источники: [uv Python support](https://docs.astral.sh/uv/reference/policies/python/),
[Ruff и Python 3.15](https://astral.sh/blog/python-3.15),
[Pyrefly strict preset](https://pyrefly.org/en/docs/configuration/#preset),
[Pydantic](https://pypi.org/project/pydantic/2.14.1/),
[Neo4j](https://pypi.org/project/neo4j/6.4.1/),
[Celery](https://pypi.org/project/celery/5.6.3/),
[Kombu](https://pypi.org/project/kombu/5.6.2/),
[Redis](https://pypi.org/project/redis/6.4.0/),
[PyJWT](https://pypi.org/project/PyJWT/2.15.1/),
[PDFium](https://pypi.org/project/pypdfium2/5.14.0/),
[PEP 810](https://peps.python.org/pep-0810/).

## P03: реальное подключение Neo4j

29 октября 2026: в зависимости добавлен `neo4j==6.4.1`, frozen lock обновлён.
На Windows CPython 3.15.2 / uv 0.13.6 прошли frozen sync, Ruff lint/format,
Pyrefly strict без ошибок и предупреждений; `pytest --neo4j` — 73 passed
(55 unit/runtime + 18 integration). Integration-набор повторён успешно на
сохранённом тестовом томе. `docker compose -f docker-compose.test.yml config
--quiet` и `up -d --wait` прошли, Neo4j 5.26.0 Community healthy.

В Linux `python:3.15.2-slim` с тем же frozen lock/uv прошли установка,
55 unit/runtime тестов и `init-schema` с подключением к настоящей тестовой БД.
Секреты/.env в Linux-контейнер не монтировались. Полный integration-набор
в Linux не запускался; он выполнен с Windows-клиента против Linux Neo4j.
Локальный subprocess-тест подтверждает, что импорт API не загружает `neo4j`,
а обращение к lazy symbol исполняет импорт. Required readiness выполняется
явно до обслуживания API. Проверки и ограничения схемы: [neo4j.md](neo4j.md).
AI/очереди и рабочие данные не использовались.

## P04: JWT и ротация Refresh

PyJWT 2.15.1 теперь добавлен в зависимости и frozen lock. 29 октября 2026:
Windows CPython 3.15.2 / uv 0.13.6 — frozen sync, Ruff check/format check,
Pyrefly strict (0 errors), 152 passed с `--neo4j` (28 integration).
Linux `python:3.15.2-slim` — те же frozen sync, Ruff/Pyrefly и
124 passed, 28 integration skipped. Реальные транзакции выполнялись
Windows-клиентом против изолированной Linux Neo4j 5.26.0 Community.
Lazy PyJWT проверен в чистом процессе: отсутствует после импорта приложения,
загружается при выдаче JWT, encode/decode успешно выполняются.
Политика replay, проверки и ограничения: [auth.md](auth.md).
