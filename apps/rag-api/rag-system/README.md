# ⚡ RAG Intelligence — Production RAG System

Full-stack **Retrieval-Augmented Generation** system with:

| Layer | Technology |
|---|---|
| Frontend | PHP 8.x |
| Web Server | Apache 2.4 |
| AI Backend | FastAPI (Python) |
| RAG Storage | ChromaDB (persistent) |
| Embeddings | SentenceTransformers (`all-MiniLM-L6-v2`) |
| LLM | **Free** — Groq API (llama3) + HuggingFace fallback |

---

## 🚀 Quick Start (Docker — Recommended)

```bash
# 1. Clone / extract the project
cd rag-system

# 2. Configure environment
cp backend/.env.example backend/.env
nano backend/.env   # Add GROQ_API_KEY or HF_API_KEY

# 3. Start everything
docker-compose up --build

# 4. Open browser
# Frontend:  http://localhost:8080
# API Docs:  http://localhost:8000/docs
```

---

## 🛠️ Manual Setup (No Docker)

### Prerequisites
- Python 3.10+
- PHP 8.1+ with `curl` extension
- Apache 2.4 (or use PHP built-in server for dev)

```bash
# Run setup script
chmod +x scripts/setup.sh
./scripts/setup.sh

# Start both services
chmod +x scripts/start.sh
./scripts/start.sh
```

---

## 🔑 Free LLM API Keys

### Option 1: Groq (RECOMMENDED — fastest)
1. Sign up at https://console.groq.com
2. Create an API key (free tier: 14,400 tokens/min)
3. Add to `backend/.env`:
   ```
   GROQ_API_KEY=gsk_your_key_here
   GROQ_MODEL=llama3-8b-8192
   ```

### Option 2: HuggingFace Inference API (fallback)
1. Sign up at https://huggingface.co
2. Go to Settings → Access Tokens → New token (free)
3. Add to `backend/.env`:
   ```
   HF_API_KEY=hf_your_key_here
   HF_MODEL=mistralai/Mistral-7B-Instruct-v0.2
   ```

> The system tries Groq first, then falls back to HuggingFace, then returns raw context.

---

## 📁 Project Structure

```
rag-system/
├── backend/
│   ├── main.py              # FastAPI app (RAG logic)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env.example         # Copy to .env
│
├── frontend/
│   ├── public/
│   │   ├── index.php        # Main UI
│   │   └── .htaccess        # Apache rules
│   └── src/
│       ├── config.php       # PHP config + API helpers
│       └── api.php          # AJAX handler
│
├── scripts/
│   ├── setup.sh             # First-time setup
│   └── start.sh             # Start both services
│
├── docker-compose.yml
└── README.md
```

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/stats` | System stats |
| `POST` | `/upload` | Upload & index PDF/DOCX/TXT |
| `POST` | `/ingest-text` | Index raw text |
| `POST` | `/query` | RAG query |
| `GET` | `/documents` | List all documents |
| `DELETE` | `/documents/{id}` | Delete a document |

Full interactive docs: `http://localhost:8000/docs`

---

## ⚙️ Configuration

Edit `backend/.env`:

| Variable | Default | Description |
|---|---|---|
| `EMBED_MODEL` | `all-MiniLM-L6-v2` | SentenceTransformer model |
| `CHUNK_SIZE` | `512` | Words per chunk |
| `CHUNK_OVERLAP` | `64` | Overlap between chunks |
| `TOP_K` | `5` | Chunks retrieved per query |
| `GROQ_MODEL` | `llama3-8b-8192` | Groq model name |
| `CHROMA_PATH` | `./chroma_db` | ChromaDB storage path |

---

## 🏭 Production Deployment

### Apache Virtual Host
1. Copy `frontend/rag-apache.conf` to `/etc/apache2/sites-available/`
2. Update `ServerName` and `DocumentRoot`
3. Enable: `sudo a2ensite rag-system && sudo systemctl reload apache2`
4. Set `RAG_API_BASE` env var pointing to your FastAPI server

### HTTPS
```bash
sudo apt install certbot python3-certbot-apache
sudo certbot --apache -d rag.yourdomain.com
```

### FastAPI as a Service (systemd)
```ini
# /etc/systemd/system/rag-backend.service
[Unit]
Description=RAG FastAPI Backend
After=network.target

[Service]
User=www-data
WorkingDirectory=/var/www/rag-system/backend
EnvironmentFile=/var/www/rag-system/backend/.env
ExecStart=/var/www/rag-system/backend/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## 🔒 Security Checklist

- [ ] Set `ALLOWED_ORIGINS` in `.env` to your actual domain
- [ ] Use HTTPS in production
- [ ] Set `SSL_VERIFYPEER=true` in `config.php`
- [ ] Keep `backend/.env` out of web root (it is by default)
- [ ] Configure firewall: only expose port 80/443, keep 8000 internal
- [ ] Enable Apache `mod_security` for extra protection

---

## 📊 Supported File Types

| Format | Extension | Notes |
|---|---|---|
| PDF | `.pdf` | Text extraction via PyPDF2 |
| Word | `.docx` | Via python-docx |
| Plain text | `.txt` | UTF-8 |
| Markdown | `.md` | Treated as plain text |

Max file size: **10 MB** (configurable)
