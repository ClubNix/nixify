#!/usr/bin/env bash
set -euo pipefail

export PYTHONUTF8=1
export APP_BUILD="${APP_BUILD:-manual}"
export NIXIFY_ENTRYPOINT="/root/nixify/Nixify.py"
export NIXIFY_STRICT_ENTRYPOINT=1

cd /root/nixify
exec python3 /root/nixify/Nixify.py
