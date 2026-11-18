#!/usr/bin/env bash
# start.sh — Start backend + PHP frontend
set -e

# ── Backend ─────────────────────────────────────────────────────────────
echo "Starting FastAPI backend on :8000..."
cd "$(dirname "$0")/../backend"
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!
echo "  Backend PID: $BACKEND_PID"

# Wait for backend to be ready
echo "  Waiting for backend..."
for i in {1..20}; do
    curl -sf http://localhost:8000/health > /dev/null && break
    sleep 1
done
echo "  ✅ Backend ready"

# ── Frontend ─────────────────────────────────────────────────────────────
echo ""
echo "Starting PHP frontend on :8080..."
cd "$(dirname "$0")/../frontend/public"
export RAG_API_BASE=http://localhost:8000
php -S 0.0.0.0:8080 &
FRONTEND_PID=$!
echo "  Frontend PID: $FRONTEND_PID"

echo ""
echo "========================================="
echo "  ✅ RAG System Running"
echo "  Frontend: http://localhost:8080"
echo "  API Docs: http://localhost:8000/docs"
echo "  Press Ctrl+C to stop"
echo "========================================="

# Cleanup on exit
trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo 'Stopped.'" EXIT
wait
