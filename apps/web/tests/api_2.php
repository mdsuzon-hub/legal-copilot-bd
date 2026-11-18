<?php
/**
 * PDF RAG — PHP Web Frontend
 * Place this file on any PHP server (PHP 7.4+, no extra extensions needed)
 * Point $API_BASE to wherever api.py is running.
 */

$API_BASE = getenv('RAG_API_URL') ?: 'http://localhost:8000';

// ── tiny HTTP helper ──────────────────────────────────────────────────────────
function api(string $method, string $path, $body = null, bool $json = true): array {
    global $API_BASE;
    $ch = curl_init($API_BASE . $path);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 120,
        CURLOPT_CUSTOMREQUEST  => strtoupper($method),
        CURLOPT_HTTPHEADER     => ['Accept: application/json'],
    ]);
    if ($body !== null) {
        if ($json) {
            curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body));
            curl_setopt($ch, CURLOPT_HTTPHEADER, [
                'Content-Type: application/json',
                'Accept: application/json',
            ]);
        }
        // multipart handled separately (see upload_pdf)
    }
    $raw  = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    $data = json_decode($raw ?: '{}', true) ?: ['raw' => $raw];
    $data['_http_code'] = $code;
    return $data;
}

function upload_pdf(string $file_path, string $file_name): array {
    global $API_BASE;
    $ch = curl_init($API_BASE . '/upload?reset=true');
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 300,
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => ['file' => new CURLFile($file_path, 'application/pdf', $file_name)],
    ]);
    $raw  = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    $data = json_decode($raw ?: '{}', true) ?: [];
    $data['_http_code'] = $code;
    return $data;
}

// ── Session ───────────────────────────────────────────────────────────────────
session_start();
if (empty($_SESSION['sid'])) {
    $_SESSION['sid'] = 'php_' . bin2hex(random_bytes(8));
}
$SESSION_ID = $_SESSION['sid'];

// ── Handle AJAX / form actions ────────────────────────────────────────────────
$action = $_POST['action'] ?? $_GET['action'] ?? '';

// AJAX: ask question
if ($action === 'chat' && isset($_POST['question'])) {
    header('Content-Type: application/json');
    $result = api('POST', '/chat', [
        'question'   => trim($_POST['question']),
        'session_id' => $SESSION_ID,
        'top_k'      => (int)($_POST['top_k'] ?? 5),
        'debug'      => !empty($_POST['debug']),
    ]);
    echo json_encode($result);
    exit;
}

// AJAX: upload PDF
if ($action === 'upload' && isset($_FILES['pdf'])) {
    header('Content-Type: application/json');
    $tmp  = $_FILES['pdf']['tmp_name'];
    $name = basename($_FILES['pdf']['name']);
    if (!str_ends_with(strtolower($name), '.pdf')) {
        echo json_encode(['error' => 'Only PDF files allowed']);
        exit;
    }
    $result = upload_pdf($tmp, $name);
    echo json_encode($result);
    exit;
}

// AJAX: status
if ($action === 'status') {
    header('Content-Type: application/json');
    echo json_encode(api('GET', '/status'));
    exit;
}

// AJAX: clear history
if ($action === 'clear') {
    header('Content-Type: application/json');
    echo json_encode(api('DELETE', "/history?session_id={$SESSION_ID}"));
    exit;
}

// AJAX: suggestions
if ($action === 'suggest') {
    header('Content-Type: application/json');
    echo json_encode(api('GET', "/suggest?session_id={$SESSION_ID}"));
    exit;
}

// AJAX: config (set API key)
if ($action === 'config' && isset($_POST['groq_key'])) {
    header('Content-Type: application/json');
    echo json_encode(api('POST', '/config', ['groq_api_key' => trim($_POST['groq_key'])]));
    exit;
}

