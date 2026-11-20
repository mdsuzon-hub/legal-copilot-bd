#!/usr/bin/env python3
"""
=============================================================================
  DIU Assistant — Production Flask Backend
  Converted from api.php (PHP -> Flask)
  Handles: General Chat · RAG · Web Search · DIU Notice Scraping
=============================================================================
"""

import base64
import html
import json
import logging
import os
import re
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
import secrets

import requests
from flask import Flask, jsonify, request, send_from_directory, session, redirect, url_for

import config

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger('diu_assistant')

# ── Flask App Setup ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
LIVE_DIR = BASE_DIR / 'live'
SCORCES_DIR = BASE_DIR / 'scorces'

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get('SECRET_KEY', 'diu-assistant-session-secret-2026')
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024  # 30MB max upload
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ── CORS Helper ──────────────────────────────────────────────────────────────
def cors_response(data_or_response, status_code=200):
    if isinstance(data_or_response, tuple):
        response = data_or_response[0]
        status = data_or_response[1]
    elif hasattr(data_or_response, 'status_code'):
        response = data_or_response
        status = response.status_code
    else:
        response = jsonify(data_or_response)
        status = status_code

    if not hasattr(response, 'headers'):
        response = jsonify(response)
        response.status_code = status

    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Content-Type'] = 'application/json; charset=utf-8'
    return response, status


def json_error(message: str, code: int = 400):
    return cors_response({'error': message}, code)


# ── Static File Routes ───────────────────────────────────────────────────────
@app.route('/')
@app.route('/index.html')
def index():
    return send_from_directory(str(LIVE_DIR), 'index.html')


@app.route('/scorces/<path:filename>')
@app.route('/live/scorces/<path:filename>')
def serve_scorces(filename):
    if (SCORCES_DIR / filename).is_file():
        return send_from_directory(str(SCORCES_DIR), filename)
    return "File not found", 404


# ── Authentication Routes (Google OAuth 2.0) ─────────────────────────────────
@app.route('/auth/config', methods=['GET', 'OPTIONS'])
def auth_config():
    """Return public OAuth configuration for client-side authentication."""
    if request.method == 'OPTIONS':
        return cors_response('', 204)
    return cors_response({
        'google_client_id': config.GOOGLE_CLIENT_ID,
        'configured': bool(config.GOOGLE_CLIENT_ID),
    })


@app.route('/auth/google/login')
def google_login():
    """Initiate server-side Google OAuth 2.0 Authorization Code flow."""
    if not config.GOOGLE_CLIENT_ID:
        return redirect('/?auth_error=' + quote('Google Client ID is not configured'))

    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state

    redirect_uri = request.host_url.rstrip('/') + '/auth/google/callback'

    auth_url = (
        'https://accounts.google.com/o/oauth2/v2/auth?'
        f'client_id={quote(config.GOOGLE_CLIENT_ID)}&'
        f'redirect_uri={quote(redirect_uri)}&'
        'response_type=code&'
        'scope=openid%20email%20profile&'
        f'state={quote(state)}&'
        'access_type=online&'
        'prompt=select_account'
    )
    return redirect(auth_url)


