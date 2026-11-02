#!/bin/sh
# Thin wrapper kept for backward compatibility; the logic lives in read_guard.py.
D="$(cd "$(dirname "$0")" && pwd)"
for p in python3 python py; do
  if command -v "$p" >/dev/null 2>&1; then exec "$p" "$D/read_guard.py"; fi
done
exit 0
