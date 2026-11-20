import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent.parent

# Load .env file safely without external dependencies
def _load_env_file(filepath: Path):
    if not filepath.is_file():
        return
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass

_load_env_file(ROOT_DIR / '.env')
_load_env_file(BASE_DIR / '.env')

# Groq API Key
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '').strip()

# Groq Model
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'openai/gpt-oss-120b').strip() or 'openai/gpt-oss-120b'

# SerpAPI Key (optional, falls back to DuckDuckGo scraping)
SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '').strip()

# RAG Server (Python rag_server.py default: port 5001)
RAG_SERVER_URL = os.environ.get('RAG_SERVER_URL', 'http://127.0.0.1:5001').rstrip('/')

# DIU Notice Board URL
DIU_NOTICE_URL = os.environ.get('DIU_NOTICE_URL', 'https://noticeboard.diu.edu.bd/')

# Cache directory & TTL
CACHE_DIR = Path(os.environ.get('CACHE_DIR', str(BASE_DIR / 'live' / 'rag_cache')))
NOTICE_CACHE_TTL = int(os.environ.get('NOTICE_CACHE_TTL', '600'))  # 10 minutes

# Supabase Credentials (optional)
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').strip().rstrip('/')
SUPABASE_KEY = (os.environ.get('SUPABASE_KEY', '') or os.environ.get('SUPABASE_ANON_KEY', '') or os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')).strip()

# Model display names
MODEL_NAMES = {
    'openai/gpt-oss-120b': 'GPT-OSS 120B',
    'openai/gpt-oss-20b': 'GPT-OSS 20B',
    'llama-3.3-70b-versatile': 'Llama 3.3 70B',
    'llama3-70b-8192': 'Llama 3 70B',
    'mixtral-8x7b-32768': 'Mixtral 8x7B',
    'gemma2-9b-it': 'Gemma 2 9B',
}

def get_model_name(model_id: str = None) -> str:
    model = model_id or GROQ_MODEL
    return MODEL_NAMES.get(model, model)