@app.route('/auth/google/callback')
def google_callback():
    """Handle callback from Google OAuth 2.0."""
    error = request.args.get('error')
    if error:
        err_msg = request.args.get('error_description') or error
        logger.warning(f"[OAuth] Google login error: {err_msg}")
        return redirect('/?auth_error=' + quote(err_msg))

    code = request.args.get('code')
    if not code:
        return redirect('/?auth_error=' + quote('Missing authorization code from Google'))

    state = request.args.get('state')
    saved_state = session.pop('oauth_state', None)
    if saved_state and state != saved_state:
        logger.warning("[OAuth] State mismatch / CSRF warning")
        # Log warning but proceed if state is otherwise valid

    redirect_uri = request.host_url.rstrip('/') + '/auth/google/callback'

    # Exchange authorization code for tokens
    try:
        token_res = requests.post(
            'https://oauth2.googleapis.com/token',
            data={
                'code': code,
                'client_id': config.GOOGLE_CLIENT_ID,
                'client_secret': config.GOOGLE_CLIENT_SECRET,
                'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code',
            },
            timeout=15,
        )
        if token_res.status_code != 200:
            logger.error(f"[OAuth] Token exchange failed ({token_res.status_code}): {token_res.text}")
            return redirect('/?auth_error=' + quote('Failed to exchange token with Google'))

        token_data = token_res.json()
        access_token = token_data.get('access_token')

        # Retrieve user profile from Google UserInfo endpoint
        user_res = requests.get(
            'https://www.googleapis.com/oauth2/v3/userinfo',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=15,
        )
        if user_res.status_code != 200:
            logger.error(f"[OAuth] UserInfo fetch failed: {user_res.text}")
            return redirect('/?auth_error=' + quote('Failed to retrieve user profile from Google'))

        profile = user_res.json()
        google_id = profile.get('sub')
        email = profile.get('email')
        name = profile.get('name') or (email.split('@')[0] if email else 'User')
        picture = profile.get('picture')

        if not email:
            return redirect('/?auth_error=' + quote('No email provided by Google account'))

        user = {
            'google_id': google_id,
            'email': email,
            'name': name,
            'picture': picture,
        }
        session['user'] = user
        logger.info(f"[OAuth] User signed in via Google: {email}")

        return redirect('/?auth_success=1')

    except Exception as e:
        logger.error(f"[OAuth] Exception in google_callback: {e}", exc_info=True)
        return redirect('/?auth_error=' + quote(f'Authentication error: {e}'))


@app.route('/auth/google/verify', methods=['POST', 'OPTIONS'])
def google_verify_token():
    """
    Verify Google ID Token from Google Identity Services (One-Tap / Sign-In Button).
    Enables secure client-side login without exposing client secrets.
    """
    if request.method == 'OPTIONS':
        return cors_response('', 204)

    data = request.get_json(silent=True) or request.form
    credential = data.get('credential') or data.get('id_token')

    if not credential:
        return json_error('Missing Google ID token credential', 400)

    try:
        # Verify token with Google public tokeninfo endpoint
        res = requests.get(
            f'https://oauth2.googleapis.com/tokeninfo?id_token={quote(credential)}',
            timeout=10,
        )
        if res.status_code != 200:
            return json_error('Invalid Google token verification', 401)

        payload = res.json()

        # Validate audience if client ID is configured
        if config.GOOGLE_CLIENT_ID and payload.get('aud') != config.GOOGLE_CLIENT_ID:
            logger.warning(f"[OAuth] Token audience mismatch: {payload.get('aud')} != {config.GOOGLE_CLIENT_ID}")
            return json_error('Invalid token audience', 401)

        google_id = payload.get('sub')
        email = payload.get('email')
        name = payload.get('name') or (email.split('@')[0] if email else 'User')
        picture = payload.get('picture')

        if not email:
            return json_error('No email found in Google token', 400)

        user = {
            'google_id': google_id,
            'email': email,
            'name': name,
            'picture': picture,
        }
        session['user'] = user
        logger.info(f"[OAuth] User verified via ID token: {email}")

        return cors_response({
            'success': True,
            'user': user,
        })

    except Exception as e:
        logger.error(f"[OAuth] Token verification exception: {e}", exc_info=True)
        return json_error(f'Token verification error: {e}', 500)


@app.route('/auth/me', methods=['GET', 'OPTIONS'])
@app.route('/auth/user', methods=['GET', 'OPTIONS'])
def auth_me():
    """Return currently authenticated user information from session."""
    if request.method == 'OPTIONS':
        return cors_response('', 204)

    user = session.get('user')
    if user:
        return cors_response({
            'authenticated': True,
            'user': user,
        })
    return cors_response({
        'authenticated': False,
        'user': None,
    })


