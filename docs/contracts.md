# Контракт P02

Это контракт для следующих шагов, а не список уже работающих endpoint-ов.
В P02 реализованы только DTO, их валидация и настройки. БД, JWT, очередь,
обработка документов и AI ещё не реализованы. Все UUID публичные; внутренние
идентификаторы Neo4j в API не появляются. JSON использует snake_case.
Неизвестные поля DTO запрещены, строки содержимого не могут быть пустыми
или состоять только из пробелов. Импорты схем выполняются eager.

## Авторизация

`POST /auth/token`, JSON `{"app_token":"<secret>"}` → 200:
`{"access_token":"<jwt>","refresh_token":"<jwt>","token_type":"bearer","expires_in":900}`.
`POST /auth/refresh`, JSON `{"refresh_token":"<jwt>"}` → 200, такая же новая пара.
`expires_in` — фактический Access TTL в секундах из настроек.
Все бизнес-ручки требуют `Authorization: Bearer <access>`, включая retry.
`GET /health/live` (и будущий `/health/ready`) публичны; OpenAPI/Swagger
не содержат бизнес-данных. Неготовый readiness возвращает 503.

Access TTL по умолчанию 900 секунд, Refresh — 86400. JWT проверяет purpose,
exp, issuer, audience и явно разрешённый сервером алгоритм; алгоритм из JWT
не определяет allowlist. Refresh не заменяет Access и наоборот. App Token
сравнивается безопасно. Открытые токены не сохраняются и не логируются.

Ротация Refresh атомарно погашает старый токен и выпускает новый в том же
семействе. Хранятся хеши токенов, family/session ID и состояние погашения.
Повтор криптографически валидного погашенного Refresh → 401 `refresh_reused`
и отзыв всего семейства, включая новый Refresh победившего запроса.
Два конкурентных refresh дают не более одного успеха; повторной выдачи пары нет.
Невалидный/истёкший токен → 401 `unauthorized`; он не используется для поиска
и отзыва чужого семейства. Уже выданный Access действует до exp (он stateless);
мгновенный отзыв Access не обещается. Нет grace period для reuse.

## Загрузка, версии, состояния

`POST /articles/upload`: multipart, ровно одно из `text` (непустой Markdown)
и `file` (непустой PDF либо UTF-8 Markdown), необязательный UUID `article_id`.
Пустое переданное поле считается переданным и валидируется, а не игнорируется.
Заголовок `Idempotency-Key` необязателен; непустой непрозрачный ключ.
Допустимы `application/pdf`, `text/markdown`, `text/plain` (последние два —
Markdown). Тип подтверждается парсером; имя файла не определяет путь хранения.

Новая работа → 202, например:

```json
{
  "article_id": "00000000-0000-0000-0000-000000000001",
  "revision_id": "00000000-0000-0000-0000-000000000002",
  "job_id": "00000000-0000-0000-0000-000000000003",
  "status": "queued",
  "status_url": "/articles/00000000-0000-0000-0000-000000000001",
  "unchanged": false
}
```

No-op → 200, те же поля с существующими ID и текущим status, `unchanged=true`.
Никаких новых job/revision, LLM/Ollama-вызовов, в том числе для failed.
SHA-256 — от исходных байтов; text кодируется UTF-8 без нормализации пробелов,
переносов и Unicode. Изменённые метаданные PDF меняют hash.

С явным article_id сравниваем hash только с последней входной ревизией этой
статьи: равный → no-op, другой → новая ревизия с номером +1. Неизвестная
статья → 404. Совпадение с чужой статьёй не перенаправляет обновление.
Без article_id ищем совпадение только с последними входными ревизиями,
включая pending/failed; при нескольких выбираем минимальный article_id
в лексикографическом UUID-порядке. Иначе создаём статью с ревизией №1.
Историческое совпадение не даёт no-op. Идентичность по похожести не угадывается.

Idempotency-Key привязан к авторизованному приложению и каноническому запросу:
article_id (включая отсутствие), способу text/file, типу содержимого и hash.
Имя файла не участвует. Тот же ключ/запрос воспроизводит исходные HTTP-код
и body (даже если статус уже изменился); другой запрос → 409
`idempotency_conflict`. Запись сохраняется без TTL до отдельного решения
о retention; idempotency replay имеет приоритет над новым вычислением no-op.

