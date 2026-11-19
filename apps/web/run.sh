#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

PORT=${PORT:-5000}
HOST=${HOST:-"127.0.0.1"}

echo "Starting DIU Assistant Flask Web Server on http://${HOST}:${PORT}..."
exec python3 app.py
