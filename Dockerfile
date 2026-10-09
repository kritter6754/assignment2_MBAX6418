# Server image for the always-on app (built by Railway from the folder made by
# scripts/build_deploy_bundle.py, which adds the prepared library/ next to this file).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY course_assistant ./course_assistant
RUN uv sync --frozen --no-dev

# Prepared library: slide images, extracted text, and the search indexes.
COPY library ./library

# Secrets (CLASS_API_KEY, endpoint URLs, APP_PASSWORD) come from Railway variables, never the image.
ENV DATA_DIR=/app/library HOST=0.0.0.0 PYTHONUNBUFFERED=1 GRADIO_ANALYTICS_ENABLED=False
CMD ["uv", "run", "--no-sync", "course-assistant"]
