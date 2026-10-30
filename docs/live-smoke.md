# P22: opt-in live smoke

Обновление 30 октября 2026: по явному разрешению пользователя выполнен
[рабочий smoke статьи Yahoo](live-article-validation.md) с его `.env`, настоящими
DeepSeek JSON mode, Ollama и публикацией графа: 13 персонажей, 10 связей.
Затем все три P22 fixtures прошли через рабочий HTTP/worker с настоящим
`deepseek-flash` для текста/Vision и существующим semantic oracle: Markdown,
PDF-схема и смешанный PDF. Evidence, aliases, направления, embeddings и
публикация проверены. Точные результаты и IDs — в отчёте выше. Штатная команда
изолированного pytest ниже в этом сеансе не запускалась. Далее сохранён исходный
статус и инструкция opt-in набора; прежнее отсутствие live-проверок уже устранено.

Отдельный тестовый ключ и разрешение на live-вызовы в этой сессии не предоставлены.
Реальные JSON mode, Vision, Ollama embed и публикация результатов реального LLM
в Neo4j пока **не проверены**. Employer sample не предоставлен; используются
только явно синтетические fixtures. P22 нельзя отмечать завершённым по offline-тестам.

## Что проверяет сценарий

`tests/live/test_live_provider.py` загружает короткий Markdown, визуальную семейную
схему PDF и двухстраничный смешанный PDF через настоящие маршруты приложения
(TestClient). Затем production `execute_job` получает настоящую Neo4j lease,
поддерживает heartbeat и запускает весь `RevisionPipeline`: preparation с PDF
subprocess, extraction, normalization/resolution, Ollama и graph publication.
HTTP upload/status/graph выполняются в процессе; сокеты API, Celery broker и
dispatcher этот набор повторно не проверяет — для них остаётся P21.
Новых production services/adapters и изменений архитектуры нет.

LLM transport проверяет фактически отправленный `response_format.type` и выбор
явно настроенных text/vision моделей, затем отправляет запрос реальному провайдеру.
Нет fallback режима, doubles или подставленных ответов. Успешный сценарий требует
валидного JSON, обработанного production Pydantic/evidence validation. Это проверка
заявленного режима на реальном ответе, а не доказательство внутренних механизмов
провайдера. Ollama проверяет model digest и фактическую размерность. После закрытия
processing-клиентов новый API lifecycle/driver читает завершённую revision, сущности
и связи; отдельно проверяются сохранённые ненулевые finite embeddings и digest.

Смысловые ожидания: Alice/Ally — одна персона; Alice → Bob — `PARENT_OF`;
обратная связь из отрицания не появляется; Alice–Carol — симметричный `FRIEND_OF`.
У схемы связь выражена геометрией стрелки, текст содержит лишь имена и легенду.
Для неё нужен visual evidence правильной страницы (1 либо 2), для friendship —
text evidence страницы с текстом. Проверяются endpoints, отсутствие лишних
персон/утверждений и привязка к article/revision. Формулировка description,
visual description, порядок и случайные ID полностью не сравниваются.

## Безопасный запуск после явного предоставления отдельного ключа

Нужны обычный CPython 3.15.2, uv 0.13.6, Docker Compose v2, настоящая доступная
Ollama с уже установленной моделью и provider с поддержкой выбранного JSON mode
и image data URLs. Автоматической загрузки моделей и выбора провайдера нет.
На этом Windows-хосте используйте `.venv/Scripts/uv.exe`: системный uv другой версии.

Ни `--neo4j`, ни `--e2e`, ни наличие обычного рабочего ключа не включают live.
Нужны одновременно `--live-provider`, отдельный `P22_LLM__API_KEY` и
`P22_LIVE_CONSENT=separate-key-provided-for-p22`. При непустом `CI` все live-тесты
пропускаются даже с флагом и consent. Settings/ключ не читаются до этого gate;
рабочая `.env` не загружается. Не выставляйте consent, пока пользователь явно
не предоставит отдельный ключ именно для этого прогона.

Задайте в окружении текущего процесса все значения (без секретов в файлах):

