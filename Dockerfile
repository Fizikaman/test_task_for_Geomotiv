FROM ghcr.io/astral-sh/uv:0.13.6 AS uv
FROM python:3.15.2-slim

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /data/blobs \
    && chown -R app:app /data /app
USER 10001:10001
CMD ["uvicorn", "article_graph.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
