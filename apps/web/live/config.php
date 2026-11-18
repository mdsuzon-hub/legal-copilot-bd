<?php
/**
 * DIU Assistant — Configuration
 * Copy this file to config.php and fill in your API keys.
 *
 * NEVER commit config.php to version control.
 */

// ── Required: Groq API Key ─────────────────────────────────────────────────
// Get your FREE key at: https://console.groq.com/keys
define('GROQ_API_KEY', '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');

// ── Optional: SerpAPI Key (for better web search) ─────────────────────────
// Get at: https://serpapi.com  (100 free searches/month)
// Leave empty to use DuckDuckGo scraping (free, no key needed)
define('SERPAPI_KEY', '');

// ── Groq Model ────────────────────────────────────────────────────────────
// Options: openai/gpt-oss-120b, llama3-70b-8192, mixtral-8x7b-32768
define('GROQ_MODEL', 'openai/gpt-oss-120b');

// ── RAG Server (Python) ────────────────────────────────────────────────────
// Start with: python rag_server.py --port 5001
define('RAG_SERVER_URL', 'http://127.0.0.1:5001');

// ── DIU Notice Board URL ───────────────────────────────────────────────────
define('DIU_NOTICE_URL', 'https://noticeboard.diu.edu.bd/');

// ── Cache Settings ─────────────────────────────────────────────────────────
define('CACHE_DIR',        __DIR__ . '/rag_cache');
define('NOTICE_CACHE_TTL', 600);  // seconds (10 minutes)
