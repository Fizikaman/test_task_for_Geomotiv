# P21: HTTP E2E

Из корня проекта (обычный CPython 3.15.2, uv 0.13.6, Docker Compose v2):

```powershell
uv run --frozen pytest -m e2e --e2e
```

На текущем Windows-хосте закреплённый uv также доступен как
`.venv/Scripts/uv.exe`. Без `--e2e` сценарии пропускаются.

Fixture собирает production Dockerfile и запускает отдельный проект
`geomotiv-p21-e2e` из `docker-compose.e2e.yml`. Настоящие HTTP API,
Celery prefork worker, dispatcher, Neo4j и Redis работают в контейнерах.
Хостовый httpx обращается к API на `127.0.0.1:38000`, управление double —
к `127.0.0.1:38080`. TestClient, eager Celery и ручная публикация графа
не используются. Тесты не требуют модели или оплачиваемых API.

Первый сценарий получает Access через `/auth/token`, загружает Markdown-файл,
poll-ит статус и проверяет все четыре GET графа. Повтор файла возвращает
тот же Job/revision с `unchanged`, без дополнительных вызовов AI.
После HTTP refresh новый Access используется для обновления с `article_id`:
Alice–Bob заменяется Alice–Carol, прежняя связь исчезает из активного графа.
Без Access запрещены upload, status и все четыре GET; Refresh вместо Access
также отклоняется.

Второй сценарий загружает синтетический PDF: текст содержит только имена,
отношение выражено красной векторной стрелкой. Настоящий renderer worker
передаёт PNG в Chat Completions double. Double проверяет vision model,
base64 bytes, PNG chunks/CRC, размер, RGB scanlines и пиксели стержня/наконечника
стрелки. Только после этого возвращает отношение с visual evidence страницы 1.
Счётчик принятых изображений и visual evidence проверяются через HTTP.
Обычные тесты `tests/e2e/test_e2e_double.py` дополнительно отправляют double запросы
без изображения, с неверными/обрезанными bytes и валидным PNG без стрелки:
все должны получить BAD_REQUEST. Настоящая модель не участвует; это проверка
транспорта изображения и публикации, а не качества AI.

Каждый HTTP-запрос ограничен 5–10 секундами; polling — monotonic deadline
90 секунд, Compose startup — 180 секунд, каждая Compose-команда — 300 секунд.
При падении pytest показывает безопасный статус задачи, состояние контейнеров
и последние 80 строк логов API/worker/dispatcher/double/init-schema.
Access log API выключен, double не логирует запросы, тела документов и токены.

Рабочая `.env` не загружается: используется публичный `tests/e2e.env`,
параметры провайдеров и тестовые credentials закреплены в Compose.
Worker/dispatcher/Neo4j/Redis находятся только во внутренней сети;
API и double дополнительно публикуют loopback-порты через управляющую сеть.
Отдельный named volume содержит blobs, данные Neo4j/Redis эфемерные.
В начале и в `finally` выполняется `down --volumes --remove-orphans`
только для `geomotiv-p21-e2e`. Проекты P03/P19/P20 и рабочие данные не затрагиваются.
Повторная команда восстанавливается после прерванного запуска; параллельные
запуски с тем же project name запрещены. Образы остаются в Docker cache.


## Проверено 30 октября 2026

На Windows-хосте с Linux Docker-контейнерами финальная команда E2E прошла
дважды подряд: 2 passed / 631 deselected за 79.80 и 81.39 секунды.
Полный обычный pytest: 442 passed, 191 skipped; сюда входят пять проверок
HTTP double и runtime-проверки PEP 810 lazy imports. Frozen sync, Ruff check,
Ruff format --check и Pyrefly strict прошли (0 ошибок типов, прежнее
предупреждение AsyncIterator). Контейнеры и тома P21 удалены после прогонов.

В первом прогоне был HTTP ReadTimeout в Markdown polling; PDF прошёл.
В раннем общем pytest один существующий cancellation-тест extraction упал
по peak concurrency; отдельный extraction-набор затем прошёл 32/32,
а повторный полный набор — без ошибок. Production-код этих тестов не менялся.
Healthcheck E2E API использует liveness после обязательной startup validation,
чтобы периодические readiness embeddings не искажали no-op counters.
Диагностика падения прикрепляется к упавшему сценарию, включая первый
при успешном втором сценарии.

Linux-хост и отдельные P03/P20 opt-in наборы в P21 не проверялись.
Linux worker/dispatcher, PDF renderer, HTTP API и настоящие Neo4j/Redis
проверены в Compose. Качество реального LLM/Ollama относится к P22;
платные API в P21 не вызывались. Следующий шаг — P22.