| Переменная | Требование |
| --- | --- |
| `P22_LLM__BASE_URL` | Явный provider endpoint без credentials/query |
| `P22_LLM__TEXT_MODEL` | Явная text model |
| `P22_LLM__VISION_MODEL` | Явная vision model |
| `P22_LLM__OUTPUT_MODE` | `json_object` либо `json_schema` |
| `P22_OLLAMA__BASE_URL` | URL настоящей Ollama |
| `P22_OLLAMA__MODEL` | Установленный model:tag |
| `P22_OLLAMA__DIGEST` | Его точный SHA-256, 64 hex символа |
| `P22_OLLAMA__DIMENSIONS` | Фактическая размерность модели |

Отдельный ключ в PowerShell вводится скрыто, без literal в истории команд:

```powershell
$p22Secret = Read-Host 'Separate provider key explicitly supplied for P22' -AsSecureString
$env:P22_LLM__API_KEY = [System.Net.NetworkCredential]::new('', $p22Secret).Password
$env:P22_LIVE_CONSENT = 'separate-key-provided-for-p22'
try {
    docker compose --env-file tests/p22.env -f docker-compose.live.yml up -d --wait --wait-timeout 180
    if ($LASTEXITCODE -ne 0) { throw 'P22 database startup failed' }
    .venv/Scripts/uv.exe run --frozen pytest tests/live/test_live_provider.py --live-provider -q
} finally {
    Remove-Item Env:P22_LLM__API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:P22_LIVE_CONSENT -ErrorAction SilentlyContinue
    $p22Secret = $null
    docker compose --env-file tests/p22.env -f docker-compose.live.yml down --volumes --remove-orphans --timeout 10
}
```

Ключ не передаётся Docker, CLI аргументам, checkpoints или отчётам. Smoke отключает
Python logging на время вызовов и скрывает raw exception/locals в ошибке. Не включайте
внешние HTTP recorders/debuggers, дампы процесса или плагины записи окружения.
Артефакты документов существуют только во временном каталоге и удаляются в finally.
Не сохраняйте ключ в `.env`, fixtures, junit properties или другую историю.

Compose содержит только отдельный эфемерный Neo4j на `127.0.0.1:47687`,
с публичным синтетическим паролем. Данные и логи находятся в tmpfs.
Перед каждой пробой требуется пустая БД; чужие данные приводят к отказу до
мутаций. После пробы все узлы этой эксклюзивной тестовой БД удаляются в finally.
Запрещены параллельные запуски и другие пользователи этого endpoint.
P03/P19/P20/P21 и production endpoints не используются.
При прерывании процесса повторите `down` из finally перед новым запуском.

Один документ ограничен 300 секундами processing, provider call — 60 секундами,
lease — 120 секундами с heartbeat каждые 15 секунд. Автоматических повторных
оплачиваемых попыток нет. Весь набор делает несколько вызовов и расходует квоту
реального провайдера; limits остаются production budgets.

## Offline-проверки

```powershell
.venv/Scripts/uv.exe sync --frozen
.venv/Scripts/uv.exe run --frozen ruff check .
.venv/Scripts/uv.exe run --frozen ruff format --check .
.venv/Scripts/uv.exe run --frozen pyrefly check
.venv/Scripts/uv.exe run --frozen pytest
```

`tests/live/test_p22_fixture.py` проверяет реальные PDF renderer/subprocess, oracle на
валидных и испорченных графах и runtime gates в отдельных Python-процессах:
без флага, в CI с флагом, с флагом без consent. Во всех трёх случаях модуль
live harness не импортируется, реальные providers не вызываются.

Проверено 30 октября 2026 на Windows, CPython 3.15.2 и uv 0.13.6:
frozen sync, Ruff check, Ruff format --check и Pyrefly strict прошли
(0 ошибок; одно прежнее предупреждение AsyncIterator в article_routes).
Набор P22: 13 passed, 3 skipped; полный обычный pytest: 455 passed,
194 skipped за 51.26 секунды, включая runtime lazy-import проверки.
`docker compose ... config --quiet` и `git diff --check` прошли.
Новый live Compose не запускался, его startup/cleanup ещё не проверены.
Отдельные opt-in infrastructure/E2E наборы не запускались в рамках P22.
В sandbox PDF subprocess зависал, а default pytest temp directory и uv cache
были недоступны; финальные PDF/pytest проверки прошли вне sandbox,
frozen sync использовал workspace cache `.cache/uv`.

Следующий шаг: после явного предоставления отдельного ключа выполнить все три
live-сценария, записать только безопасный итог, provider/model identifiers и
результаты смысловых инвариантов. До этого JSON mode + Vision + реальный embed +
graph persistence через real LLM и оценка employer sample остаются непроверенными.
