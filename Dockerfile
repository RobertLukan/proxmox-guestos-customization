FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade 'pip>=26.1.2' \
    && pip uninstall -y setuptools pkg_resources 2>/dev/null || true \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --force-reinstall --no-cache-dir \
         'setuptools==83.0.0' 'msgpack==1.2.1' \
    && find /usr/local/lib -type d -name 'setuptools-7*' -prune -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib -type d -name 'msgpack-1.1*' -prune -exec rm -rf {} + 2>/dev/null || true \
    && rm -f /usr/local/lib/python*/ensurepip/_bundled/setuptools-*.whl \
    && python -c "import setuptools, msgpack, importlib.metadata as m; \
assert m.version('setuptools') >= '83.0.0', m.version('setuptools'); \
assert m.version('msgpack') >= '1.2.1', m.version('msgpack'); \
print('setuptools', m.version('setuptools'), 'msgpack', m.version('msgpack'))" \
    && pip check

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