// ── Fetch initial status ──────────────────────────────────────────────────────
$status = @api('GET', '/status');
$api_ok = ($status['_http_code'] ?? 0) === 200;
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PDF RAG — AI Document Chat</title>
<style>
  /* ── Reset & Variables ─────────────────────────────────────────── */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:        #0d1117;
    --surface:   #161b22;
    --surface2:  #21262d;
    --border:    #30363d;
    --accent:    #58a6ff;
    --accent2:   #1f6feb;
    --success:   #3fb950;
    --warning:   #d29922;
    --danger:    #f85149;
    --text:      #e6edf3;
    --text-muted:#8b949e;
    --radius:    10px;
    --shadow:    0 8px 24px rgba(0,0,0,.4);
  }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }

  /* ── Header ────────────────────────────────────────────────────── */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
    display: flex;
    align-items: center;
    gap: 12px;
    position: sticky; top: 0; z-index: 100;
  }
  header .logo { font-size: 1.4rem; font-weight: 700; color: var(--accent); }
  header .sub  { font-size: .8rem; color: var(--text-muted); }
  #api-badge {
    margin-left: auto;
    padding: 4px 10px;
    border-radius: 20px;
    font-size: .75rem;
    font-weight: 600;
  }
  .badge-ok  { background: #1e3a2a; color: var(--success); }
  .badge-err { background: #3a1e1e; color: var(--danger);  }

  /* ── Layout ────────────────────────────────────────────────────── */
  .app {
    display: flex;
    flex: 1;
    overflow: hidden;
    height: calc(100vh - 57px);
  }

  /* ── Sidebar ───────────────────────────────────────────────────── */
  .sidebar {
    width: 300px; min-width: 260px;
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column;
    padding: 16px;
    gap: 16px;
    overflow-y: auto;
  }
  .sidebar h3 {
    font-size: .8rem;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--text-muted);
    margin-bottom: 4px;
  }

  /* Upload zone */
  .upload-zone {
    border: 2px dashed var(--border);
    border-radius: var(--radius);
    padding: 20px;
    text-align: center;
    cursor: pointer;
    transition: border-color .2s, background .2s;
  }
  .upload-zone:hover, .upload-zone.drag { border-color: var(--accent); background: #1f2937; }
  .upload-zone input { display: none; }
  .upload-zone .icon { font-size: 2rem; margin-bottom: 6px; }
  .upload-zone p { font-size: .85rem; color: var(--text-muted); }
  .upload-zone .progress-bar {
    height: 4px; background: var(--border); border-radius: 2px;
    margin-top: 10px; overflow: hidden; display: none;
  }
  .upload-zone .progress-bar span {
    display: block; height: 100%; background: var(--accent);
    animation: indeterminate 1.5s infinite;
  }
  @keyframes indeterminate {
    0%   { transform: translateX(-100%) scaleX(.5); }
    100% { transform: translateX(200%) scaleX(.5); }
  }

  /* Status card */
  .stat-card {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px 14px;
  }
  .stat-row {
    display: flex; justify-content: space-between;
    align-items: center;
    padding: 3px 0;
    font-size: .82rem;
  }
  .stat-row .val { color: var(--accent); font-weight: 600; }

  /* Settings */
  .setting { display: flex; flex-direction: column; gap: 4px; }
  .setting label { font-size: .8rem; color: var(--text-muted); }
  .setting input[type=range] { width: 100%; accent-color: var(--accent); }
  .setting .range-val { font-size: .8rem; color: var(--accent); text-align: right; }

  /* Input field */
  .field input, .field textarea {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: 8px 10px;
    font-size: .85rem;
    outline: none;
    transition: border-color .2s;
  }
  .field input:focus, .field textarea:focus { border-color: var(--accent); }

  /* Buttons */
  .btn {
    width: 100%; padding: 9px 14px;
    border: none; border-radius: 6px;
    cursor: pointer; font-size: .85rem; font-weight: 600;
    transition: opacity .15s, transform .1s;
  }
  .btn:hover { opacity: .85; }
  .btn:active { transform: scale(.97); }
  .btn-primary { background: var(--accent2); color: #fff; }
  .btn-danger  { background: #6e1f1f; color: var(--danger); }
  .btn-ghost   { background: var(--surface2); color: var(--text-muted); }

  /* ── Chat area ─────────────────────────────────────────────────── */
  .chat-area {
    flex: 1; display: flex; flex-direction: column;
    overflow: hidden;
  }
  .messages {
    flex: 1; overflow-y: auto;
    padding: 20px;
    display: flex; flex-direction: column; gap: 16px;
    scroll-behavior: smooth;
  }
  .messages::-webkit-scrollbar { width: 6px; }
  .messages::-webkit-scrollbar-track { background: transparent; }
  .messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  /* Message bubbles */
  .msg {
    display: flex; gap: 12px;
    animation: fadeUp .25s ease;
    max-width: 820px;
    align-self: flex-start;
  }
  @keyframes fadeUp {
    from { opacity:0; transform: translateY(8px); }
    to   { opacity:1; transform: translateY(0); }
  }
  .msg.user { align-self: flex-end; flex-direction: row-reverse; }
  .avatar {
    width: 36px; height: 36px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 1rem; flex-shrink: 0;
  }
  .msg.user     .avatar { background: var(--accent2); }
  .msg.assistant .avatar { background: #1e3a2a; }
  .bubble {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px 16px;
    font-size: .9rem;
    line-height: 1.6;
    max-width: 680px;
    word-break: break-word;
  }
  .msg.user .bubble {
    background: var(--accent2);
    border-color: transparent;
    color: #fff;
  }
  .bubble pre {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px;
    overflow-x: auto;
    margin: 8px 0;
    font-size: .8rem;
  }
  .bubble code { font-family: 'JetBrains Mono', 'Fira Code', monospace; }

  /* Meta line under answer */
  .meta {
    font-size: .75rem;
    color: var(--text-muted);
    margin-top: 8px;
    display: flex; gap: 12px; flex-wrap: wrap;
  }
  .page-badge {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 6px;
    font-size: .72rem;
    color: var(--accent);
  }

  /* Typing indicator */
  .typing { display: flex; align-items: center; gap: 4px; padding: 6px 0; }
  .typing span {
    width: 7px; height: 7px;
    background: var(--accent);
    border-radius: 50%;
    animation: bounce .9s infinite;
  }
  .typing span:nth-child(2) { animation-delay: .15s; }
  .typing span:nth-child(3) { animation-delay: .30s; }
  @keyframes bounce {
    0%,80%,100% { transform: scale(.8); opacity: .5; }
    40%          { transform: scale(1.2); opacity: 1; }
  }

  /* Suggestions */
  .suggestions {
    display: flex; flex-wrap: wrap; gap: 8px;
    padding: 0 20px 12px;
  }
  .suggestion-btn {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 20px;
    color: var(--text-muted);
    padding: 5px 12px;
    font-size: .8rem;
    cursor: pointer;
    transition: border-color .2s, color .2s;
  }
  .suggestion-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* Input bar */
  .input-bar {
    padding: 14px 20px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    display: flex; gap: 10px; align-items: flex-end;
  }
  .input-bar textarea {
    flex: 1;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    padding: 10px 14px;
    font-size: .9rem;
    resize: none;
    min-height: 42px; max-height: 140px;
    outline: none;
    line-height: 1.5;
    transition: border-color .2s;
    font-family: inherit;
  }
  .input-bar textarea:focus { border-color: var(--accent); }
  .send-btn {
    width: 42px; height: 42px;
    background: var(--accent2);
    border: none; border-radius: 8px;
    color: #fff; font-size: 1.1rem;
    cursor: pointer; flex-shrink: 0;
    transition: opacity .15s;
  }
  .send-btn:hover { opacity: .85; }
  .send-btn:disabled { opacity: .4; cursor: default; }

  /* Welcome screen */
  .welcome {
    flex: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 12px; color: var(--text-muted);
    padding: 40px;
    text-align: center;
  }
  .welcome .big-icon { font-size: 3.5rem; }
  .welcome h2 { font-size: 1.3rem; color: var(--text); }
  .welcome p  { font-size: .9rem; max-width: 380px; line-height: 1.5; }

  /* Toast */
  #toast {
    position: fixed; bottom: 24px; right: 24px;
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--text); padding: 10px 18px;
    border-radius: 8px; font-size: .85rem;
    box-shadow: var(--shadow);
    opacity: 0; transform: translateY(10px);
    transition: opacity .3s, transform .3s;
    pointer-events: none; z-index: 999;
  }
  #toast.show { opacity: 1; transform: translateY(0); }

  /* Chunk debug */
  .chunks-panel {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 10px 14px;
    margin-top: 8px;
    font-size: .78rem;
  }
  .chunks-panel summary { cursor: pointer; color: var(--text-muted); margin-bottom: 6px; }
  .chunk-item {
    border-left: 2px solid var(--border);
    padding-left: 8px; margin-bottom: 6px;
    font-size: .75rem; color: var(--text-muted);
  }
  .chunk-item .chunk-score { color: var(--success); font-weight: 600; }

  /* Responsive */
  @media (max-width: 640px) {
    .sidebar { display: none; }
  }
</style>
</head>
<body>

<header>
  <div>
    <div class="logo">📄 RAG Chat</div>
    <div class="sub">Advanced PDF Question Answering</div>
  </div>
  <span id="api-badge" class="<?= $api_ok ? 'badge-ok' : 'badge-err' ?>">
    <?= $api_ok ? '● API Online' : '✕ API Offline' ?>
  </span>
</header>

<div class="app">

  <!-- ── Sidebar ────────────────────────────────────────────────── -->
  <aside class="sidebar">

    <!-- Upload -->
    <div>
      <h3>📥 Upload PDF</h3>
      <div class="upload-zone" id="upload-zone" onclick="document.getElementById('pdf-input').click()">
        <div class="icon">📑</div>
        <p id="upload-label">Click or drag a PDF here</p>
        <div class="progress-bar" id="upload-progress"><span></span></div>
      </div>
      <input type="file" id="pdf-input" accept=".pdf">
    </div>

    <!-- Status -->
    <div>
      <h3>📊 Index Status</h3>
      <div class="stat-card" id="stat-card">
        <?php if ($api_ok): ?>
        <div class="stat-row"><span>PDF</span><span class="val" title="<?= htmlspecialchars($status['pdf_name'] ?? '') ?>"><?= htmlspecialchars(substr($status['pdf_name'] ?? '—', 0, 18)) ?></span></div>
        <div class="stat-row"><span>Chunks</span><span class="val"><?= $status['chunk_count'] ?? 0 ?></span></div>
        <div class="stat-row"><span>BM25 Docs</span><span class="val"><?= $status['bm25_docs'] ?? 0 ?></span></div>
        <div class="stat-row"><span>Groq Ready</span><span class="val"><?= ($status['groq_configured'] ?? false) ? '✓' : '✗' ?></span></div>
        <div class="stat-row"><span>Compression</span><span class="val"><?= ($status['context_compress'] ?? false) ? 'ON' : 'OFF' ?></span></div>
        <?php else: ?>
        <div class="stat-row"><span style="color:var(--danger)">Cannot reach API at <?= htmlspecialchars($API_BASE) ?></span></div>
        <?php endif; ?>
      </div>
    </div>

    <!-- Groq Key -->
    <?php if ($api_ok && !($status['groq_configured'] ?? false)): ?>
    <div>
      <h3>🔑 Groq API Key</h3>
      <div class="field">
        <input type="password" id="groq-key" placeholder="gsk_…">
      </div>
      <button class="btn btn-primary" style="margin-top:8px" onclick="saveKey()">Save Key</button>
    </div>
    <?php endif; ?>

    <!-- Settings -->
    <div>
      <h3>⚙ Settings</h3>
      <div class="setting">
        <label>Top-K Results: <span class="range-val" id="topk-val">5</span></label>
        <input type="range" id="topk" min="1" max="12" value="5"
               oninput="document.getElementById('topk-val').textContent=this.value">
      </div>
      <div class="setting" style="margin-top:10px">
        <label>
          <input type="checkbox" id="debug-mode"> Show retrieved chunks
        </label>
      </div>
    </div>

    <!-- Actions -->
    <div style="margin-top: auto; display: flex; flex-direction: column; gap: 8px;">
      <button class="btn btn-ghost" onclick="loadSuggestions()">💡 Get Suggestions</button>
      <button class="btn btn-danger" onclick="clearHistory()">🗑 Clear History</button>
    </div>

  </aside>

  <!-- ── Chat area ─────────────────────────────────────────────── -->
  <main class="chat-area">

    <div class="messages" id="messages">
      <div class="welcome" id="welcome">
        <div class="big-icon">🤖</div>
        <h2>Ask anything about your PDF</h2>
        <p>Upload a PDF using the sidebar, then type a question below. The AI will answer strictly from the document with page citations.</p>
        <?php if (!$api_ok): ?>
        <p style="color:var(--danger);margin-top:12px">⚠ Cannot reach API at <code><?= htmlspecialchars($API_BASE) ?></code>. Make sure api.py is running.</p>
        <?php endif; ?>
      </div>
    </div>

    <div class="suggestions" id="suggestions"></div>

    <div class="input-bar">
      <textarea id="question" rows="1" placeholder="Ask a question about your document…"
                onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
      <button class="send-btn" id="send-btn" onclick="sendQuestion()" title="Send (Enter)">➤</button>
    </div>

  </main>
</div>

<div id="toast"></div>

<script>
// ── helpers ──────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const SESSION_ID = '<?= $SESSION_ID ?>';

function toast(msg, ms = 2800) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), ms);
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

function scrollBottom() {
  const m = $('messages');
  m.scrollTop = m.scrollHeight;
}

function formatAnswer(text) {
  // Basic markdown: bold, code blocks, line breaks
  return text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g, '<em>$1</em>')
    .replace(/\n/g, '<br>');
}