@app.route('/auth/logout', methods=['GET', 'POST', 'OPTIONS'])
def auth_logout():
    """Log out user by clearing session."""
    if request.method == 'OPTIONS':
        return cors_response('', 204)

    user = session.pop('user', None)
    if user:
        logger.info(f"[OAuth] User logged out: {user.get('email')}")

    if request.method == 'POST' or request.is_json:
        return cors_response({
            'success': True,
            'message': 'Logged out successfully',
        })
    return redirect('/?logged_out=1')


# ── Favicon Domain Mapping ───────────────────────────────────────────────────
FAVICON_MAP = {
    'diu.edu.bd': '🎓',
    'noticeboard.diu': '📋',
    'wikipedia.org': '📖',
    'google.com': '🔍',
    'youtube.com': '▶️',
    'github.com': '💻',
    'stackoverflow.com': '💡',
    'bdnews24.com': '📰',
    'prothomalo.com': '📰',
    'thedailystar.net': '📰',
}

def get_domain_favicon(url: str) -> str:
    if not url:
        return '🔗'
    try:
        host = urlparse(url).netloc.lower()
        for domain, icon in FAVICON_MAP.items():
            if domain in host:
                return icon
    except Exception:
        pass
    return '🔗'


# ── Clean Text Helper ────────────────────────────────────────────────────────
def clean_text(text: str) -> str:
    if not text:
        return ''
    t = html.unescape(text)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


# ── Notice Related Query Detection ───────────────────────────────────────────
NOTICE_KEYWORDS = [
    'notice',
    'notification',
    'announcement',
    'noticeboard',
    'circular',
    'exam schedule',
    'exam routine',
    'result',
    'cgpa',
    'registration',
    'holiday',
    'class cancel',
    'class schedule',
    'event',
    'seminar',
    'workshop',
    'scholarship',
    'payment deadline',
    'latest news',
    'recent update',
    'new update',
]

def is_notice_related(question: str) -> bool:
    for kw in NOTICE_KEYWORDS:
        if re.search(r'\b' + re.escape(kw), question, re.IGNORECASE):
            return True
    return False


# ── Groq API Caller ──────────────────────────────────────────────────────────
def call_groq(messages: list, temp: float = 0.5, max_tokens: int = 1024, effort: str = 'low') -> dict:
    if not config.GROQ_API_KEY:
        return {'answer': '⚠️ Groq API key not configured. Please add your key to config.py or set GROQ_API_KEY.'}

    model = config.GROQ_MODEL
    body = {
        'model': model,
        'messages': messages,
        'temperature': temp,
        'max_tokens': max_tokens,
        'stream': False,
    }
    if 'gpt-oss' in model.lower():
        body['reasoning_effort'] = effort

    try:
        res = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {config.GROQ_API_KEY}',
            },
            json=body,
            timeout=60,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Groq API connection error: {e}")

    if res.status_code == 429:
        return {'answer': '⚠️ Rate limit exceeded. Please wait a moment and try again.'}
    if res.status_code == 401:
        return {'answer': '⚠️ Invalid API key. Please check your Groq API key.'}
    if res.status_code != 200:
        raise RuntimeError(f"Groq API error HTTP {res.status_code}: {res.text[:200]}")

    try:
        data = res.json()
    except Exception:
        raise RuntimeError("Groq API returned invalid JSON")

    if 'error' in data:
        raise RuntimeError(data['error'].get('message', 'Groq API error'))

    choices = data.get('choices', [])
    content = ''
    if choices and 'message' in choices[0]:
        content = choices[0]['message'].get('content', '').strip()

    if not content:
        return {'answer': '⚠️ The model returned an empty response. Please try again.'}

    return {'answer': content}


def build_messages(system_prompt: str, history: list, question: str, file_data: dict = None) -> list:
    messages = [{'role': 'system', 'content': system_prompt}]
    for h in history:
        messages.append({'role': h['role'], 'content': h['content']})

    user_content = question
    if file_data:
        user_content += f"\n\n[Attached file: {file_data.get('name', 'file')}]\n"
        ftype = file_data.get('type', '')
        fname = file_data.get('name', '')
        if 'text' in ftype or fname.endswith(('.txt', '.md')):
            raw_b64 = file_data.get('data', '')
            raw_b64 = re.sub(r'^data:[^;]+;base64,', '', raw_b64)
            try:
                decoded = base64.b64decode(raw_b64).decode('utf-8', errors='replace')
                user_content += decoded[:4000]
            except Exception:
                pass

    messages.append({'role': 'user', 'content': user_content})
    return messages


