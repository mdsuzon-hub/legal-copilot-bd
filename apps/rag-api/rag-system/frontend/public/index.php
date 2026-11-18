<?php
session_start();
require_once __DIR__ . '/src/config.php';

// Fetch stats for initial render
$stats_result = rag_api('/stats', 'GET');
$stats = ($stats_result['status'] === 200) ? $stats_result['body'] : null;
$health_result = rag_api('/health', 'GET');
$online = ($health_result['status'] === 200);
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title><?= h(APP_NAME) ?> — Intelligent Document Search</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&family=Inter:wght@300;400;500&display=swap" rel="stylesheet">
<style>
/* ── CSS Variables ─────────────────────────────────────────────────── */
:root {
  --bg:         #0a0a0f;
  --surface:    #111118;
  --surface2:   #18181f;
  --border:     #2a2a35;
  --accent:     #6c63ff;
  --accent2:    #ff6584;
  --accent3:    #43e97b;
  --text:       #e8e8f0;
  --text-dim:   #8888a0;
  --text-muted: #44445a;
  --font-head:  'Syne', sans-serif;
  --font-body:  'Inter', sans-serif;
  --font-mono:  'JetBrains Mono', monospace;
  --radius:     12px;
  --radius-sm:  6px;
  --shadow:     0 4px 32px rgba(108,99,255,.15);
}

/* ── Reset ─────────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  min-height: 100vh;
  overflow-x: hidden;
}

/* ── Background grid ────────────────────────────────────────────────── */
body::before {
  content: '';
  position: fixed; inset: 0; z-index: 0;
  background-image:
    linear-gradient(rgba(108,99,255,.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(108,99,255,.03) 1px, transparent 1px);
  background-size: 40px 40px;
  pointer-events: none;
}

/* ── Layout ─────────────────────────────────────────────────────────── */
.app { position: relative; z-index: 1; display: flex; flex-direction: column; min-height: 100vh; }

/* ── Header ─────────────────────────────────────────────────────────── */
header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 32px;
  border-bottom: 1px solid var(--border);
  background: rgba(17,17,24,.8);
  backdrop-filter: blur(12px);
  position: sticky; top: 0; z-index: 100;
}
.logo {
  display: flex; align-items: center; gap: 12px;
  font-family: var(--font-head); font-size: 1.3rem; font-weight: 800;
  letter-spacing: -.02em;
}
.logo-icon {
  width: 36px; height: 36px; border-radius: 10px;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  display: flex; align-items: center; justify-content: center;
  font-size: 18px;
}
.status-badge {
  display: flex; align-items: center; gap: 7px;
  padding: 6px 14px; border-radius: 999px;
  font-size: .78rem; font-weight: 500;
  border: 1px solid var(--border);
  background: var(--surface);
}
.status-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: <?= $online ? 'var(--accent3)' : '#ff4d4d' ?>;
  box-shadow: 0 0 8px <?= $online ? 'var(--accent3)' : '#ff4d4d' ?>;
  animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }

