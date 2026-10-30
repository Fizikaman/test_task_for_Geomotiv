# Рабочие проверки статьи и PDF/Vision — 30 октября 2026

По явному запросу пользователя рабочий Compose собран и запущен с его `.env`.
Файл/ключи не выводились и не копировались в отчёт. Реальные вызовы провайдера
разрешены пользователем для этой проверки. Это отдельный рабочий smoke,
а не полный opt-in набор P22.

## Запуск

Порт 8000 занят BroLango. Его контейнеры/данные не изменялись. Для Geomotiv
создан локальный игнорируемый `.cache/live.override.yml`, заменяющий только
публикацию API на `127.0.0.1:18000:8000`. Повтор запуска из корня проекта:

```powershell
docker compose -f docker-compose.yml -f .cache/live.override.yml up -d --build --wait --wait-timeout 1800
docker compose -f docker-compose.yml -f .cache/live.override.yml ps -a
```

Оба init — exit 0; API, worker, dispatcher, Neo4j, Redis, Ollama — healthy.
Стек оставлен работать, его тома и загруженная статья сохранены.
Swagger: <http://127.0.0.1:18000/docs>.

## Реальный документ

Из пользовательской LinkedIn redirect-ссылки взят исходный
[Yahoo URL](https://uk.news.yahoo.com/patrick-stewart-accepting-star-trek-201108574.html).
Скачан HTML, из основного блока извлечены 59 абзацев без навигации/подписей
к изображениям, сформирован Markdown с заголовком: 9947 UTF-8 байт.
Текст и HTML находятся только в игнорируемом `.cache`, текст не выводился в логи.
API принимает Markdown/PDF, отдельного URL-import endpoint нет.

Provider: `api.deepseek.com`, text model `deepseek-chat`, output `json_object`.
Параметры взяты из runtime settings; ключ не выводился. Vision model не задана.
Использованы настоящие HTTP API, Redis, dispatcher, prefork worker, Neo4j,
Ollama `bge-m3:567m` и LLM; doubles/eager/manual graph publication не применялись.

- Article: `6ecfa4cd-bc7a-4b4c-94f0-b3951cb66599`.
- Revision: `e474e4b7-4da0-4e07-af25-038054fa51f2`.
- Job: `2118f239-5f41-4225-b020-3d0db3dda10c`.
- Upload: HTTP 202; итог `completed`, active revision совпала с загруженной.
- Опубликованы 13 персонажей и 10 связей; Patrick Stewart найден.
- Типы: `APPEARED_ALONGSIDE`, `APPEARED_OPPOSITE`, `DISLIKED`, `FRIEND_OF`,
  `HAD_ROW_WITH`, `SPOUSE_OF`.
- Все 10 text evidence цитат сверены с исходником с нормализацией пробелов.
  Article/revision и оба конца связей сверены с опубликованными персонажами.
- Из Neo4j повторно прочитаны 13 embeddings: размерность 1024,
  конечные ненулевые векторы проходят production validation.

## HTTP и проверки

41 проверка прошла: все 11 OpenAPI operations плюс Swagger/ReDoc/OpenAPI.
Проверены live/ready 200; выдача и ротация JWT 200; отсутствие Access и подмена
его Refresh дают 401; upload 202, повторные text-file uploads дают no-op 200
с тем же job; статус и все четыре GET графа дают 200, включая type filter.
Неизвестные article/retry дают 404; retry completed статьи — 409;
upload без multipart — 415; оба источника одновременно — 422.
Первый вспомогательный прогон остановился на неверном ожидании 422 вместо 415
для запроса без multipart. Исправлено только ожидание helper; повторная загрузка
стала no-op, новая платная задача из-за этого не создавалась.

Frozen sync, Ruff lint/format, Pyrefly strict прошли (0 ошибок типов, прежний
warning AsyncIterator). После обновления документации запущен её pytest-набор.
Production-код, зависимости и Git index/history не менялись.

## Границы первого прогона

PDF/Vision и три синтетических live-сценария P22 не запускались: vision model
отсутствует. P22 остаётся открытым. Проверены HTTP-контракт и один успешный
реальный текстовый pipeline; это не полная экспертная оценка извлечения,
нагрузочная проверка или подтверждение всех возможностей провайдера.
Успешный retry failed job и обновление статьи новой ревизией в этом рабочем
smoke не выполнялись; проверки этих веток с doubles описаны в P20/P21/P24.

## Повтор после настройки Vision

Пользователь сменил text model на `deepseek-flash`. Runtime показал, что
`vision_model` осталась пустой; первый запуск остановился до загрузки документов
и платных вызовов. Без чтения содержимого `.env` добавлена отдельная строка
`ARTICLE_GRAPH_LLM__VISION_MODEL=deepseek-flash`. После пересоздания приложения
runtime подтвердил обе модели `deepseek-flash` и output mode `json_object`.

Через реальные HTTP API → Redis/dispatcher/prefork worker → DeepSeek/Ollama →
Neo4j загружены все три синтетических документа из `tests/p22_fixture.py`.
Для каждого выполнен существующий `check_graph`, проверены active revision,
article/revision provenance связей, сохранённые embeddings и digest модели.

| Документ | Article ID | Результат | Персонажи / связи | Visual evidence |
| --- | --- | --- | --- | --- |
| Markdown | `92106da9-75f6-4ebb-891c-650dd4c1696c` | completed, oracle passed | 3 / 2 | Нет |
| PDF-схема | `b2f1fe9a-2f82-48ec-8ac2-4bdb304d4d81` | completed, oracle passed | 2 / 1 | Страница 1 |
| Смешанный PDF | `7cecad97-6070-4e82-a9aa-c192e359ffda` | completed, oracle passed | 3 / 2 | Страница 2 |

У схемы отношение выражено стрелкой; текст содержит имена и легенду.
Подтверждены Alice/Ally как одна персона, направленная Alice → Bob `PARENT_OF`,
отсутствие обратного родительства и лишних утверждений. Markdown/смешанный PDF
сохранили симметричную Alice–Carol `FRIEND_OF` с text evidence нужной страницы.
PDF renderer действительно исполнялся в worker, изображения передавались
настоящему Vision API; doubles или ручной публикации не было. Для трёх документов
повторно прочитаны соответственно 3/2/3 finite nonzero embeddings размерности 1024.

Frozen sync, Ruff lint/format, Pyrefly strict: прошли, 0 ошибок типов;
сохраняется прежний warning AsyncIterator. Production-код и зависимости не
менялись. Проверка выполнена вспомогательным HTTP harness, а не командой
`pytest --live-provider` с отдельной эфемерной БД; эта команда в данном сеансе
не запускалась. Синтетические статьи оставлены в рабочем графе вместе с Yahoo
статьёй, все шесть постоянных сервисов healthy, оба init exit 0.
Полнота/точность на произвольных PDF и нагрузка этим прогоном не доказаны.
Offline-регрессия `tests/test_p22_fixture.py tests/test_documentation.py`:
16 passed за 4.25 с; `git diff --check` прошёл.
