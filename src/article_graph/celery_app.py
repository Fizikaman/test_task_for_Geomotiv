"""Celery CLI entry point: required settings are validated before worker startup."""

from article_graph.worker import WorkerSettings, create_app

app = create_app(WorkerSettings())