def generate_related(question: str, answer: str) -> list:
    system_prompt = (
        "Generate 3 short follow-up questions a user might ask after receiving this answer. "
        "Return ONLY a JSON array of 3 strings. No explanation. Keep questions concise (under 80 chars each). "
        "Use the same language as the original question."
    )
    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': f"Original question: {question}\n\nAnswer summary: {answer[:300]}"},
    ]
    try:
        res = call_groq(messages, temp=0.8, max_tokens=600)
        text = res.get('answer', '')
        text = re.sub(r'```(?:json)?|```', '', text).strip()
        parsed = json.loads(text)
        if isinstance(parsed, list) and len(parsed) >= 2:
            return [str(x) for x in parsed[:4]]
    except Exception as e:
        logger.warning(f"generate_related error: {e}")

    return [
        'Can you tell me more about this topic?',
        'What are the official DIU resources for this?',
        'Are there any recent updates related to this?',
    ]


# ── Handler: General Chat ────────────────────────────────────────────────────
def handle_general(question: str, history: list, file_data: dict = None) -> dict:
    system_prompt = """You are the DIU (Daffodil International University) Smart Assistant — a helpful, 
accurate, and friendly AI for students, faculty, and staff of Daffodil International 
University, Dhaka, Bangladesh.

Your role:
- Answer general questions conversationally and helpfully
- Provide accurate information about DIU when you know it
- For university-specific queries (notices, results, schedules), suggest users 
  enable "RAG" or "Web" search mode for live data
- Be concise but thorough
- Respond in the same language the user writes in (Bengali or English)
- For greetings, be warm and welcoming

DIU Quick Facts:
- Location: Daffodil Smart City, Birulia, Savar, Dhaka-1216, Bangladesh
- Website: diu.edu.bd | noticeboard.diu.edu.bd
- Founded: 2002
- Notable for: Software Engineering, CSE, Business, Law departments"""

    messages = build_messages(system_prompt, history, question, file_data)
    res = call_groq(messages, temp=0.7, max_tokens=2048)
    answer = res.get('answer', 'Sorry, I could not generate a response.')

    return {
        'answer': answer,
        'mode': 'general',
        'sources': [],
        'notices': [],
        'related': generate_related(question, answer),
        'model_name': config.get_model_name(),
    }


# ── Handler: RAG ─────────────────────────────────────────────────────────────
def is_pdf_file(f: dict) -> bool:
    name = f.get('name', '').lower()
    ftype = f.get('type', '').lower()
    return 'pdf' in ftype or name.endswith('.pdf')


def rag_upload_pdf(f: dict) -> dict:
    raw_b64 = re.sub(r'^data:[^;]+;base64,', '', f.get('data', ''))
    try:
        binary_data = base64.b64decode(raw_b64)
    except Exception:
        return {'ok': False, 'chunks': 0, 'error': 'invalid file data'}

    if not binary_data:
        return {'ok': False, 'chunks': 0, 'error': 'invalid file data'}

    files = {'file': (f.get('name', 'document.pdf'), binary_data, 'application/pdf')}
    try:
        res = requests.post(
            f"{config.RAG_SERVER_URL}/upload",
            files=files,
            timeout=300
        )
        if res.status_code == 200:
            data = res.json()
            return {'ok': True, 'chunks': int(data.get('chunks', 0)), 'error': ''}
        return {'ok': False, 'chunks': 0, 'error': f"HTTP {res.status_code}: {res.text[:100]}"}
    except Exception as e:
        return {'ok': False, 'chunks': 0, 'error': str(e)}