function addMessage(role, content, meta = null) {
  $('welcome')?.remove();
  const msgs = $('messages');

  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '🧑' : '🤖';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = formatAnswer(content);

  if (meta) {
    const metaDiv = document.createElement('div');
    metaDiv.className = 'meta';
    if (meta.elapsed) metaDiv.innerHTML += `<span>⏱ ${meta.elapsed}s</span>`;
    if (meta.pages?.length) {
      meta.pages.forEach(p => {
        const b = document.createElement('span');
        b.className = 'page-badge';
        b.textContent = `p.${p}`;
        metaDiv.appendChild(b);
      });
    }
    if (meta.queries_used?.length > 1) {
      metaDiv.innerHTML += `<span title="${meta.queries_used.join(' | ')}">🔀 ${meta.queries_used.length} query rewrites</span>`;
    }
    bubble.appendChild(metaDiv);

    // Chunk debug
    if (meta.chunks?.length) {
      const details = document.createElement('details');
      details.className = 'chunks-panel';
      details.innerHTML = `<summary>🔍 ${meta.chunks.length} retrieved chunks</summary>`;
      meta.chunks.forEach((c, i) => {
        const d = document.createElement('div');
        d.className = 'chunk-item';
        d.innerHTML = `<span class="chunk-score">Rerank: ${c.rerank_score}</span> · Page ${c.page} · ${c.type}<br><em>${c.text_preview}…</em>`;
        details.appendChild(d);
      });
      bubble.appendChild(details);
    }
  }

  wrap.appendChild(avatar);
  wrap.appendChild(bubble);
  msgs.appendChild(wrap);
  scrollBottom();
  return bubble;
}

