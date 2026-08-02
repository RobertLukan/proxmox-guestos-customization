FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade 'pip>=26.1.2' \
    && pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY . .

# Stamp image build time so local rebuilds are distinguishable from VERSION alone.
# CI may pass BUILD_TIMESTAMP explicitly; otherwise use UTC now at build.
ARG BUILD_TIMESTAMP=
RUN if [ -n "$BUILD_TIMESTAMP" ]; then \
      printf '%s\n' "$BUILD_TIMESTAMP" > /app/BUILD_TIMESTAMP; \
    else \
      date -u +%Y-%m-%dT%H:%M:%SZ > /app/BUILD_TIMESTAMP; \
    fi

EXPOSE 5001

# Default command runs the web server; the worker service overrides this in
# docker-compose to run the Celery worker instead.
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "wsgi:app"]
