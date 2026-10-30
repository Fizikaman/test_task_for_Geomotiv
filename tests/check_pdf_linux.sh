#!/bin/sh
# Bind only source, tests, pyproject.toml and uv.lock into /input read-only.
# Run inside python:3.15.2-slim, never against a production volume.
set -eu
mkdir /tmp/project
cp /input/pyproject.toml /input/uv.lock /tmp/project/
cp -r /input/src /input/tests /tmp/project/
cd /tmp/project
python -m pip install --disable-pip-version-check uv==0.13.6
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyrefly check
uv run --frozen pytest tests/documents/test_pdf_preparation.py tests/documents/test_preparation.py tests/documents/test_blob_store.py
