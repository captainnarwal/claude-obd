#!/bin/sh
# Thin wrapper: `obd.py doctor`.
D="$(cd "$(dirname "$0")/.." && pwd)"
exec "$D/obd" doctor "$@"
