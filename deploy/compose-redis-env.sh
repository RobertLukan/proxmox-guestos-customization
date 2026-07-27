#!/bin/sh
# Wire REDIS_PASSWORD into Celery URLs when CELERY_* are unset/default.
# Sourced by docker-compose web/worker commands.
set -e
if [ -n "${REDIS_PASSWORD:-}" ]; then
  default_broker='redis://redis:6379/0'
  if [ -z "${CELERY_BROKER_URL:-}" ] || [ "${CELERY_BROKER_URL}" = "${default_broker}" ]; then
    export CELERY_BROKER_URL="redis://:${REDIS_PASSWORD}@redis:6379/0"
  fi
  if [ -z "${CELERY_RESULT_BACKEND:-}" ] || [ "${CELERY_RESULT_BACKEND}" = "${default_broker}" ]; then
    export CELERY_RESULT_BACKEND="redis://:${REDIS_PASSWORD}@redis:6379/0"
  fi
fi