function addTyping() {
  $('welcome')?.remove();
  const msgs = $('messages');
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';
  wrap.id = 'typing-indicator';
  wrap.innerHTML = `<div class="avatar">🤖</div><div class="bubble"><div class="typing"><span></span><span></span><span></span></div></div>`;
  msgs.appendChild(wrap);
  scrollBottom();
}

function removeTyping() {
  $('typing-indicator')?.remove();
}

// ── Send question ─────────────────────────────────────────────────────────────
async function sendQuestion() {
  const q = $('question').value.trim();
  if (!q) return;

  $('question').value = '';
  $('question').style.height = 'auto';
  $('send-btn').disabled = true;
  $('suggestions').innerHTML = '';

  addMessage('user', q);
  addTyping();

  const topK   = $('topk').value;
  const debug  = $('debug-mode').checked;

  try {
    const fd = new FormData();
    fd.append('action', 'chat');
    fd.append('question', q);
    fd.append('top_k', topK);
    if (debug) fd.append('debug', '1');

    const res  = await fetch('', { method: 'POST', body: fd });
    const data = await res.json();

    removeTyping();

    if (data.detail || data.error) {
      addMessage('assistant', `⚠️ Error: ${data.detail || data.error}`);
    } else {
      addMessage('assistant', data.answer, {
        elapsed:     data.elapsed,
        pages:       data.pages,
        queries_used: data.queries_used,
        chunks:      debug ? data.chunks : [],
      });
    }
  } catch (err) {
    removeTyping();
    addMessage('assistant', `⚠️ Network error: ${err.message}`);
  } finally {
    $('send-btn').disabled = false;
    $('question').focus();
  }
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendQuestion();
  }
}

