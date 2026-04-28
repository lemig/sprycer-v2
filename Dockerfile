# Sprycer v2 production image — single-stage, slim base.
# Optimized for cheap rebuilds (deps cached, app layer changes most).
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# psycopg[binary] ships its own libpq, but ca-certificates is needed for
# Neon's TLS, and curl is the easiest way to install uv.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer — cached unless pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# App layer — invalidates on any source change.
COPY . .
RUN uv sync --frozen --no-dev

# collectstatic needs Django to import settings. SECRET_KEY only needs to be
# non-empty to satisfy the prod-mode assertion; it's never used at request
# time (the runtime SECRET_KEY comes from Fly secrets).
ENV SECRET_KEY=collectstatic-build-only DEBUG=False
RUN python manage.py collectstatic --noinput

# Drop privileges. The /app tree is owned by app:app so the non-root user
# can read static + write logs.
RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

# Fly forwards :8080 by default, but we expose :8000 because that's what the
# fly.toml internal_port references.
EXPOSE 8000

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "sprycer.wsgi:application"]