.nav-tabs {
  display: flex; gap: 4px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 4px;
}
.nav-tab {
  padding: 7px 18px; border-radius: var(--radius-sm);
  font-size: .85rem; font-weight: 500; cursor: pointer;
  border: none; background: transparent; color: var(--text-dim);
  transition: all .2s;
}
.nav-tab.active { background: var(--accent); color: #fff; }
.nav-tab:hover:not(.active) { color: var(--text); background: var(--surface2); }

/* ── Main content ───────────────────────────────────────────────────── */
.main { flex: 1; padding: 32px; max-width: 1200px; margin: 0 auto; width: 100%; }

/* ── Stats bar ──────────────────────────────────────────────────────── */
.stats-bar {
  display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap;
}
.stat-card {
  flex: 1; min-width: 150px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px 20px;
  display: flex; flex-direction: column; gap: 4px;
}
.stat-label { font-size: .72rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
.stat-value { font-family: var(--font-head); font-size: 1.4rem; font-weight: 700; color: var(--text); }
.stat-value.accent { color: var(--accent); }

/* ── Panels ─────────────────────────────────────────────────────────── */
.panel { display: none; }
.panel.active { display: block; animation: fadeUp .3s ease; }
@keyframes fadeUp { from{opacity:0;transform:translateY(12px)} to{opacity:1;transform:none} }

/* ── Query panel ────────────────────────────────────────────────────── */
.query-box {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 24px;
  margin-bottom: 24px;
}
.query-box label {
  display: block; font-size: .8rem; font-weight: 500; color: var(--text-dim);
  text-transform: uppercase; letter-spacing: .07em; margin-bottom: 10px;
}
.query-input-wrap { display: flex; gap: 12px; }
textarea.query-input {
  flex: 1; background: var(--bg); border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text);
  font-family: var(--font-body); font-size: 1rem; padding: 14px 16px;
  resize: vertical; min-height: 80px;
  transition: border-color .2s;
  outline: none;
}
textarea.query-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(108,99,255,.15); }
.btn-primary {
  background: linear-gradient(135deg, var(--accent), #8b5cf6);
  color: #fff; border: none; border-radius: var(--radius-sm);
  padding: 14px 24px; font-size: .9rem; font-weight: 600;
  cursor: pointer; white-space: nowrap; align-self: flex-end;
  transition: opacity .2s, transform .15s;
}
.btn-primary:hover { opacity: .9; transform: translateY(-1px); }
.btn-primary:active { transform: translateY(0); }
.btn-primary:disabled { opacity: .5; cursor: not-allowed; transform: none; }

.query-options { display: flex; align-items: center; gap: 20px; margin-top: 12px; }
.query-options label { font-size: .82rem; color: var(--text-dim); display: flex; align-items: center; gap: 8px; }
input[type="range"] { accent-color: var(--accent); }
#topk-val { font-weight: 600; color: var(--accent); font-family: var(--font-mono); }

/* ── Answer card ────────────────────────────────────────────────────── */
.answer-card {
  background: var(--surface); border: 1px solid var(--border);
  border-left: 3px solid var(--accent); border-radius: var(--radius);
  padding: 24px; margin-bottom: 20px;
}
.answer-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 14px;
}
.answer-title { font-family: var(--font-head); font-weight: 700; font-size: 1rem; }
.model-badge {
  background: rgba(108,99,255,.15); color: var(--accent);
  border: 1px solid rgba(108,99,255,.3); border-radius: 999px;
  padding: 3px 10px; font-size: .72rem; font-family: var(--font-mono);
}
.answer-text {
  line-height: 1.75; color: var(--text);
  white-space: pre-wrap; font-size: .95rem;
}

/* ── Sources ────────────────────────────────────────────────────────── */
.sources-grid { display: flex; flex-direction: column; gap: 10px; }
.source-card {
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 14px 16px;
  cursor: pointer; transition: border-color .2s;
}
.source-card:hover { border-color: var(--accent); }
.source-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.source-file { font-size: .82rem; font-weight: 600; color: var(--accent); font-family: var(--font-mono); }
.source-score {
  font-size: .75rem; font-family: var(--font-mono);
  padding: 2px 8px; border-radius: 999px;
  background: rgba(67,233,123,.1); color: var(--accent3); border: 1px solid rgba(67,233,123,.2);
}
.source-excerpt { font-size: .82rem; color: var(--text-dim); line-height: 1.5; }

/* ── Upload panel ───────────────────────────────────────────────────── */
.upload-zone {
  border: 2px dashed var(--border); border-radius: var(--radius);
  padding: 48px 32px; text-align: center; cursor: pointer;
  transition: all .2s; background: var(--surface);
  margin-bottom: 24px;
}
.upload-zone:hover, .upload-zone.dragover {
  border-color: var(--accent); background: rgba(108,99,255,.05);
}
.upload-icon { font-size: 3rem; margin-bottom: 12px; }
.upload-title { font-family: var(--font-head); font-size: 1.1rem; font-weight: 700; margin-bottom: 6px; }
.upload-sub { font-size: .85rem; color: var(--text-dim); }
#file-input { display: none; }
.file-types { display: flex; gap: 8px; justify-content: center; margin-top: 16px; }
.file-type-badge {
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 3px 10px;
  font-size: .75rem; font-family: var(--font-mono); color: var(--text-dim);
}

