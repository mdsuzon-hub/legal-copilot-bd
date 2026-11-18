#!/usr/bin/env bash
# setup.sh — First-time setup for RAG System (no Docker)
set -e

echo "========================================="
echo "  RAG System — Local Setup"
echo "========================================="

# ── Python backend ─────────────────────────────────────────────────────
echo ""
echo "[1/4] Setting up Python backend..."
cd backend

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "  ✅ Created backend/.env — EDIT IT with your API keys!"
else
    echo "  ℹ️  backend/.env already exists"
fi

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  ✅ Python dependencies installed"

# Pre-download embedding model
echo "  Downloading embedding model (first time only)..."
python3 -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2'); print('  ✅ Model ready')"

mkdir -p chroma_db
cd ..

# ── Apache check ────────────────────────────────────────────────────────
echo ""
echo "[2/4] Checking Apache..."
if command -v apache2 &>/dev/null || command -v httpd &>/dev/null; then
    echo "  ✅ Apache detected"
else
    echo "  ⚠️  Apache not found. Install: sudo apt install apache2 libapache2-mod-php"
fi

# ── PHP check ───────────────────────────────────────────────────────────
echo ""
echo "[3/4] Checking PHP..."
if command -v php &>/dev/null; then
    PHP_VER=$(php -r "echo PHP_VERSION;")
    echo "  ✅ PHP $PHP_VER detected"
    php -m | grep -q curl && echo "  ✅ curl extension enabled" || echo "  ⚠️  php-curl needed: sudo apt install php-curl"
else
    echo "  ⚠️  PHP not found. Install: sudo apt install php php-curl"
fi

echo ""
echo "[4/4] Setup complete!"
echo ""
echo "========================================="
echo "  Next steps:"
echo "========================================="
echo ""
echo "  1. Edit backend/.env — add GROQ_API_KEY or HF_API_KEY"
echo "  2. Start backend:"
echo "     cd backend && source venv/bin/activate"
echo "     uvicorn main:app --reload --port 8000"
echo ""
echo "  3. Serve frontend (PHP built-in server for dev):"
echo "     cd frontend/public && RAG_API_BASE=http://localhost:8000 php -S localhost:8080"
echo ""
echo "  4. Open: http://localhost:8080"
echo "  5. Docs:  http://localhost:8000/docs"
echo ""
echo "  OR use Docker: docker-compose up --build"
echo "========================================="
