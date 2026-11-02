#!/bin/sh
# Thin wrapper: `obd.py setup [--install-uv]`.
D="$(cd "$(dirname "$0")/.." && pwd)"
exec "$D/obd" setup "$@"
