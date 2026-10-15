# Server image for the always-on app. Railway builds it from GitHub on every push to main.
# The prepared library (slide images, extracted text, search indexes) is NOT in the image or
# the repo: it lives on a Railway volume mounted at /data, uploaded by scripts/deploy_railway.sh.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY course_assistant ./course_assistant
COPY agentic_icon.png ./
RUN uv sync --frozen --no-dev

# Secrets (CLASS_API_KEY, endpoint URLs, APP_PASSWORD) come from Railway variables, never the image.
ENV DATA_DIR=/data/library HOST=0.0.0.0 PYTHONUNBUFFERED=1 GRADIO_ANALYTICS_ENABLED=False
CMD ["uv", "run", "--no-sync", "course-assistant"]
