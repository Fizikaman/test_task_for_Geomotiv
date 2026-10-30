# P13: атомарная публикация

`GraphPublisher.publish(Publication(...))` принимает уже подготовленный
`ResolutionArtifact`, article/job UUID, положительный `attempt` и UUID
`attempt_token`. Провайдеры, файловое хранилище и HTTP не вызываются.
Валидация ссылок, лимитов, векторов и сериализация graph payload/evidence
выполняются до транзакции. Evidence хранится в `RelationAssertion.evidence`
как JSON-массив с discriminator text/visual; confidence, kind, directed,
article/revision UUID и оба endpoint UUID — отдельные свойства.

В одной managed write transaction:

1. Блокируются Article, затем Job. После блокировки проверяются настоящие
   HAS_REVISION/HAS_JOB, принадлежность article/revision/job, текущий `r.job_id`,
   status=processing, attempt, attempt_token и действующий lease.
2. Только последняя запрошенная revision может публиковаться. Вытесненная
   получает revision/job status=superseded без изменений активного графа.
   Истёкший lease или чужой attempt ничего не завершает и не публикует.
3. Блокировка `RegistrationLock(name='graph-publication')` сериализует публикации
   разных статей, защищая отсутствие новых UUID и определения видов отношений.
   Она независима от `RegistrationLock(name='uploads')`; publisher не берёт uploads.
   Затем блокируются EmbeddingSpace и существующие Character в UUID-порядке.
   Проверяются metadata пространства и текущий профиль каждого выбранного
   match-кандидата против snapshot в артефакте. Изменённый/удалённый кандидат
   отклоняется с `candidate_changed`; повторный resolver внутри транзакции запрещён.
4. Lease проверяется снова после ожидания блокировок. Пакетные UNWIND-запросы
   создают новые Characters, MENTIONS по local_id, RelationAssertions и
   ASSERTS/SUBJECT/OBJECT. Профили/векторы существующих совпавших Characters
   сохраняются. `RelationType.kind` уникален; directed нельзя поменять даже
   после исчезновения последнего активного утверждения этого вида.
5. Повторно проверяется реальное серверное время lease; завершение Job/revision,
   хеш публикации и `Article.active_revision_id` записываются вместе. Ошибка
   на любом этапе, включая constraint или истечение lease, откатывает всё.

Требуется повторный `init-schema`: P13 добавляет uniqueness `RelationType.kind`.
Действующая P03 Community-схема и официальный driver сохраняются; новых
зависимостей нет. Число запросов публикации ограничено и не растёт с количеством
персонажей/связей; содержимое ограничено `Limits`. Глобальная блокировка графа
ограничивает пропускную способность; нагрузочные гарантии не проверены.

## Контракт для P14/P15

`Job.attempt_token` — строковый UUID владельца попытки, `attempt` — целое число
от 1. `lease_expires_at` — Unix seconds (float), сравниваемый с
`datetime.realtime().epochMillis / 1000.0` Neo4j, включая проверку перед завершением.
Claim/reclaim/heartbeat в P15 должны брать Article -> Job и читать состояние
после блокировки; reclaim меняет token и увеличивает attempt. P13 не реализует
захват lease, heartbeat, доставку или pipeline.

Повтор completed Job с тем же attempt/token и SHA-256 полного resolution JSON
возвращает completed без новых graph writes; живой lease ему уже не нужен.
Другой payload отклоняется с `publication_conflict`, другой владелец —
`stale_attempt`. Повтор исторического completed Job не переключает active назад.
Retry, заменивший `r.job_id`, блокирует прежний Job. Повтор superseded того же
владельца возвращает superseded. Код ошибки не включает артефакт или token.

## Чтение и история

Старые MENTIONS и assertions сохраняются для provenance. Все будущие бизнес-чтения
обязаны выбирать revision через `r.revision_id = a.active_revision_id`:

```cypher
MATCH (a:Article)-[:HAS_REVISION]->(r:ArticleRevision)-[:ASSERTS]->(v:RelationAssertion)
WHERE r.revision_id = a.active_revision_id
RETURN v
```

Список Characters использует тот же фильтр через MENTIONS и DISTINCT character_id.
Реестр RelationType служит проверке directed; публичный список видов берётся из
активных assertions. Новая пустая revision скрывает прежний граф только своей
статьи. Characters и подтверждения других статей не удаляются. HTTP graph GET
относятся к последующему шагу; P13 проверяет контракт чтения прямыми DB-запросами.

## Проверки

`tests/processing/test_publication.py --neo4j` использует настоящую изолированную
`docker-compose.test.yml` Neo4j 5.26.0 Community. Только claim будущего worker
подготавливается Cypher-фикстурой; publisher и проверки состояния — настоящие.
Проверяются запись evidence/векторов/endpoints/статусов, rollback при реальной
ошибке uniqueness после частичной записи и инъекции ошибки после всех graph
writes, исчезнувшая связь, общий Character двух статей, пустой результат,
повтор/конкурентный commit, вытесненная revision до/после нового успеха,
stale token/attempt, замена Job при retry, expiry до/внутри транзакции,
гонка регистрации с публикацией, изменённый/удалённый кандидат,
несовместимые metadata/directed и некорректные ссылки артефакта.
Отдельный subprocess проверяет PEP 810 lazy driver и отсутствие загрузки
httpx/PDFium при импорте publisher. Платные API не вызываются.
