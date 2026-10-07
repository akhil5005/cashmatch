# Matches the host Python (3.13) so behaviour is identical in and out of Docker.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency install is its own layer so source edits don't invalidate the cache.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e . && \
    pip install --no-cache-dir "pytest>=8.3" "httpx>=0.28" "ruff>=0.8"

COPY . .

# Never run the API as root.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "cashmatch.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