def handle_rag(question: str, history: list, file_data: dict = None) -> dict:
    upload_note = ''

    if file_data and is_pdf_file(file_data):
        up = rag_upload_pdf(file_data)
        fname = file_data.get('name', 'file')
        if up['ok']:
            upload_note = f"📎 **{fname}** added to the knowledge base" + (f" ({up['chunks']} chunks total)" if up['chunks'] else '') + ".\n\n"
        else:
            upload_note = f"⚠️ Could not index **{fname}**: {up['error']}\n\n"
        file_data = None  # Already handled

    rag_error = None
    data = None
    try:
        res = requests.post(
            f"{config.RAG_SERVER_URL}/ask",
            json={
                'question': question,
                'history': history,
                'top_k': 6,
            },
            timeout=120,
        )
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and 'error' in data:
                rag_error = data['error']
        else:
            rag_error = f"HTTP {res.status_code}"
    except Exception as e:
        rag_error = str(e)

    # Server unreachable or returned error: graceful fallback
    if rag_error or not isinstance(data, dict):
        reason = rag_error or 'Unknown error'
        logger.warning(f"RAG failure: {reason} ({config.RAG_SERVER_URL})")
        fallback = handle_general(question, history, file_data)
        fallback['answer'] = (
            f"⚠️ **RAG server unavailable** — {reason}\n"
            f"Make sure `rag_server.py` is running at `{config.RAG_SERVER_URL}`. "
            "The answer below is general knowledge, **not** from your documents.\n\n"
            f"{upload_note}{fallback['answer']}"
        )
        fallback['mode'] = 'rag'
        return fallback

    raw_sources = data.get('sources') if isinstance(data.get('sources'), list) else []
    cited = [s for s in raw_sources if isinstance(s, dict) and s.get('cited')]
    shown = cited if cited else raw_sources

    sources = []
    for s in shown:
        if isinstance(s, dict):
            title = s.get('source') or s.get('title') or 'Document'
            if s.get('page'):
                title += f" · p.{s['page']}"
            sources.append({
                'title': title,
                'url': s.get('url', ''),
                'favicon': s.get('favicon', '📄'),
                'snippet': s.get('snippet', ''),
                'tag': s.get('tag', ''),
            })

    answer = data.get('answer', 'No answer from RAG.')
    return {
        'answer': upload_note + answer,
        'mode': 'rag',
        'sources': sources,
        'notices': [],
        'related': data.get('related') if data.get('related') else generate_related(question, answer),
        'model_name': data.get('model_name', config.get_model_name()),
    }


# ── Handler: Web Search ──────────────────────────────────────────────────────
class DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.in_result = False
        self.in_title = False
        self.in_snippet = False
        self.cur_title = []
        self.cur_href = ''
        self.cur_snippet = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        classes = attr_dict.get('class', '').split()
        if 'result' in classes and tag == 'div':
            self.in_result = True
            self.cur_title = []
            self.cur_href = ''
            self.cur_snippet = []
        elif self.in_result and 'result__a' in classes and tag == 'a':
            self.in_title = True
            self.cur_href = attr_dict.get('href', '')
        elif self.in_result and any('result__snippet' in c for c in classes):
            self.in_snippet = True

    def handle_endtag(self, tag):
        if self.in_title and tag == 'a':
            self.in_title = False
        elif self.in_snippet and tag in ('div', 'a', 'span', 'p'):
            self.in_snippet = False
        elif self.in_result and tag == 'div':
            # End of result div
            t = clean_text(''.join(self.cur_title))
            s = clean_text(''.join(self.cur_snippet))
            href = self.cur_href

            # DDG wraps URLs in uddg=
            url = href
            if 'uddg=' in href:
                parsed = urlparse(href)
                params = parse_qs(parsed.query)
                url = unquote(params.get('uddg', [href])[0])

            if t and url:
                self.results.append({'title': t, 'url': url, 'snippet': s})
                self.cur_title = []
                self.cur_snippet = []
                self.cur_href = ''
            self.in_result = False

    def handle_data(self, data):
        if self.in_title:
            self.cur_title.append(data)
        elif self.in_snippet:
            self.cur_snippet.append(data)