/* ── Text ingest ────────────────────────────────────────────────────── */
.text-ingest-box {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 24px; margin-bottom: 24px;
}
.form-group { margin-bottom: 16px; }
.form-group label {
  display: block; font-size: .8rem; font-weight: 500;
  color: var(--text-dim); text-transform: uppercase; letter-spacing: .07em; margin-bottom: 8px;
}
.form-input, .form-textarea {
  width: 100%; background: var(--bg); border: 1px solid var(--border);
  border-radius: var(--radius-sm); color: var(--text);
  font-family: var(--font-body); font-size: .95rem; padding: 11px 14px;
  outline: none; transition: border-color .2s;
}
.form-textarea { min-height: 140px; resize: vertical; }
.form-input:focus, .form-textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(108,99,255,.15); }

/* ── Documents panel ────────────────────────────────────────────────── */
.doc-list { display: flex; flex-direction: column; gap: 10px; }
.doc-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px 20px;
  display: flex; justify-content: space-between; align-items: center;
}
.doc-info { display: flex; flex-direction: column; gap: 4px; }
.doc-name { font-weight: 600; font-size: .92rem; }
.doc-meta { font-size: .78rem; color: var(--text-dim); font-family: var(--font-mono); }
.btn-danger {
  background: rgba(255,77,77,.1); color: #ff4d4d;
  border: 1px solid rgba(255,77,77,.3); border-radius: var(--radius-sm);
  padding: 7px 14px; font-size: .8rem; font-weight: 600; cursor: pointer;
  transition: all .2s;
}
.btn-danger:hover { background: rgba(255,77,77,.2); }

/* ── Loading spinner ────────────────────────────────────────────────── */
.spinner-overlay {
  display: none; position: fixed; inset: 0; z-index: 999;
  background: rgba(10,10,15,.7); backdrop-filter: blur(4px);
  align-items: center; justify-content: center; flex-direction: column; gap: 16px;
}
.spinner-overlay.show { display: flex; }
.spinner {
  width: 48px; height: 48px; border-radius: 50%;
  border: 3px solid var(--border);
  border-top-color: var(--accent);
  animation: spin 0.7s linear infinite;
}
.spinner-text { font-size: .9rem; color: var(--text-dim); }
@keyframes spin { to{transform:rotate(360deg)} }

/* ── Toast ──────────────────────────────────────────────────────────── */
.toast-container { position: fixed; bottom: 28px; right: 28px; z-index: 9999; display: flex; flex-direction: column; gap: 10px; }
.toast {
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px 20px;
  display: flex; align-items: center; gap: 10px;
  font-size: .87rem; box-shadow: var(--shadow);
  animation: slideIn .3s ease; max-width: 380px;
}
.toast.success { border-left: 3px solid var(--accent3); }
.toast.error   { border-left: 3px solid #ff4d4d; }
.toast.info    { border-left: 3px solid var(--accent); }
@keyframes slideIn { from{opacity:0;transform:translateX(30px)} to{opacity:1;transform:none} }

/* ── Empty state ────────────────────────────────────────────────────── */
.empty-state {
  text-align: center; padding: 48px;
  color: var(--text-muted); font-size: .9rem;
}
.empty-state .icon { font-size: 3rem; margin-bottom: 12px; display: block; }

/* ── Section header ─────────────────────────────────────────────────── */
.section-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 16px;
}
.section-title {
  font-family: var(--font-head); font-size: 1rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .05em; color: var(--text-dim);
}
.btn-secondary {
  background: var(--surface2); border: 1px solid var(--border);
  color: var(--text-dim); border-radius: var(--radius-sm);
  padding: 7px 14px; font-size: .8rem; font-weight: 500; cursor: pointer;
  transition: all .2s;
}
.btn-secondary:hover { color: var(--text); border-color: var(--accent); }

