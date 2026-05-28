[group('dev')]
dev:
    uv sync --all-groups
    uv run pre-commit install || echo "pre-commit not configured yet; skip"

[group('dev')]
lint:
    uv run ruff check .
    uv run ty check src

[group('dev')]
test:
    uv run pytest -q

[group('dev')]
clean:
    rm -rf dist/ .pytest_cache __pycache__
