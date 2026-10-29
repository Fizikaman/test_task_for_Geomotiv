# Article Graph — P01

Минимальный API на обычном CPython **3.15.2**. Для будущих Docker/CI использовать
ту же ветку 3.15, без free-threaded/JIT. Остальной стек пока не реализован.

Нужен uv **0.13.6**. Если установлен старый uv, команды можно запускать через
`uvx uv==0.13.6 ...`; в созданном окружении доступен `.venv/Scripts/uv.exe` (Windows).

```powershell
uv sync --frozen
Copy-Item .env.example .env
# Задать в .env свой ARTICLE_GRAPH_APP_TOKEN (не менее 32 символов).
uv run --frozen article-graph
```

Приложение слушает `127.0.0.1:8000`. Фабрика — `article_graph.app:create_app`;
альтернативный запуск: `uv run --frozen uvicorn article_graph.app:create_app --factory`.
Публичный `GET /health/live` возвращает `{"status":"ok"}` и проверяет только
работоспособность процесса. Отсутствующий/короткий App Token останавливает создание
приложения; ошибки настроек скрывают входные значения. В P01 App Token только
валидируется и хранится в настройках; авторизация добавляется в следующем шаге.

```powershell
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyrefly check
uv run --frozen pytest
```

В ограниченном Windows sandbox временные файлы pytest можно направить в проект:
`uv run --frozen pytest --basetemp=.cache/pytest`. HTTP-проверки требуют разрешения
на запуск локального сервера. Обычный запуск вне sandbox не требует этой настройки.

Результаты проверок и ограничения будущих зависимостей: [docs/compatibility.md](docs/compatibility.md).