// ── Upload PDF ────────────────────────────────────────────────────────────────
async function uploadPDF(file) {
  const label = $('upload-label');
  const prog  = $('upload-progress');
  label.textContent = `Uploading ${file.name}…`;
  prog.style.display = 'block';

  const fd = new FormData();
  fd.append('action', 'upload');
  fd.append('pdf', file);

  try {
    const res  = await fetch('', { method: 'POST', body: fd });
    const data = await res.json();
    prog.style.display = 'none';

    if (data.status === 'indexed') {
      label.textContent = `✓ ${data.pdf} (${data.chunks} chunks)`;
      toast(`✓ Indexed ${data.chunks} chunks from ${data.pages} pages`);
      refreshStatus();
    } else {
      label.textContent = '✗ Upload failed';
      toast(`Error: ${data.detail || JSON.stringify(data)}`, 5000);
    }
  } catch (err) {
    prog.style.display = 'none';
    label.textContent = '✗ Network error';
    toast(err.message, 4000);
  }
}

$('pdf-input').addEventListener('change', e => {
  if (e.target.files[0]) uploadPDF(e.target.files[0]);
});

// Drag & drop
const zone = $('upload-zone');
zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag'); });
zone.addEventListener('dragleave', () => zone.classList.remove('drag'));
zone.addEventListener('drop', e => {
  e.preventDefault(); zone.classList.remove('drag');
  const f = e.dataTransfer.files[0];
  if (f?.name.toLowerCase().endsWith('.pdf')) uploadPDF(f);
  else toast('Please drop a PDF file');
});