`GET /articles/{article_id}` → 200 DTO Article: article_id,
latest_revision_id, active_revision_id (nullable), job_id, status, error.
Job относится к последней запрошенной ревизии. Публичные статусы: queued,
processing, completed, failed. Только failed содержит безопасную error;
остальные возвращают null. completed означает latest = active.
Неудачное обновление сохраняет прежнюю active revision; при первом failed
active_revision_id=null. Revision хранит article_id, revision_id, number и hash.
Job дополнительно хранит attempt (0 до исполнения), revision_id и job_id.

`POST /articles/{article_id}/retry`: без body, только для последней failed
ревизии → 202 ProcessingResponse, прежние article/revision ID, новый job_id,
queued, unchanged=false. Неизвестная статья → 404; любой иной статус →
409 `retry_conflict`. Конкурентные retry принимают только одну новую работу.
Новая явная попытка получает собственный бюджет job_attempts.

Доставка at-least-once. Повтор одного job использует те же локальные ID и
стабильные ID новых Character; имена не уникальны. Автоповторы ограничены
job_attempts (включает первую попытку), backoff = retry_backoff_seconds ×
2^(номер завершившейся попытки − 1). Повторяются временные 429/5xx провайдера,
сетевые ошибки и timeout; ошибки документа/схемы/лимитов не повторяются.
Не наслаивать независимые retry-бюджеты HTTP SDK и worker.

Публикация результатов и переключение active атомарны: только последняя
запрошенная ревизия и действующий владелец processing lease/attempt.
Старый владелец lease не может публиковать даже ту же ревизию. Вытесненная
ревизия/job получает внутренний terminal superseded; stale completion ничего
не публикует и не меняет Article.status. Это внутренняя операция, HTTP-кода нет.
Повторный commit идемпотентен. Старые утверждения исключаются из всех чтений;
глобальные персонажи из других статей сохраняются. До успешной публикации
читается предыдущий активный граф. Exactly-once платного вызова не обещается.
Файл записывается атомарно до регистрации job/outbox; после сбоя допустим
непривязанный immutable файл, требующий будущей уборки.

## Извлечение и граф

Extraction: `characters: [{local_id,name,description,aliases:[]}]`,
`relations: [{local_id,subject_id,object_id,kind,directed,polarity,evidence,confidence}]`.
Оба списка обязательны, могут быть пустыми. Local ID — непустой ASCII
`[A-Za-z0-9_-]+`, уникален внутри своего списка и стабилен при повторе ревизии.
Character ID и relation ID имеют отдельные пространства имён.
Оба конца каждой relation обязаны ссылаться на character.local_id того же
Extraction. Имена могут совпадать. Неоднозначные персонажи остаются раздельными.
Контекст description обязателен; aliases необязателен. UUID назначаются позже.

Каждая relation имеет непустой evidence. Варианты:

- `{"kind":"text","fragment_id":"chunk-1","page":null,"quote":"дословная цитата"}`;
- `{"kind":"visual","fragment_id":"page-1","page":1,"description":"описание схемы"}`.

Страницы нумеруются с 1; visual требует страницу, Markdown text допускает null.
quote/description не могут быть пустыми; visual не имеет quote и text не имеет
description. fragment_id указывает фрагмент источника. Public Relation добавляет
relation_id, article_id, revision_id и использует UUID концов. Источник всех её
evidence — эта revision. Фактическое наличие цитаты/страницы в документе
проверяет будущий pipeline; DTO не доказывает достоверность текста.

polarity обязательно и допускает только `affirmed`. Отрицание («А не родитель Б»),
предположение («возможно, А родитель Б»), неизвестность и отсутствие сведений
не создают positive relation. Для статьи только с такими фразами relations=[];
сами упомянутые персонажи допустимы. `negated`/`unknown`/`uncertain` в relation
отклоняются, а не преобразуются в affirmed. Нельзя добавлять связи из общих
знаний. confidence необязателен (null), иначе конечное число в [0,1]; высокий
confidence не отменяет требования явного утверждения и evidence.

kind — канонический UPPER_SNAKE_CASE (`[A-Z][A-Z0-9_]*`). FRIEND_OF всегда
directed=false; PARENT_OF всегда directed=true, subject — родитель, object —
ребёнок. Другие типы требуют явного directed, одинакового для одного kind
в реестре типов; конфликт определений отклоняется будущим нормализатором.
Синонимы нормализуются до DTO. Для symmetric концов в хранилище используется
лексикографический UUID-порядок, в извлечении допускается любой порядок.
Параллельные evidence/статьи не теряются при объединении. Self-relations DTO
допускает, если источник явно их утверждает; сходство embeddings не evidence.

## Чтение и пагинация

