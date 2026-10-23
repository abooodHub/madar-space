#!/usr/bin/env bash
set -euo pipefail
mkdir -p /tmp/pulse /tmp/runtime-pwuser
chmod 700 /tmp/runtime-pwuser
export XDG_RUNTIME_DIR=/tmp/runtime-pwuser
if [[ -z "${XSA_SCREEN:-}" ]]; then
  record_size="${XSA_RECORD_SIZE:-1280x720}"
  IFS=x read -r record_width record_height <<< "$record_size"
  export XSA_SCREEN="${record_width}x${record_height}x24"
fi
# Stale X lock from an unclean container stop would block Xvfb.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
exec "$@"