def search_via_serpapi(query: str) -> list:
    try:
        res = requests.get(
            'https://serpapi.com/search',
            params={'q': query, 'api_key': config.SERPAPI_KEY, 'num': 8, 'hl': 'en'},
            timeout=15,
        )
        if res.status_code == 200:
            data = res.json()
            organic = data.get('organic_results', [])
            return [
                {
                    'title': r.get('title', ''),
                    'url': r.get('link', ''),
                    'snippet': r.get('snippet', ''),
                }
                for r in organic
            ]
    except Exception as e:
        logger.warning(f"SerpAPI search error: {e}")
    return []


def search_via_ddg(query: str) -> list:
    try:
        res = requests.get(
            'https://html.duckduckgo.com/html/',
            params={'q': query},
            headers={
                'User-Agent': 'Mozilla/5.0 (compatible; DIU-Assistant/1.0)',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            timeout=15,
        )
        if res.status_code == 200:
            parser = DDGParser()
            parser.feed(res.text)
            if parser.results:
                return parser.results[:8]
    except Exception as e:
        logger.warning(f"DDG search error: {e}")
    return search_via_google(query)


def search_via_google(query: str) -> list:
    try:
        res = requests.get(
            'https://www.google.com/search',
            params={'q': query, 'num': 8, 'hl': 'en'},
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            timeout=10,
        )
        if res.status_code == 200:
            # Fallback regex extraction of titles & links
            matches = re.findall(r'<a href="(/url\?q=[^"&]+|https?://[^"&]+)"[^>]*><h3[^>]*>(.*?)</h3>', res.text)
            results = []
            for link, h3 in matches:
                title = clean_text(re.sub(r'<[^>]+>', '', h3))
                url = link
                if '/url?q=' in link:
                    url = unquote(link.split('/url?q=')[1].split('&')[0])
                if title and url.startswith('http'):
                    results.append({'title': title, 'url': url, 'snippet': ''})
                if len(results) >= 8:
                    break
            if results:
                return results
    except Exception as e:
        logger.warning(f"Google fallback search error: {e}")

    return [{'title': 'Web search unavailable', 'url': '', 'snippet': 'No web search results available.'}]


def handle_web(question: str, history: list) -> dict:
    if config.SERPAPI_KEY:
        search_results = search_via_serpapi(question)
    else:
        search_results = search_via_ddg(question)

    context_parts = []
    for i, r in enumerate(search_results[:6]):
        context_parts.append(f"[{i+1}] {r.get('title', '')}\nURL: {r.get('url', '')}\n{r.get('snippet', '')}")
    context = "\n\n".join(context_parts)

    system_prompt = f"""You are the DIU Smart Assistant with web search capability.
The user's question has been searched on the web. Use the search results below 
to provide an accurate, helpful answer.

Rules:
- Synthesize information from the search results
- Cite sources by number [1], [2] etc. when referencing specific facts
- If results are about DIU/university topics, focus on those
- Be concise and clear
- If search results don't answer the question, say so honestly
- Always end with: "📄 Sources: [list the relevant source numbers and titles]"

SEARCH RESULTS:
{context}"""

    messages = build_messages(system_prompt, history, question)
    res = call_groq(messages, temp=0.3, max_tokens=2048)
    answer = res.get('answer', 'Could not generate answer from search results.')

    sources = [
        {
            'title': r.get('title') or r.get('url') or 'Web Result',
            'url': r.get('url', ''),
            'favicon': get_domain_favicon(r.get('url', '')),
        }
        for r in search_results[:5]
    ]

    return {
        'answer': answer,
        'mode': 'web',
        'sources': sources,
        'notices': [],
        'related': generate_related(question, answer),
        'model_name': config.get_model_name(),
    }


# ── Handler: DIU Notice Scraper & AI ─────────────────────────────────────────
class NoticeHTMLParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.notices = []
        self.cur_link_text = []
        self.cur_href = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            attr_dict = dict(attrs)
            self.cur_href = attr_dict.get('href', '')
            self.cur_link_text = []

    def handle_endtag(self, tag):
        if tag == 'a' and self.cur_href:
            text = clean_text(''.join(self.cur_link_text))
            href = self.cur_href.strip()
            self.cur_href = None
            self.cur_link_text = []

            if len(text) >= 10 and len(text) <= 350:
                if not re.match(r'^(home|about|contact|login|menu|nav|header|footer)$', text, re.IGNORECASE):
                    full_url = urljoin(self.base_url, href)
                    if full_url.startswith(('http://', 'https://')):
                        self.notices.append({
                            'title': text,
                            'url': full_url,
                            'date': extract_date_from_text(text),
                            'category': guess_category(text),
                            'summary': text,
                        })

    def handle_data(self, data):
        if self.cur_href is not None:
            self.cur_link_text.append(data)


def extract_date_from_text(text: str) -> str:
    patterns = [
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
        r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})',
        r'(\d{4}-\d{2}-\d{2})',
        r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return ''


