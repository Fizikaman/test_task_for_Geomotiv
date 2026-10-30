# P17: upload, статус и ручной retry

Все три маршрута требуют Access Bearer JWT. Проверка Access выполняется до
чтения multipart. Refresh не подходит для бизнес-маршрутов.

- `POST /articles/upload`: multipart с ровно одним `text` или `file`,
  необязательным UUID `article_id` и заголовком `Idempotency-Key`.
- `GET /articles/{article_id}`: latest revision, active revision, текущий Job,
  status и безопасная error. Неудачное обновление сохраняет прежнюю active.
- `POST /articles/{article_id}/retry`: latest failed revision получает новый
  Job и Outbox; конкурирующая либо неподходящая попытка получает 409.

Upload использует существующие `RegistrationService`, `LocalBlobStore` и
`ArticleRepository`; retry и status используют тот же repository. Новая работа
возвращает 202, no-op — 200, конфликт ключа — 409. Replay ключа воспроизводит
первоначальные HTTP-код и body, даже после обновления статьи. Регистрация
атомарно создаёт Job и pending Outbox; доступность broker не проверяется.

Для API задайте `ARTICLE_GRAPH_BLOB_ROOT` (default `./.data/blobs`). Worker
должен видеть тот же каталог через `ARTICLE_GRAPH_WORKER__BLOB_ROOT`;
оба значения приведены в `.env.example`. Имя клиентского файла не участвует
в построении пути. Сначала сохраняется immutable source, затем выполняется
транзакция Neo4j; после ошибки регистрации может остаться непривязанный blob.

Markdown-файлы принимаются как `text/markdown` или `text/plain`, PDF — как
`application/pdf`. Формат проверяет существующий storage validator; PDF preflight
выполняется в ограниченном дочернем процессе. Извлечение текста/изображений,
pipeline, LLM, embeddings и доставка в Celery в HTTP-маршрутах не запускаются.
Синхронная регистрация выполняется в thread pool, вне API event loop; multipart
закрывается после завершения работы с его файлами.

Размер источника ограничен `limits.upload_bytes`, включая UTF-8 байты поля text.
До multipart-разбора весь HTTP body ограничен `upload_bytes + 64 KiB`:
дополнительное место предназначено для boundary, заголовков и article_id.
Content-Length не считается доказательством размера; chunked body проверяется
тем же способом. Body буферизуется в памяти в этих пределах. Парсер допускает
не более одного файла и двух текстовых полей. Дубликаты, неизвестные поля,
оборванный multipart, неверный UTF-8 и не-UTF-8 charset полей отклоняются.
Переданное пустое поле учитывается при проверке ровно одного источника.

Ошибки имеют контрактную форму `error.code/message`; исключения и введённые
значения не копируются в ответ. Статус failed возвращает код из Job и фиксированное
`Processing failed`, даже если сохранённое сообщение содержит небезопасные данные.
Source locator, hash, текст документа, credentials и traceback не входят в status
DTO. Upload/status/retry и ошибки возвращают `Cache-Control: no-store`.
Storage/Neo4j failure дают 503, непредвиденная ошибка — безопасный 500.

## Проверено 30 октября 2026 года

Обычный CPython 3.15.2, uv 0.13.6, frozen sync, Ruff lint/format и Pyrefly strict
(0 errors). Целевой набор `test_article_http`, `test_registration`, `test_auth`,
`test_app`, `test_blob_store` с `--neo4j`: **185 passed, 1 skipped**.
Пропуск — существующий Windows symlink-тест без прав на создание ссылок.

39 новых HTTP-проверок, включая 12 integration на настоящей изолированной
Neo4j 5.26.0 Community: Markdown text/file и PDF registration, точный UTF-8 hash,
no-op/update/idempotency replay/conflict, active при failed update, конкурентный
retry и durable Outbox, auth, UUID, multipart, размеры, PDF page limit и безопасные
ошибки. Внешние HTTP transports запрещены fixture: платные API не вызывались.
Отдельный subprocess проверяет eager router и lazy optional/processing imports;
существующие storage-тесты проверяют PDFium в дочернем процессе.

Проверки выполнены Windows-клиентом вне sandbox из-за кэша uv и доступа к Docker.
Полный pytest, Linux, настоящий Redis/worker и нагрузка в P17 не запускались.
Тестовая Neo4j остановлена, том сохранён. Следующий шаг — P18, чтение активного графа.