// ── Status refresh ────────────────────────────────────────────────────────────
async function refreshStatus() {
  const fd = new FormData(); fd.append('action', 'status');
  const res  = await fetch('', { method: 'POST', body: fd });
  const data = await res.json();
  // Update stat-card rows
  const card = $('stat-card');
  card.querySelector('[data-key="pdf"]')   && (card.querySelector('[data-key="pdf"]').textContent   = (data.pdf_name || '—').substring(0, 18));
  card.querySelector('[data-key="chunks"]')&& (card.querySelector('[data-key="chunks"]').textContent = data.chunk_count);
}

// ── Clear history ─────────────────────────────────────────────────────────────
async function clearHistory() {
  const fd = new FormData(); fd.append('action', 'clear');
  await fetch('', { method: 'POST', body: fd });
  $('messages').innerHTML = '';
  $('suggestions').innerHTML = '';
  toast('Conversation cleared');
}

// ── Suggestions ───────────────────────────────────────────────────────────────
async function loadSuggestions() {
  const fd = new FormData(); fd.append('action', 'suggest');
  try {
    const res  = await fetch('', { method: 'POST', body: fd });
    const data = await res.json();
    const box  = $('suggestions');
    box.innerHTML = '';
    (data.suggestions || []).forEach(s => {
      const btn = document.createElement('button');
      btn.className = 'suggestion-btn';
      btn.textContent = s;
      btn.onclick = () => { $('question').value = s; sendQuestion(); };
      box.appendChild(btn);
    });
    if (!data.suggestions?.length) toast('No suggestions yet — ask a question first');
  } catch { toast('Could not load suggestions'); }
}

// ── Save Groq key ─────────────────────────────────────────────────────────────
async function saveKey() {
  const key = $('groq-key')?.value?.trim();
  if (!key) return;
  const fd = new FormData();
  fd.append('action', 'config');
  fd.append('groq_key', key);
  const res  = await fetch('', { method: 'POST', body: fd });
  const data = await res.json();
  if (data.updated?.groq) { toast('✓ Groq key saved'); location.reload(); }
  else toast('Failed to save key');
}

// Auto-focus
$('question').focus();
</script>
</body>
</html>