def guess_category(text: str) -> str:
    t = text.lower()
    categories = {
        'Exam': ['exam', 'examination', 'result', 'semester', 'midterm', 'final'],
        'Admission': ['admission', 'enroll', 'apply', 'application', 'intake'],
        'Academic': ['class', 'schedule', 'course', 'academic', 'lecture', 'semester'],
        'Event': ['event', 'seminar', 'workshop', 'conference', 'program', 'ceremony'],
        'Holiday': ['holiday', 'vacation', 'leave', 'break', 'close'],
        'Scholarship': ['scholarship', 'financial', 'aid', 'waiver', 'stipend'],
        'Fee': ['fee', 'payment', 'tuition', 'charge', 'due'],
        'Job': ['job', 'career', 'recruitment', 'vacancy', 'position'],
    }
    for cat, keywords in categories.items():
        for kw in keywords:
            if kw in t:
                return cat
    return 'General'


def scrape_notices() -> list:
    cache_file = config.CACHE_DIR / 'diu_notices.json'

    # Check cache freshness
    if cache_file.exists():
        try:
            mtime = cache_file.stat().st_mtime
            if (time.time() - mtime) < config.NOTICE_CACHE_TTL:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                    if cached:
                        return cached
        except Exception as e:
            logger.warning(f"Notice cache read error: {e}")

    notices = []
    pages = [
        config.DIU_NOTICE_URL,
        f"{config.DIU_NOTICE_URL.rstrip('/')}/?page=1",
    ]

    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; DIU-Assistant/1.0; +https://diu.edu.bd)',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,bn;q=0.8',
    }

    for url in pages:
        try:
            res = requests.get(url, headers=headers, timeout=15, verify=False)
            if res.status_code == 200:
                parser = NoticeHTMLParser(url)
                parser.feed(res.text)
                for n in parser.notices:
                    if not any(existing['title'] == n['title'] for existing in notices):
                        notices.append(n)
            if len(notices) >= 50:
                break
        except Exception as e:
            logger.warning(f"Error scraping notices from {url}: {e}")

    # Write cache
    if notices:
        try:
            config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(notices, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write notice cache: {e}")

    return notices


def filter_relevant_notices(notices: list, question: str) -> list:
    q_words = [w for w in re.split(r'\W+', question.lower()) if len(w) > 3]
    scored = []
    for n in notices:
        title_lower = (n.get('title') or '').lower()
        category_lower = (n.get('category') or '').lower()
        score = 0
        for w in q_words:
            if w in title_lower:
                score += 3
            if w in category_lower:
                score += 2
        if n.get('date'):
            score += 1

        if score > 0:
            scored.append((score, n))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored[:10]]