Обязательные GET возвращают 200 `Page<T>`:
`{"items":[],"limit":20,"offset":0,"next_offset":null}`.
Параметры limit по умолчанию limits.page_default, offset=0; limit >0 и
не выше limits.page_max, offset ≥0. Переполнение → 422, без тихого clamp.
next_offset = offset + limit при наличии следующей страницы, иначе null.
Offset за концом даёт пустую страницу. Snapshot между запросами не обещается.

- `/characters`: уникальные Character только из active revisions,
  сортировка character_id ASC.
- `/characters/by_article/{article_id}`: то же для активной ревизии статьи;
  известная статья без active даёт пустую страницу.
- `/relations/types`: уникальные RelationType из активных утверждений,
  сортировка kind ASC, включая directed.
- `/relations/connected/{character_id}?type=PARENT_OF`: Page<Relation>,
  сортировка relation_id ASC. Для directed возвращаются только исходящие
  (subject_id = запрошенному); для symmetric — оба конца. type необязателен,
  точное каноническое имя; корректный неизвестный тип даёт пустую страницу.
  UUID концов позволяет получать связанных персонажей без потери provenance.

Неизвестный article/character UUID → 404. Известный исторический character
без активных связей даёт пустую страницу; наличие в БД не включает его в
общий список. Один relation assertion возвращается один раз, его evidence
не размножает строки. Утверждения разных статей имеют собственные relation_id.

## Ошибки

Единая форма `{"error":{"code":"...","message":"безопасное описание"}}`.
Никаких содержимого документов, токенов, stack trace и provider response.
P02 задаёт форму; HTTP exception handlers добавляются с endpoint-ами.

| HTTP | code | Причина |
| --- | --- | --- |
| 401 | unauthorized | Нет/невалидный Access, App Token, Refresh; wrong purpose, expired |
| 401 | refresh_reused | Повтор погашенного Refresh, отзыв семейства |
| 404 | not_found | Неизвестный UUID статьи/персонажа |
| 409 | idempotency_conflict | Ключ использован для другого запроса |
| 409 | retry_conflict | Последняя ревизия не failed или retry уже принят |
| 413 | payload_too_large | Входные байты превышают upload_bytes |
| 415 | unsupported_media_type | Неподдерживаемый формат |
| 422 | validation_error | DTO/query/multipart, UUID, оба/ни одного источника, пустой ввод |
| 422 | invalid_document | Синхронно обнаружен повреждённый PDF/невалидный UTF-8 |
| 422 | limit_exceeded | Синхронно обнаружено превышение страниц/пикселей/бюджета |
| 503 | service_unavailable | Нельзя надёжно зарегистрировать работу или хранить auth-сессию |
| 500 | internal_error | Непредвиденная внутренняя ошибка |

Для 401 возвращается `WWW-Authenticate: Bearer`. Недоступная очередь после
durable регистрации outbox не отменяет 202. Ошибка после 202 отражается
через failed при poll с HTTP 200: invalid_document, limit_exceeded,
extraction_invalid, provider_unavailable, processing_timeout либо internal_error.
Внешний 429/5xx не проксируется клиенту как результат уже принятой работы.

## Лимиты

Все численные бюджеты находятся в `Settings.limits` (`Limits` в settings.py).
Env override: `ARTICLE_GRAPH_LIMITS__PDF_PAGES=10` и аналогично для других полей.
Все положительны; page_default ≤ page_max.

| Настройка | Default |
| --- | ---: |
| upload_bytes | 2097152 |
| pdf_pages | 20 |
| page_pixels | 4000000 |
| chunks / llm_calls | 20 / 20 |
| processing_seconds / provider_timeout_seconds | 300 / 30 |
| job_attempts / retry_backoff_seconds | 3 / 2 |
| characters / relations | 100 / 200 |
| evidence_per_relation | 5 |
| page_default / page_max | 20 / 100 |
| access_ttl_seconds / refresh_ttl_seconds | 900 / 86400 |

characters/relations — на полное извлечение ревизии; evidence_per_relation —
на одно утверждение; page_pixels — на одну отрендеренную страницу;
chunks/llm_calls/time — на попытку обработки ревизии (все provider requests,
включая повторы, входят в llm_calls). Хвост документа не отбрасывается молча.
После разбора Extraction потребитель обязан вызвать `check_limits(settings.limits)`;
пагинация аналогично использует PageRequest.check_limits. Остальные бюджеты
будут применяться соответствующими pipeline/HTTP-компонентами в их шагах.
