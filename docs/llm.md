# P05: Chat Completions LLM

`src/article_graph/ai/llm.py` предоставляет протокол `LLM`: типизированный
`generate(request, response_model)` и `extract(content) -> Extraction`.
`ChatCompletionsLLM` использует httpx 0.28.1, загружаемый явно через PEP 810.
Схемы и проверка настроек импортируются eager. После работы нужно вызвать
`await adapter.aclose()` в `finally`. Клиент принадлежит адаптеру и создаётся
в процессе потребителя; подключение к будущему pipeline — P14.

Настройки в `Settings.llm`, env-префикс `ARTICLE_GRAPH_LLM__`, примеры в
`.env.example`. Base URL, API key, text model и output mode обязательны,
без значений по умолчанию. Отсутствующая или пустая обязательная настройка
вызывает ошибку валидации `Settings` при создании приложения, до обслуживания
HTTP. Непустой ключ дополнительно проверяется при создании адаптера, до
создания клиента (`llm_key_required`), без fake или сетевого запроса. Сам API
пока не создаёт LLM-клиент. Транспорт для тестов передаётся только явно.
Секреты соседнего проекта не используются.

Поддерживается ограниченный контракт `/chat/completions`: `stream=false`, один
assistant choice, `finish_reason=stop`, строковый JSON content. Текст передаётся
как text blocks, изображения PNG/JPEG/WebP — как image_url blocks с base64 data
URL. Проверка пригодности/размера изображения относится к будущему preprocess.
Идентификаторы фрагментов и страницы потребитель передаёт в text blocks рядом
с содержимым/изображениями. Визуальный запрос требует явно заданный vision_model;
настроенная text model не считается автоматически поддерживающей vision.

Режим выбирается по документированным возможностям конкретного провайдера:

- `json_schema`: отправляется Pydantic JSON Schema в response_format.json_schema.
  Не включается провайдерский strict=true: его ограниченный диалект часто
  несовместим с defaults/discriminators Pydantic. Поддержку схемы нужно проверить
  для конкретной модели. Независимо от серверных гарантий ответ валидируется локально.
- `json_object`: схема передаётся в system prompt, response_format=json_object.
  Этот режим нужно выбрать явно, если провайдер не поддерживает JSON Schema.

Оба режима используют `model_validate_json(strict=True, extra="forbid")`,
включая вложенные DTO и семантические валидаторы Extraction. Не исправляются
невалидный JSON, Markdown fences, типы и усечённые ответы. После extraction
проверяются лимиты characters/relations/evidence. Подтверждение цитат по самому
документу и объединение фрагментов относятся к следующим шагам.

Полные промпты находятся в package resources:
`src/article_graph/prompts/extraction.v1.txt` и `json.v1.txt`. Изменение поведения
требует новой версии; будущий checkpoint должен учитывать версии промптов.

Один вызов адаптера — одна HTTP-попытка, HTTP transport retries=0, без SDK,
без автоматического schema fallback, без redirect и без proxy из окружения.
`LLMError.retryable=true` только для HTTP 429/5xx, timeout и транспортных ошибок.
Прочие HTTP-коды, refusal/content_filter, length/truncation, неполная оболочка,
пустой content, ошибка валидации и превышение лимитов — permanent. Ошибки имеют
фиксированные коды, не содержат body/ключ/документ и не сохраняют контекст
исходного HTTP/Pydantic exception. Провайдерские тела не логируются.

Будущий worker (P15) владеет общим `limits.job_attempts` и backoff из контракта;
он не должен наслаивать ещё один HTTP retry. До P15 повторов нет вовсе.
Каждый вызов должен учитываться в llm_calls будущим pipeline, включая повторы.
Exactly-once для платного вызова при crash после ответа не гарантируется.

HTTP timeout — минимум LLM timeout и limits.provider_timeout_seconds,
для connect/read/write/pool. Это timeout операций HTTP; общий deadline обработки
ограничит pipeline. Ответ читается с ограничением max_response_bytes после
декомпрессии. Настройка max_tokens ограничивает генерацию у провайдера.

Совместимость любого OpenAI-подобного API не обещается. Другие endpoints,
tool calls, streaming, провайдерские thinking-параметры и автоматическое
определение возможностей не реализованы. DeepSeek приведён только как пример
для ручной проверки в `.env.example`; его live JSON/vision возможности не проверены.
BroLango `infrastructure/ai/deepseek.py` просмотрен точечно; биллинг, учёт
токенов и отдельный retry framework не переносились.

Проверки без платных запросов: `uv run --frozen pytest tests/ai/test_llm.py`.
HTTP MockTransport проверяет реальные сериализованные payload, image bytes,
режимы JSON, строгую валидацию, ошибки и единственную попытку. Отдельный
subprocess проверяет runtime lazy import. Live smoke остаётся opt-in P22;
Linux/container проверка этой реализации ещё не выполнена.

29 октября 2026, Windows CPython 3.15.2 / uv 0.13.6: `uv sync --frozen`,
`uv run --frozen ruff check .`, `uv run --frozen ruff format --check .`,
`uv run --frozen pyrefly check` прошли. `uv run --frozen pytest`:
187 passed (включая 63 P05), 28 Neo4j integration skipped без `--neo4j`.
Включены проверки отсутствующих/пустых обязательных env: ошибка возникает
до обслуживания HTTP, настройки провайдера не подставляются автоматически.