/* ── Responsive ─────────────────────────────────────────────────────── */
@media (max-width: 768px) {
  header { padding: 14px 16px; flex-wrap: wrap; gap: 12px; }
  .main  { padding: 16px; }
  .query-input-wrap { flex-direction: column; }
  .btn-primary { width: 100%; text-align: center; }
}
</style>
</head>
<body>
<div class="app">

  <!-- Header -->
  <header>
    <div class="logo">
      <div class="logo-icon">⚡</div>
      <?= h(APP_NAME) ?>
    </div>
    <nav class="nav-tabs">
      <button class="nav-tab active" onclick="switchPanel('query')">🔍 Query</button>
      <button class="nav-tab" onclick="switchPanel('upload')">📁 Upload</button>
      <button class="nav-tab" onclick="switchPanel('documents')">📚 Documents</button>
    </nav>
    <div class="status-badge">
      <div class="status-dot"></div>
      <span><?= $online ? 'Backend Online' : 'Backend Offline' ?></span>
    </div>
  </header>

  <!-- Main -->
  <main class="main">

    <!-- Stats bar -->
    <div class="stats-bar">
      <div class="stat-card">
        <span class="stat-label">Indexed Chunks</span>
        <span class="stat-value accent" id="stat-chunks"><?= h($stats['total_chunks'] ?? '—') ?></span>
      </div>
      <div class="stat-card">
        <span class="stat-label">Embedding Model</span>
        <span class="stat-value" style="font-size:1rem;font-family:var(--font-mono)"><?= h($stats['embed_model'] ?? '—') ?></span>
      </div>
      <div class="stat-card">
        <span class="stat-label">LLM Provider</span>
        <span class="stat-value" style="font-size:1rem"><?= h($stats['llm_provider'] ?? '—') ?></span>
      </div>
      <div class="stat-card">
        <span class="stat-label">Collection</span>
        <span class="stat-value" style="font-size:1rem;font-family:var(--font-mono)"><?= h($stats['collection_name'] ?? '—') ?></span>
      </div>
    </div>

    <!-- ── Query Panel ── -->
    <div id="panel-query" class="panel active">
      <div class="query-box">
        <label>Ask a question about your documents</label>
        <div class="query-input-wrap">
          <textarea
            id="query-input"
            class="query-input"
            placeholder="e.g. What are the key findings in the Q3 report?"
            rows="3"
          ></textarea>
          <button class="btn-primary" onclick="submitQuery()" id="query-btn">
            ⚡ Ask
          </button>
        </div>
        <div class="query-options">
          <label>
            Top-K results:
            <input type="range" id="topk" min="1" max="10" value="5" oninput="document.getElementById('topk-val').textContent=this.value">
            <span id="topk-val">5</span>
          </label>
        </div>
      </div>

      <div id="answer-area"></div>
    </div>

    <!-- ── Upload Panel ── -->
    <div id="panel-upload" class="panel">

      <!-- File drop zone -->
      <div class="upload-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
        <div class="upload-icon">📄</div>
        <div class="upload-title">Drop files here or click to browse</div>
        <div class="upload-sub">PDF, DOCX, TXT, MD — max <?= MAX_FILE_SIZE_MB ?>MB</div>
        <div class="file-types">
          <?php foreach (ALLOWED_EXTENSIONS as $ext): ?>
            <span class="file-type-badge">.<?= h($ext) ?></span>
          <?php endforeach; ?>
        </div>
      </div>
      <input type="file" id="file-input" accept=".pdf,.docx,.txt,.md" onchange="uploadFile(this)">

      <!-- Text ingest -->
      <div class="text-ingest-box">
        <div class="section-header">
          <span class="section-title">✏️ Ingest Raw Text</span>
        </div>
        <div class="form-group">
          <label>Document name</label>
          <input type="text" class="form-input" id="ingest-name" placeholder="my-notes.txt" value="manual_input.txt">
        </div>
        <div class="form-group">
          <label>Text content</label>
          <textarea class="form-textarea" id="ingest-text" placeholder="Paste your text content here..."></textarea>
        </div>
        <button class="btn-primary" onclick="ingestText()">📥 Index Text</button>
      </div>
    </div>

    <!-- ── Documents Panel ── -->
    <div id="panel-documents" class="panel">
      <div class="section-header">
        <span class="section-title">📚 Indexed Documents</span>
        <button class="btn-secondary" onclick="loadDocuments()">↻ Refresh</button>
      </div>
      <div id="doc-list" class="doc-list">
        <div class="empty-state"><span class="icon">📭</span>No documents loaded yet.</div>
      </div>
    </div>

  </main>
