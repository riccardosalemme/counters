FROM python:3.13-slim

# uv comes from its own published image: no pip bootstrap layer, pinned version.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=core.settings

WORKDIR /app

# Dependencies first and on their own, so editing the source does not rebuild
# this layer.
COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project

COPY src/ ./src/
WORKDIR /app/src

# Baked into the image: the static files never change at runtime, and this way
# a container starts without needing write access to its own code.
RUN SECRET_KEY=build-only DEBUG=false python manage.py collectstatic --noinput

# Home of the SQLite database when DATABASE_URL points here. Owned by the app
# user so a named volume mounted on it inherits that ownership.
RUN useradd --system --create-home app \
    && mkdir -p /data \
    && chown app:app /data

USER app
EXPOSE 8000

# migrate on start, so a fresh volume or a new release lands ready to use.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn core.wsgi:application --bind 0.0.0.0:8000 --workers 3"]
