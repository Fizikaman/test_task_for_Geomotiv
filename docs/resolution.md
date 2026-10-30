# Разрешение идентичности — P12

`resolution.resolve` принимает validated `ExtractionArtifact`, явный `article_id`,
Embeddings, CandidateStore, LLM, BlobStore и Limits. Возвращает `ResolutionArtifact`
и immutable Blob; граф не изменяет. `Neo4jCandidates` — настоящий read-only поиск
`db.index.vector.queryNodes`, параметризованные index/top_k/vector/digest.
Репозиторий вызывается через `asyncio.to_thread`; driver/sessions не разделяются
между операциями. Перед поиском проверяется полная привязка EmbeddingSpace,
размерность и вектор; из результатов исключаются чужие embedding_digest.

## Решение

Embedding input v1: `name + "\n" + description` (raw-text-v1). По одному персонажу
на batch, без зависимости от лимита batch у Ollama. top_k по умолчанию 10,
допустимы 1–100; значение может передать вызывающий pipeline.

1. ANN только предлагает кандидатов. Score сохраняется для аудита, но не
   участвует в решении и не передаётся LLM. Неполная видимость ANN допустима:
   отсутствие результата создаёт отдельного персонажа, а не разрешает слияние.
2. Без LLM допускается единственный кандидат с пересечением полных имён/aliases
   и полностью одинаковым нормализованным описанием (NFKC, casefold, whitespace).
   Описание короче 24 символов отправляется на дополнительную проверку.
   Длина сама по себе ничего не доказывает; требуется полное совпадение контекста.
3. Остальные непустые наборы проверяются отдельным text-вызовом с
   [identity.v1.txt](../src/article_graph/prompts/identity.v1.txt).
   Весь набор кандидатов передаётся JSON-данными отдельно от system instruction.
   Схема `IdentityVerdict` требует `match/no_match/uncertain`, reason и nullable
   candidate_id/source_context/candidate_context. У match все три обязательны;
   у остальных — null. Адаптер проверяет Pydantic JSON, resolver повторно проверяет
   DTO и членство выбранного UUID в наборе.
4. Даже LLM-match требует пересечения полного имени/alias и дословных цитат
   контекста из обоих описаний, каждая минимум 12 символов. Неизвестный UUID,
   выдуманная/короткая цитата, отсутствие alias либо несколько кандидатов
   с одинаковым полным контекстом дают uncertain и отдельный UUID.
5. Перед принятием match повторно читаются актуальные name/description/aliases
   выбранного Character. Изменение/удаление профиля даёт candidate_changed,
   uncertain и отдельный UUID. Старый embedding с cosine=1 не отменяет проверки
   текущего описания. Это проверено на настоящей Neo4j.

Новые UUIDv5 определяются фиксированным namespace, SHA-256 сериализованного
extraction artifact, пространством character/relation и local_id. Для одинакового
artifact новые UUID одинаковы независимо от ANN, LLM reason и повторного запуска;
разные local_id сохраняют отдельные UUID при отсутствии подтверждённого match.
Имена не уникальны. Внутри artifact нет дополнительного нечёткого объединения
локальных персонажей; несколько local_id могут указывать на один подтверждённый
глобальный UUID.

## Результат и повторы

Immutable JSON содержит version/prompt_version, extraction_hash/revision_id,
embedding metadata, исходные профили и векторы, local_id → character_id,
итоговые decision/reason, снимки кандидатов со score и исходный LLM verdict.
Relation evidence и confidence сохраняются. Нет содержимого документов в логах,
ключи не входят в artifact. Хранилище artifact содержит контекст и должно иметь
те же права доступа, что исходные документы.

Повторное вычисление после изменения графа/ответа модели может дать другое
решение о match; обещание стабильности касается новых UUID. P14 должен повторно
использовать уже сохранённый resolution artifact и его hash, а не заново
принимать решения на каждом retry. Immutable Blob сам по себе не регистрирует
этап в Job; это остаётся P14. При одинаковом результате Blob дедуплицируется.

## Связи

`FRIENDS_WITH → FRIEND_OF` (symmetric),
`FATHER_OF/MOTHER_OF → PARENT_OF` (directed),
`CHILD_OF → PARENT_OF` с перестановкой концов.
Неизвестные валидные UPPER_SNAKE_CASE виды сохраняются как property values с
явным directed. Conflict с известным видом, переданным реестром RelationType
или внутри artifact отклоняется до provider calls. Symmetric endpoints сортируются
по UUID. Реестр направлений из существующего графа обязан передавать будущий
pipeline; публикация обязана защищать его от конкурентного конфликта.
Cypher relationship names из kind не создаются. Будущая P13 использует фиксированные
MENTIONS/ASSERTS/SUBJECT/OBJECT; kind остаётся свойством RelationAssertion.

## Бюджеты и ограничения

Применяются characters/relations/evidence_per_relation, llm_calls, llm_input_bytes,
llm_total_input_bytes, provider_timeout_seconds и processing_seconds. При ошибке
не возвращается частичный результат и нет graph writes. Retries отсутствуют.
Pipeline P14 должен учитывать общий бюджет extraction + resolution, поскольку
каждый отдельный вызов resolver получает собственный Limits.

Повторное чтение профиля защищает решение до возврата resolver, но не является
транзакционной гарантией будущей публикации. P13 должен проверить snapshot
выбранного кандидата и определения видов в транзакции вместе с lease/fencing;
внешние вызовы в этой транзакции запрещены. Семантика LLM вероятностная:
grounded quotations не доказывают верность интерпретации. Без новых явных aliases
resolver предпочитает отдельный UUID даже при похожей биографии.

30 октября 2026: Windows, обычный CPython 3.15.2, uv 0.13.6; 36 тестов P12,
включая 3 integration на изолированной Neo4j 5.26.0 Community. Проверены alias,
тёзки/разный контекст, uncertainty, отсутствие кандидатов, replay UUID/Blob,
валидация verdict/цитат, direction/symmetry, бюджеты, lazy runtime и два варианта
устаревания профиля/ANN. Общий pytest --neo4j: 465 passed, 2 skipped (существующие
Windows symlink без прав и Linux RLIMIT_AS). LLM использует HTTP mock, embeddings
синтетические. Реальные Ollama/LLM, Linux-прогон P12 и worker не проверялись.