def handle_notice(question: str, history: list) -> dict:
    notices = scrape_notices()
    if not notices:
        return handle_web(f"{question} site:diu.edu.bd OR site:noticeboard.diu.edu.bd", history)

    relevant = filter_relevant_notices(notices, question)
    selected = relevant if relevant else notices[:10]

    context_parts = []
    for i, n in enumerate(selected[:10]):
        context_parts.append(
            f"[Notice {i+1}]\nTitle: {n.get('title', '')}\nDate: {n.get('date', 'N/A')}\n"
            f"Category: {n.get('category', 'General')}\nURL: {n.get('url', '')}\nSummary: {n.get('summary', '')}"
        )
    notice_context = "\n\n".join(context_parts)

    system_prompt = f"""You are the DIU Smart Assistant specializing in Daffodil International University notices.
You have access to the latest notices from the DIU Notice Board (noticeboard.diu.edu.bd).

Total notices fetched: {len(notices)}

LATEST DIU NOTICES:
{notice_context}

Rules:
- Answer the user's question using ONLY the notice data above
- List relevant notices with their titles, dates, and URLs
- If asking for "latest" or "recent" notices, list the most recent ones
- Format notice titles in **bold**
- Always include the notice URL so users can click for details
- If no notices match the query, list the most recent 3-5 notices
- Be helpful and organized"""

    messages = build_messages(system_prompt, history, question)
    res = call_groq(messages, temp=0.2, max_tokens=2048)
    answer = res.get('answer', 'Could not retrieve notices at this time.')

    notice_cards = (relevant if relevant else notices)[:5]

    return {
        'answer': answer,
        'mode': 'notice',
        'sources': [{
            'title': 'DIU Notice Board',
            'url': config.DIU_NOTICE_URL,
            'favicon': '📋',
        }],
        'notices': notice_cards,
        'related': [
            'What are the latest examination notices from DIU?',
            'Are there any upcoming DIU events or seminars?',
            'What are the recent academic notices from DIU?',
            'Show me admission-related notices from DIU',
        ],
        'model_name': config.get_model_name(),
    }


# ── Main API Route: /api.php & /api ──────────────────────────────────────────
@app.route('/api.php', methods=['POST', 'OPTIONS'])
@app.route('/api', methods=['POST', 'OPTIONS'])
def api_endpoint():
    if request.method == 'OPTIONS':
        return cors_response('', 204)

    # Input validation
    question = (request.form.get('question') or (request.json.get('question') if request.is_json else '') or '').strip()
    mode = (request.form.get('mode') or (request.json.get('mode') if request.is_json else 'general') or 'general').strip()
    hist_raw = request.form.get('history') or (request.json.get('history') if request.is_json else '[]')

    if not question:
        return json_error('Question is required', 400)
    if len(question) > 4000:
        return json_error('Question too long (max 4000 chars)', 400)

    if mode not in ('general', 'rag', 'web'):
        mode = 'general'

    # Sanitize history
    if isinstance(hist_raw, str):
        try:
            history = json.loads(hist_raw)
        except Exception:
            history = []
    elif isinstance(hist_raw, list):
        history = hist_raw
    else:
        history = []

    clean_history = []
    for h in history:
        if isinstance(h, dict) and h.get('role') in ('user', 'assistant') and isinstance(h.get('content'), str):
            clean_history.append({'role': h['role'], 'content': h['content']})
    clean_history = clean_history[-16:]

    # Uploaded file
    file_data = None
    f_data = request.form.get('file_data') or (request.json.get('file_data') if request.is_json else None)
    f_name = request.form.get('file_name') or (request.json.get('file_name') if request.is_json else None)
    f_type = request.form.get('file_type') or (request.json.get('file_type') if request.is_json else None)
    if f_data and f_name:
        file_data = {
            'name': os.path.basename(f_name),
            'type': f_type or 'application/octet-stream',
            'data': f_data,
        }

    # Dispatch to appropriate handler
    try:
        is_notice = is_notice_related(question)
        if mode == 'rag':
            result = handle_rag(question, clean_history, file_data)
        elif mode == 'web':
            result = handle_web(question, clean_history)
        elif is_notice:
            result = handle_notice(question, clean_history)
        else:
            result = handle_general(question, clean_history, file_data)

        return cors_response(result, 200)

    except Exception as e:
        logger.error(f"[DIU API] Error: {e}", exc_info=True)
        return json_error(f"Server error: {e}", 500)


# ── Server Runner ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '127.0.0.1')
    logger.info(f"Starting Flask server on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