</div>

<!-- Spinner overlay -->
<div class="spinner-overlay" id="spinner">
  <div class="spinner"></div>
  <div class="spinner-text" id="spinner-text">Processing…</div>
</div>

<!-- Toast container -->
<div class="toast-container" id="toasts"></div>

<script>
// ─── Panel switching ──────────────────────────────────────────────────
function switchPanel(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  event.currentTarget.classList.add('active');
  if (name === 'documents') loadDocuments();
}

// ─── Toast ───────────────────────────────────────────────────────────
function toast(msg, type = 'info', duration = 4000) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${msg}</span>`;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), duration);
}

// ─── Spinner ─────────────────────────────────────────────────────────
function showSpinner(text = 'Processing…') {
  document.getElementById('spinner-text').textContent = text;
  document.getElementById('spinner').classList.add('show');
}
function hideSpinner() { document.getElementById('spinner').classList.remove('show'); }

// ─── API helper ───────────────────────────────────────────────────────
async function apiCall(formData) {
  const resp = await fetch('src/api.php', { method: 'POST', body: formData });
  const data = await resp.json().catch(() => ({ error: 'Invalid response from server' }));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

// ─── Query ───────────────────────────────────────────────────────────
async function submitQuery() {
  const q = document.getElementById('query-input').value.trim();
  if (!q) { toast('Please enter a question.', 'error'); return; }
  const btn = document.getElementById('query-btn');
  btn.disabled = true;
  showSpinner('Searching and generating answer…');
  try {
    const fd = new FormData();
    fd.append('action', 'query');
    fd.append('query', q);
    fd.append('top_k', document.getElementById('topk').value);
    const data = await apiCall(fd);
    renderAnswer(data);
  } catch(e) {
    toast('Query failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    hideSpinner();
  }
}

function renderAnswer(data) {
  const area = document.getElementById('answer-area');
  const sourcesHtml = (data.sources || []).map(s => `
    <div class="source-card">
      <div class="source-header">
        <span class="source-file">📄 ${esc(s.filename)}</span>
        <span class="source-score">${(s.score * 100).toFixed(1)}% match</span>
      </div>
      <div class="source-excerpt">${esc(s.excerpt)}</div>
    </div>
  `).join('');

  area.innerHTML = `
    <div class="answer-card">
      <div class="answer-header">
        <span class="answer-title">💬 Answer</span>
        <span class="model-badge">${esc(data.model_used || 'unknown')}</span>
      </div>
      <div class="answer-text">${esc(data.answer)}</div>
    </div>
    ${sourcesHtml ? `
    <div class="section-header" style="margin-top:24px">
      <span class="section-title">📎 Sources (${data.sources.length})</span>
    </div>
    <div class="sources-grid">${sourcesHtml}</div>` : ''}
  `;
}

// ─── Upload file ─────────────────────────────────────────────────────
async function uploadFile(input) {
  const file = input.files[0];
  if (!file) return;
  const maxBytes = <?= MAX_FILE_SIZE_MB ?> * 1024 * 1024;
  if (file.size > maxBytes) { toast('File too large (max <?= MAX_FILE_SIZE_MB ?>MB)', 'error'); return; }

  showSpinner(`Uploading ${file.name}…`);
  try {
    const fd = new FormData();
    fd.append('action', 'upload');
    fd.append('file', file);
    const data = await apiCall(fd);
    toast(`✅ Indexed "${data.filename}" — ${data.chunks_indexed} chunks`, 'success', 5000);
    refreshStats();
  } catch(e) {
    toast('Upload failed: ' + e.message, 'error');
  } finally {
    hideSpinner();
    input.value = '';
  }
}

// ─── Ingest text ─────────────────────────────────────────────────────
async function ingestText() {
  const text = document.getElementById('ingest-text').value.trim();
  const name = document.getElementById('ingest-name').value.trim() || 'manual_input.txt';
  if (text.length < 20) { toast('Text too short (min 20 characters)', 'error'); return; }
  showSpinner('Indexing text…');
  try {
    const fd = new FormData();
    fd.append('action', 'ingest_text');
    fd.append('text', text);
    fd.append('filename', name);
    const data = await apiCall(fd);
    toast(`✅ Indexed "${data.filename}" — ${data.chunks_indexed} chunks`, 'success', 5000);
    document.getElementById('ingest-text').value = '';
    refreshStats();
  } catch(e) {
    toast('Ingest failed: ' + e.message, 'error');
  } finally {
    hideSpinner();
  }
}

// ─── Documents ───────────────────────────────────────────────────────
async function loadDocuments() {
  const list = document.getElementById('doc-list');
  list.innerHTML = '<div class="empty-state"><span class="icon">⏳</span>Loading…</div>';
  try {
    const fd = new FormData();
    fd.append('action', 'list_documents');
    const data = await apiCall(fd);
    if (!data.length) {
      list.innerHTML = '<div class="empty-state"><span class="icon">📭</span>No documents indexed yet.</div>';
      return;
    }
    list.innerHTML = data.map(doc => `
      <div class="doc-card" id="doc-${esc(doc.doc_id)}">
        <div class="doc-info">
          <span class="doc-name">📄 ${esc(doc.filename)}</span>
          <span class="doc-meta">ID: ${esc(doc.doc_id)} · ${doc.chunk_count} chunks</span>
        </div>
        <button class="btn-danger" onclick="deleteDoc('${esc(doc.doc_id)}', '${esc(doc.filename)}')">🗑 Delete</button>
      </div>
    `).join('');
  } catch(e) {
    list.innerHTML = `<div class="empty-state">Error: ${esc(e.message)}</div>`;
  }
}

async function deleteDoc(doc_id, filename) {
  if (!confirm(`Delete "${filename}" and all its chunks?`)) return;
  showSpinner('Deleting…');
  try {
    const fd = new FormData();
    fd.append('action', 'delete_document');
    fd.append('doc_id', doc_id);
    const data = await apiCall(fd);
    toast(`🗑 Deleted ${data.deleted} chunks from "${filename}"`, 'info');
    document.getElementById('doc-' + doc_id)?.remove();
    refreshStats();
  } catch(e) {
    toast('Delete failed: ' + e.message, 'error');
  } finally {
    hideSpinner();
  }
}

// ─── Stats refresh ────────────────────────────────────────────────────
async function refreshStats() {
  try {
    const fd = new FormData();
    fd.append('action', 'stats');
    const data = await apiCall(fd);
    document.getElementById('stat-chunks').textContent = data.total_chunks ?? '—';
  } catch {}
}

// ─── Drag & drop ──────────────────────────────────────────────────────
const dz = document.getElementById('drop-zone');
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('dragover'); });
dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file) {
    const fi = document.getElementById('file-input');
    const dt = new DataTransfer(); dt.items.add(file); fi.files = dt.files;
    uploadFile(fi);
  }
});

// ─── Keyboard shortcut ────────────────────────────────────────────────
document.getElementById('query-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submitQuery();
});

// ─── Sanitize ─────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>
