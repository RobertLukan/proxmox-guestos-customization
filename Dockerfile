FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# samba-common-bin provides /usr/bin/net, used for Offline Domain Join
# provisioning (djoin.exe equivalent). Client tooling only; no daemons run.
# Reinstall util-linux/mount so Debian security updates (Trivy image CVEs)
# land even when the python:3.12-slim snapshot is a few days behind.
RUN apt-get update \
    && apt-get install -y --no-install-recommends samba-common-bin \
    && apt-get install -y --no-install-recommends \
         util-linux mount libblkid1 libmount1 libuuid1 libfdisk1 libsmartcols1 bsdutils \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better layer caching.
COPY requirements.txt .
# Drop CPython ensurepip copies (setuptools 70.3.x / older msgpack) so Trivy
# does not keep reporting CVEs against wheels that are not what we run.
RUN rm -rf /usr/local/lib/python*/ensurepip \
    && pip install --no-cache-dir --upgrade 'pip>=26.1.2' \
    && pip uninstall -y setuptools pkg_resources msgpack 2>/dev/null || true \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --force-reinstall \
         'setuptools==83.0.0' 'msgpack==1.2.1' \
    && find /usr/local -type d \( -name 'setuptools-7*' -o -name 'msgpack-1.1*' \) \
         -prune -exec rm -rf {} + 2>/dev/null || true \
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
