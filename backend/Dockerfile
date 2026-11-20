FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /uvx /bin/

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY . .
# 첫 설치 후 uv.lock을 Git에 포함하면 이후 Docker 빌드를 동일하게 재현할 수 있다.
RUN if [ -f uv.lock ]; then \
        uv sync --locked --no-dev --no-install-project; \
    else \
        uv sync --no-dev --no-install-project; \
    fi

USER 10001
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
