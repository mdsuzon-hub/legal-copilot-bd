<?php

/**
 * ╔══════════════════════════════════════════════════════════════════════════╗
 * ║  DIU Assistant — Production PHP Backend  api.php                        ║
 * ║  Handles: General Chat · RAG · Web Search · DIU Notice Scraping         ║
 * ╚══════════════════════════════════════════════════════════════════════════╝
 *
 * SETUP:
 *   1. Place this file at the root alongside index.html
 *   2. (Optional) copy config.php.example → config.php and fill in keys.
 *      Without config.php, everything is read from environment variables.
 *   3. pip install flask groq chromadb pdfplumber sentence-transformers
 *   4. Start RAG server (same folder as rag_pdf.py):  python rag_server.py
 *      It listens on http://127.0.0.1:5001
 *   5. Serve this directory with Apache/Nginx/`php -S`
 *
 * CONSTANTS (config.php or env):
 *   GROQ_API_KEY, GROQ_MODEL (default openai/gpt-oss-120b), SERPAPI_KEY,
 *   RAG_SERVER_URL (default http://127.0.0.1:5001), DIU_NOTICE_URL,
 *   CACHE_DIR, NOTICE_CACHE_TTL
 */

// ── Error handling ────────────────────────────────────────────────────────────
error_reporting(E_ALL);
ini_set('display_errors', 0);
ini_set('log_errors', 1);
set_time_limit(180);

// ── CORS headers ──────────────────────────────────────────────────────────────
header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit;
}
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    jsonError('Method not allowed', 405);
    exit;
}

// ── Config ────────────────────────────────────────────────────────────────────
// config.php is optional. Any constant it does not define falls back to env/default,
// so a partial config.php can never cause "Undefined constant" or an empty RAG URL.
$configFile = __DIR__ . '/config.php';
if (file_exists($configFile)) {
    require_once $configFile;
}
defined('GROQ_API_KEY')      || define('GROQ_API_KEY',      getenv('GROQ_API_KEY')      ?: '');
defined('GROQ_MODEL')        || define('GROQ_MODEL',        getenv('GROQ_MODEL')        ?: '');
defined('SERPAPI_KEY')       || define('SERPAPI_KEY',       getenv('SERPAPI_KEY')       ?: '');
defined('RAG_SERVER_URL')    || define('RAG_SERVER_URL',    getenv('RAG_SERVER_URL')    ?: '');
defined('DIU_NOTICE_URL')    || define('DIU_NOTICE_URL',    'https://noticeboard.diu.edu.bd/');
defined('CACHE_DIR')         || define('CACHE_DIR',         __DIR__ . '/rag_cache');
defined('NOTICE_CACHE_TTL')  || define('NOTICE_CACHE_TTL',  600); // 10 minutes

/** Effective Groq model (never empty). */
function groqModel(): string
{
    return GROQ_MODEL ?: 'openai/gpt-oss-120b';
}

/** Effective RAG base URL (never empty; 127.0.0.1 avoids Windows localhost→IPv6 issues). */
function ragBaseUrl(): string
{
    return rtrim(RAG_SERVER_URL ?: 'http://127.0.0.1:5001', '/');
}

// ── Input validation ──────────────────────────────────────────────────────────
$question = trim($_POST['question'] ?? '');
$mode     = trim($_POST['mode']     ?? 'general');  // general | rag | web
$histJson = trim($_POST['history']  ?? '[]');

if ($question === '') {
    jsonError('Question is required');
    exit;
}
if (mb_strlen($question) > 4000) {
    jsonError('Question too long (max 4000 chars)');
    exit;
}

$mode     = in_array($mode, ['general', 'rag', 'web'], true) ? $mode : 'general';
$history  = json_decode($histJson, true) ?: [];

// Sanitize history
$history = array_filter(
    $history,
    fn($h) =>
    is_array($h) &&
        isset($h['role'], $h['content']) &&
        in_array($h['role'], ['user', 'assistant'], true) &&
        is_string($h['content'])
);
$history = array_values(array_slice($history, -16));  // keep last 16 turns

// ── Uploaded file (optional) ──────────────────────────────────────────────────
$fileData = null;
if (!empty($_POST['file_data']) && !empty($_POST['file_name'])) {
    $fileData = [
        'name' => basename($_POST['file_name']),
        'type' => $_POST['file_type'] ?? 'application/octet-stream',
        'data' => $_POST['file_data'],   // base64
    ];
}

// ── Route to handler ──────────────────────────────────────────────────────────
try {
    $isNoticeQuery = isNoticeRelated($question);

    if ($mode === 'rag') {
        $result = handleRag($question, $history, $fileData);
    } elseif ($mode === 'web') {
        $result = handleWeb($question, $history);
    } elseif ($isNoticeQuery) {
        // Auto-detect notice queries even in general mode
        $result = handleNotice($question, $history);
    } else {
        $result = handleGeneral($question, $history, $fileData);
    }

    echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
} catch (Throwable $e) {
    error_log('[DIU API] ' . $e->getMessage() . ' in ' . $e->getFile() . ':' . $e->getLine());
    jsonError('Server error: ' . $e->getMessage());
}

// ════════════════════════════════════════════════════════════════════════════
//  HANDLER: General Chat
// ════════════════════════════════════════════════════════════════════════════
function handleGeneral(string $question, array $history, ?array $fileData): array
{
    $systemPrompt = <<<PROMPT
You are the DIU (Daffodil International University) Smart Assistant — a helpful, 
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
- Notable for: Software Engineering, CSE, Business, Law departments
PROMPT;

    $messages = buildMessages($systemPrompt, $history, $question, $fileData);
    $response = callGroq($messages, 0.7, 2048);
    $answer   = $response['answer'] ?? 'Sorry, I could not generate a response.';

    return [
        'answer'     => $answer,
        'mode'       => 'general',
        'sources'    => [],
        'notices'    => [],
        'related'    => generateRelated($question, $answer),
        'model_name' => groqModelName(),
    ];
}

// ════════════════════════════════════════════════════════════════════════════
//  HANDLER: RAG (calls Python rag_server.py)
// ════════════════════════════════════════════════════════════════════════════
function handleRag(string $question, array $history, ?array $fileData): array
{
    $uploadNote = '';

    // A PDF attached in RAG mode is added to the knowledge base first.
    if ($fileData && isPdfFile($fileData)) {
        $up = ragUploadPdf($fileData);
        if ($up['ok']) {
            $uploadNote = "📎 **{$fileData['name']}** added to the knowledge base"
                . ($up['chunks'] ? " ({$up['chunks']} chunks total)" : '') . ".\n\n";
        } else {
            $uploadNote = "⚠️ Could not index **{$fileData['name']}**: {$up['error']}\n\n";
        }
        $fileData = null; // already handled; don't inline it again
    }

    $res = ragRequest('POST', '/ask', [
        'question' => $question,
        'history'  => $history,
        'top_k'    => 6,
    ]);

    $data = $res['data'];

    // Server unreachable, or returned an error → explain precisely, then fall back
    if ($res['error'] || $res['status'] !== 200 || !is_array($data) || isset($data['error'])) {
        $reason = $res['error']
            ?: ($data['error'] ?? ('HTTP ' . $res['status']));
        error_log('[DIU API] RAG failure: ' . $reason . ' (' . ragBaseUrl() . ')');

        $fallback = handleGeneral($question, $history, $fileData);
        $fallback['answer'] = "⚠️ **RAG server unavailable** — {$reason}\n"
            . "Make sure `rag_server.py` is running at `" . ragBaseUrl() . "`. "
            . "The answer below is general knowledge, **not** from your documents.\n\n"
            . $uploadNote . $fallback['answer'];
        $fallback['mode'] = 'rag';
        return $fallback;
    }

    // Prefer cited sources; if the model cited none, show all retrieved ones
    $rawSources = is_array($data['sources'] ?? null) ? $data['sources'] : [];
    $cited      = array_filter($rawSources, fn($s) => !empty($s['cited']));
    $shown      = array_values($cited ?: $rawSources);

    $sources = array_map(function ($s) {
        $title = $s['source'] ?? $s['title'] ?? 'Document';
        if (!empty($s['page'])) $title .= ' · p.' . $s['page'];
        return [
            'title'   => $title,
            'url'     => $s['url']     ?? '',
            'favicon' => $s['favicon'] ?? '📄',
            'snippet' => $s['snippet'] ?? '',
            'tag'     => $s['tag']     ?? '',
        ];
    }, $shown);

    $answer = $data['answer'] ?? 'No answer from RAG.';

    return [
        'answer'     => $uploadNote . $answer,
        'mode'       => 'rag',
        'sources'    => $sources,
        'notices'    => [],
        'related'    => !empty($data['related']) ? $data['related'] : generateRelated($question, $answer),
        'model_name' => $data['model_name'] ?? groqModelName(),
    ];
}

/**
 * Call the Python RAG server.
 * @return array{status:int, data:mixed, error:string}
 */
function ragRequest(string $method, string $path, $body = null, int $timeout = 120): array
{
    $ch = curl_init(ragBaseUrl() . $path);
    $opts = [
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => $timeout,
        CURLOPT_CONNECTTIMEOUT => 5,
    ];
    if ($body instanceof Traversable || is_array($body)) {
        if (isset($body['__multipart'])) {
            unset($body['__multipart']);
            $opts[CURLOPT_POSTFIELDS] = $body;  // multipart (CURLFile)
        } else {
            $opts[CURLOPT_POSTFIELDS] = json_encode($body, JSON_UNESCAPED_UNICODE);
            $opts[CURLOPT_HTTPHEADER] = ['Content-Type: application/json'];
        }
    }
    curl_setopt_array($ch, $opts);

    $raw    = curl_exec($ch);
    $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err    = curl_error($ch);
    curl_close($ch);

    return [
        'status' => $status,
        'data'   => $raw !== false ? json_decode((string) $raw, true) : null,
        'error'  => $err,
    ];
}

function isPdfFile(array $f): bool
{
    return stripos($f['type'] ?? '', 'pdf') !== false
        || strtolower(pathinfo($f['name'] ?? '', PATHINFO_EXTENSION)) === 'pdf';
}

/** Decode the base64 PDF and push it to the RAG server's /upload endpoint. */
function ragUploadPdf(array $f): array
{
    // data URLs ("data:application/pdf;base64,....") are accepted too
    $b64 = preg_replace('#^data:[^;]+;base64,#', '', $f['data']);
    $bin = base64_decode($b64, true);
    if ($bin === false || $bin === '') {
        return ['ok' => false, 'chunks' => 0, 'error' => 'invalid file data'];
    }

    $tmp = tempnam(sys_get_temp_dir(), 'diu_pdf_');
    file_put_contents($tmp, $bin);
    try {
        $res = ragRequest('POST', '/upload', [
            '__multipart' => true,
            'file'        => new CURLFile($tmp, 'application/pdf', $f['name']),
        ], 300);
    } finally {
        @unlink($tmp);
    }

    if ($res['error'] || $res['status'] !== 200) {
        return ['ok' => false, 'chunks' => 0,
                'error' => $res['error'] ?: ($res['data']['error'] ?? 'HTTP ' . $res['status'])];
    }
    return ['ok' => true, 'chunks' => (int) ($res['data']['chunks'] ?? 0), 'error' => ''];
}

// ════════════════════════════════════════════════════════════════════════════
//  HANDLER: Web Search
// ════════════════════════════════════════════════════════════════════════════
function handleWeb(string $question, array $history): array
{
    // Try SerpAPI first, fall back to DuckDuckGo scraping
    $searchResults = [];

    if (!empty(SERPAPI_KEY)) {
        $searchResults = searchViaSerpApi($question);
    } else {
        $searchResults = searchViaDDG($question);
    }

    // Build context from search results
    $contextParts = [];
    foreach (array_slice($searchResults, 0, 6) as $i => $r) {
        $contextParts[] = sprintf(
            "[%d] %s\nURL: %s\n%s",
            $i + 1,
            $r['title'] ?? '',
            $r['url']   ?? '',
            $r['snippet'] ?? ''
        );
    }
    $context = implode("\n\n", $contextParts);

    $systemPrompt = <<<PROMPT
You are the DIU Smart Assistant with web search capability.
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
{$context}
PROMPT;

    $messages = buildMessages($systemPrompt, $history, $question);
    $response = callGroq($messages, 0.3, 2048);
    $answer   = $response['answer'] ?? 'Could not generate answer from search results.';

    // Format sources for display
    $sources = array_map(fn($r) => [
        'title'   => $r['title']   ?? $r['url'] ?? 'Web Result',
        'url'     => $r['url']     ?? '',
        'favicon' => getDomainFavicon($r['url'] ?? ''),
    ], array_slice($searchResults, 0, 5));

    return [
        'answer'     => $answer,
        'mode'       => 'web',
        'sources'    => $sources,
        'notices'    => [],
        'related'    => generateRelated($question, $answer),
        'model_name' => groqModelName(),
    ];
}

// ════════════════════════════════════════════════════════════════════════════
//  HANDLER: DIU Notice Board
// ════════════════════════════════════════════════════════════════════════════
function handleNotice(string $question, array $history): array
{
    $notices = scrapeNotices();

    if (empty($notices)) {
        // Fallback to web search for notices
        return handleWeb($question . ' site:diu.edu.bd OR site:noticeboard.diu.edu.bd', $history);
    }

    // Filter notices relevant to the question
    $relevant  = filterRelevantNotices($notices, $question);
    $contextParts = [];

    foreach (array_slice($relevant ?: $notices, 0, 10) as $i => $n) {
        $contextParts[] = sprintf(
            "[Notice %d]\nTitle: %s\nDate: %s\nCategory: %s\nURL: %s\nSummary: %s",
            $i + 1,
            $n['title']    ?? '',
            $n['date']     ?? 'N/A',
            $n['category'] ?? 'General',
            $n['url']      ?? '',
            $n['summary']  ?? $n['title'] ?? ''
        );
    }

    $noticeContext = implode("\n\n", $contextParts);
    $totalNotices  = count($notices);

    $systemPrompt = <<<PROMPT
You are the DIU Smart Assistant specializing in Daffodil International University notices.
You have access to the latest notices from the DIU Notice Board (noticeboard.diu.edu.bd).

Total notices fetched: {$totalNotices}

LATEST DIU NOTICES:
{$noticeContext}

Rules:
- Answer the user's question using ONLY the notice data above
- List relevant notices with their titles, dates, and URLs
- If asking for "latest" or "recent" notices, list the most recent ones
- Format notice titles in **bold**
- Always include the notice URL so users can click for details
- If no notices match the query, list the most recent 3-5 notices
- Be helpful and organized
PROMPT;

    $messages = buildMessages($systemPrompt, $history, $question);
    $response = callGroq($messages, 0.2, 2048);
    $answer   = $response['answer'] ?? 'Could not retrieve notices at this time.';

    // Return top relevant notices as cards
    $noticeCards = array_slice($relevant ?: $notices, 0, 5);

    return [
        'answer'     => $answer,
        'mode'       => 'notice',
        'sources'    => [[
            'title'   => 'DIU Notice Board',
            'url'     => DIU_NOTICE_URL,
            'favicon' => '📋',
        ]],
        'notices'    => $noticeCards,
        'related'    => [
            'What are the latest examination notices from DIU?',
            'Are there any upcoming DIU events or seminars?',
            'What are the recent academic notices from DIU?',
            'Show me admission-related notices from DIU',
        ],
        'model_name' => groqModelName(),
    ];
}

// ════════════════════════════════════════════════════════════════════════════
//  DIU NOTICE SCRAPER
// ════════════════════════════════════════════════════════════════════════════
function scrapeNotices(): array
{
    $cacheFile = CACHE_DIR . '/diu_notices.json';

    // Return cached if fresh
    if (file_exists($cacheFile)) {
        $mtime = filemtime($cacheFile);
        if ((time() - $mtime) < NOTICE_CACHE_TTL) {
            $cached = json_decode(file_get_contents($cacheFile), true);
            if (!empty($cached)) return $cached;
        }
    }

    $notices = [];

    // Try multiple pages of the notice board
    $pages = [
        DIU_NOTICE_URL,
        DIU_NOTICE_URL . '?page=1',
    ];

    foreach ($pages as $url) {
        $html = httpGet($url, 15);
        if (empty($html)) continue;

        $pageNotices = parseNoticeBoard($html, $url);
        foreach ($pageNotices as $n) {
            // Deduplicate by title
            $exists = false;
            foreach ($notices as $existing) {
                if ($existing['title'] === $n['title']) {
                    $exists = true;
                    break;
                }
            }
            if (!$exists) $notices[] = $n;
        }

        if (count($notices) >= 50) break;
        usleep(300000); // 300ms between requests
    }

    // Cache the result
    if (!is_dir(CACHE_DIR)) mkdir(CACHE_DIR, 0755, true);
    if (!empty($notices)) {
        file_put_contents($cacheFile, json_encode($notices, JSON_UNESCAPED_UNICODE));
    }

    return $notices;
}

function parseNoticeBoard(string $html, string $baseUrl): array
{
    $notices = [];

    // Suppress libxml errors
    libxml_use_internal_errors(true);
    $doc = new DOMDocument();
    $doc->loadHTML('<?xml encoding="UTF-8">' . $html, LIBXML_NOWARNING | LIBXML_NOERROR);
    libxml_clear_errors();

    $xpath = new DOMXPath($doc);

    // Strategy 1: Look for notice list items / table rows
    $strategies = [
        // Common notice board patterns
        "//table//tr[td]",
        "//ul[contains(@class,'notice')]//li",
        "//div[contains(@class,'notice')]",
        "//div[contains(@class,'post')]",
        "//article",
        "//div[contains(@class,'item')]//a[@href]",
        // Generic: any link within likely content areas
        "//div[contains(@class,'content')]//a[@href]",
        "//div[contains(@class,'main')]//a[@href]",
        "//main//a[@href]",
    ];

    foreach ($strategies as $xpath_query) {
        $nodes = $xpath->query($xpath_query);
        if (!$nodes || $nodes->length === 0) continue;

        foreach ($nodes as $node) {
            $notice = extractNoticeFromNode($node, $xpath, $baseUrl);
            if ($notice) {
                $notices[] = $notice;
                if (count($notices) >= 50) break 2;
            }
        }

        if (!empty($notices)) break;
    }

    // Strategy 2: Fallback — grab all links that look like notices
    if (empty($notices)) {
        $links = $xpath->query("//a[@href]");
        if ($links) {
            foreach ($links as $link) {
                $href  = trim($link->getAttribute('href'));
                $text  = trim($link->textContent);

                if (empty($text) || strlen($text) < 10 || strlen($text) > 300) continue;
                if (preg_match('/^\s*(home|about|contact|login|menu|nav|header|footer)\s*$/i', $text)) continue;

                $fullUrl = resolveUrl($href, $baseUrl);
                if (empty($fullUrl)) continue;

                $notices[] = [
                    'title'    => cleanText($text),
                    'url'      => $fullUrl,
                    'date'     => extractDateFromText($text),
                    'category' => guessCategory($text),
                    'summary'  => cleanText($text),
                ];

                if (count($notices) >= 40) break;
            }
        }
    }

    return $notices;
}

function extractNoticeFromNode(DOMNode $node, DOMXPath $xpath, string $baseUrl): ?array
{
    $text = trim($node->textContent);
    if (strlen($text) < 8 || strlen($text) > 2000) return null;

    // Skip navigation-like content
    if (preg_match('/^\s*(home|about|contact|login|logout|menu|search)\s*$/i', $text)) return null;

    // Find link
    $linkNode = null;
    if ($node->nodeName === 'a') {
        $linkNode = $node;
    } else {
        $links = $xpath->query('.//a[@href]', $node);
        if ($links && $links->length > 0) $linkNode = $links->item(0);
    }

    $url = '';
    $title = '';

    if ($linkNode) {
        $href  = trim($linkNode->getAttribute('href'));
        $url   = resolveUrl($href, $baseUrl);
        $title = cleanText($linkNode->textContent) ?: cleanText($text);
    } else {
        $title = cleanText($text);
    }

    if (empty($title) || strlen($title) < 8) return null;

    // Extract date
    $date = '';
    $datePatterns = [
        '/(\d{1,2}[-\/]\d{1,2}[-\/]\d{2,4})/',
        '/(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})/i',
        '/(\d{4}-\d{2}-\d{2})/',
    ];
    foreach ($datePatterns as $pattern) {
        if (preg_match($pattern, $text, $m)) {
            $date = $m[1];
            break;
        }
    }

    return [
        'title'    => $title,
        'url'      => $url,
        'date'     => $date ?: extractDateFromText($text),
        'category' => guessCategory($title),
        'summary'  => mb_strlen($text) > 200 ? mb_substr($text, 0, 200) . '…' : $text,
    ];
}

function extractDateFromText(string $text): string
{
    $patterns = [
        '/(\d{1,2}[-\/]\d{1,2}[-\/]\d{2,4})/',
        '/(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})/i',
        '/(\d{4}-\d{2}-\d{2})/',
        '/(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})/i',
    ];
    foreach ($patterns as $p) {
        if (preg_match($p, $text, $m)) return $m[1];
    }
    return '';
}

function guessCategory(string $text): string
{
    $text = strtolower($text);
    $categories = [
        'Exam'        => ['exam', 'examination', 'result', 'semester', 'midterm', 'final'],
        'Admission'   => ['admission', 'enroll', 'apply', 'application', 'intake'],
        'Academic'    => ['class', 'schedule', 'course', 'academic', 'lecture', 'semester'],
        'Event'       => ['event', 'seminar', 'workshop', 'conference', 'program', 'ceremony'],
        'Holiday'     => ['holiday', 'vacation', 'leave', 'break', 'close'],
        'Scholarship' => ['scholarship', 'financial', 'aid', 'waiver', 'stipend'],
        'Fee'         => ['fee', 'payment', 'tuition', 'charge', 'due'],
        'Job'         => ['job', 'career', 'recruitment', 'vacancy', 'position'],
    ];
    foreach ($categories as $cat => $keywords) {
        foreach ($keywords as $kw) {
            if (str_contains($text, $kw)) return $cat;
        }
    }
    return 'General';
}

function filterRelevantNotices(array $notices, string $question): array
{
    $qLower = strtolower($question);
    $qWords = array_filter(preg_split('/\W+/', $qLower), fn($w) => strlen($w) > 3);

    // Score each notice
    $scored = [];
    foreach ($notices as $notice) {
        $titleLower    = strtolower($notice['title'] ?? '');
        $categoryLower = strtolower($notice['category'] ?? '');
        $score = 0;

        foreach ($qWords as $word) {
            if (str_contains($titleLower, $word))    $score += 3;
            if (str_contains($categoryLower, $word)) $score += 2;
        }

        // Boost recent notices
        if (!empty($notice['date'])) $score += 1;

        if ($score > 0) $scored[] = ['notice' => $notice, 'score' => $score];
    }

    usort($scored, fn($a, $b) => $b['score'] <=> $a['score']);
    return array_map(fn($s) => $s['notice'], array_slice($scored, 0, 10));
}

// ════════════════════════════════════════════════════════════════════════════
//  WEB SEARCH IMPLEMENTATIONS
// ════════════════════════════════════════════════════════════════════════════
function searchViaSerpApi(string $query): array
{
    $url = 'https://serpapi.com/search?' . http_build_query([
        'q'       => $query,
        'api_key' => SERPAPI_KEY,
        'num'     => 8,
        'hl'      => 'en',
    ]);

    $body = httpGet($url, 15);
    if (!$body) return [];

    $data = json_decode($body, true);
    if (!isset($data['organic_results'])) return [];

    return array_map(fn($r) => [
        'title'   => $r['title']   ?? '',
        'url'     => $r['link']    ?? '',
        'snippet' => $r['snippet'] ?? '',
    ], $data['organic_results']);
}

function searchViaDDG(string $query): array
{
    // DuckDuckGo HTML scraping (no API key required)
    $url = 'https://html.duckduckgo.com/html/?' . http_build_query(['q' => $query]);

    $html = httpGet($url, 15, [
        'User-Agent: Mozilla/5.0 (compatible; DIU-Assistant/1.0)',
        'Accept-Language: en-US,en;q=0.9',
    ]);

    if (!$html) return searchViaGoogle($query);

    libxml_use_internal_errors(true);
    $doc = new DOMDocument();
    $doc->loadHTML('<?xml encoding="UTF-8">' . $html, LIBXML_NOWARNING | LIBXML_NOERROR);
    libxml_clear_errors();

    $xpath   = new DOMXPath($doc);
    $results = [];

    $resultDivs = $xpath->query("//div[contains(@class,'result')]");
    if (!$resultDivs) return [];

    foreach ($resultDivs as $div) {
        $titleNode   = $xpath->query(".//a[contains(@class,'result__a')]", $div)->item(0);
        $snippetNode = $xpath->query(".//*[contains(@class,'result__snippet')]", $div)->item(0);

        if (!$titleNode) continue;

        $title   = cleanText($titleNode->textContent);
        $snippet = $snippetNode ? cleanText($snippetNode->textContent) : '';
        $href    = $titleNode->getAttribute('href');

        // DDG wraps URLs
        $url = '';
        if (str_contains($href, 'uddg=')) {
            parse_str(parse_url($href, PHP_URL_QUERY) ?? '', $params);
            $url = urldecode($params['uddg'] ?? $href);
        } else {
            $url = $href;
        }

        if ($title && $url) {
            $results[] = ['title' => $title, 'url' => $url, 'snippet' => $snippet];
            if (count($results) >= 8) break;
        }
    }

    return $results;
}

function searchViaGoogle(string $query): array
{
    // Last resort: Google scraping (may be blocked — use SerpAPI in production)
    $url = 'https://www.google.com/search?' . http_build_query([
        'q'   => $query,
        'num' => 8,
        'hl'  => 'en',
    ]);

    $html = httpGet($url, 10, [
        'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language: en-US,en;q=0.9',
    ]);

    if (!$html) return [['title' => 'Web search unavailable', 'url' => '', 'snippet' => 'No web search results available. Please configure a search API.']];

    // Parse Google results
    $results = [];
    libxml_use_internal_errors(true);
    $doc = new DOMDocument();
    $doc->loadHTML('<?xml encoding="UTF-8">' . $html, LIBXML_NOWARNING | LIBXML_NOERROR);
    libxml_clear_errors();

    $xpath = new DOMXPath($doc);
    $divs  = $xpath->query("//div[@class='g']") ?: $xpath->query("//div[contains(@class,'tF2Cxc')]");

    if ($divs) {
        foreach ($divs as $div) {
            $link    = $xpath->query(".//a[@href]", $div)->item(0);
            $h3      = $xpath->query(".//h3", $div)->item(0);
            $snippet = $xpath->query(".//*[@class='VwiC3b' or contains(@class,'snippet')]", $div)->item(0);

            if (!$link || !$h3) continue;

            $results[] = [
                'title'   => cleanText($h3->textContent),
                'url'     => $link->getAttribute('href'),
                'snippet' => $snippet ? cleanText($snippet->textContent) : '',
            ];
            if (count($results) >= 8) break;
        }
    }

    return $results ?: [['title' => 'No results found', 'url' => '', 'snippet' => '']];
}

// ════════════════════════════════════════════════════════════════════════════
//  GROQ API
// ════════════════════════════════════════════════════════════════════════════
function callGroq(array $messages, float $temp = 0.5, int $maxTokens = 1024, string $effort = 'low'): array
{
    if (empty(GROQ_API_KEY)) {
        return ['answer' => '⚠️ Groq API key not configured. Please add your key to config.php or set GROQ_API_KEY.'];
    }

    $body = [
        'model'       => groqModel(),
        'messages'    => $messages,
        'temperature' => $temp,
        'max_tokens'  => $maxTokens,
        'stream'      => false,
    ];
    // gpt-oss models spend max_tokens on hidden reasoning; keep it low so answers aren't cut off
    if (stripos(groqModel(), 'gpt-oss') !== false) {
        $body['reasoning_effort'] = $effort;
    }
    $payload = json_encode($body, JSON_UNESCAPED_UNICODE);

    $ch = curl_init('https://api.groq.com/openai/v1/chat/completions');
    curl_setopt_array($ch, [
        CURLOPT_POST           => true,
        CURLOPT_POSTFIELDS     => $payload,
        CURLOPT_HTTPHEADER     => [
            'Content-Type: application/json',
            'Authorization: Bearer ' . GROQ_API_KEY,
        ],
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 60,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_SSL_VERIFYPEER => true,
    ]);

    $respBody = curl_exec($ch);
    $status   = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err      = curl_error($ch);
    curl_close($ch);

    if ($err) throw new RuntimeException("Groq API cURL error: $err");
    if ($status === 429) return ['answer' => '⚠️ Rate limit exceeded. Please wait a moment and try again.'];
    if ($status === 401) return ['answer' => '⚠️ Invalid API key. Please check your Groq API key.'];
    if ($status !== 200) throw new RuntimeException("Groq API error HTTP $status: " . substr((string) $respBody, 0, 200));

    $data = json_decode((string) $respBody, true);
    if (!$data) throw new RuntimeException("Groq API returned invalid JSON");
    if (isset($data['error'])) throw new RuntimeException($data['error']['message'] ?? 'Groq API error');

    $content = trim($data['choices'][0]['message']['content'] ?? '');
    if ($content === '') {
        return ['answer' => '⚠️ The model returned an empty response. Please try again.'];
    }
    return ['answer' => $content];
}

function buildMessages(string $system, array $history, string $question, ?array $fileData = null): array
{
    $messages = [['role' => 'system', 'content' => $system]];

    foreach ($history as $h) {
        $messages[] = ['role' => $h['role'], 'content' => $h['content']];
    }

    $userContent = $question;
    if ($fileData) {
        $userContent .= "\n\n[Attached file: {$fileData['name']}]\n";
        if (str_contains($fileData['type'], 'text') || str_contains($fileData['name'], '.txt') || str_contains($fileData['name'], '.md')) {
            $decoded = base64_decode($fileData['data']);
            if ($decoded) {
                $userContent .= mb_substr($decoded, 0, 4000);
            }
        }
    }

    $messages[] = ['role' => 'user', 'content' => $userContent];
    return $messages;
}

function generateRelated(string $question, string $answer): array
{
    // Generate related questions via Groq
    $systemPrompt = "Generate 3 short follow-up questions a user might ask after receiving this answer. Return ONLY a JSON array of 3 strings. No explanation. Keep questions concise (under 80 chars each). Use the same language as the original question.";

    $messages = [
        ['role' => 'system', 'content' => $systemPrompt],
        ['role' => 'user', 'content' => "Original question: $question\n\nAnswer summary: " . mb_substr($answer, 0, 300)],
    ];

    try {
        $response = callGroq($messages, 0.8, 600);
        $text = $response['answer'] ?? '';
        // Strip markdown code fences if present
        $text = preg_replace('/```json|```/', '', $text);
        $text = trim($text);
        $arr  = json_decode($text, true);
        if (is_array($arr) && count($arr) >= 2) {
            return array_slice($arr, 0, 4);
        }
    } catch (Throwable $e) {
        error_log('[DIU] generateRelated error: ' . $e->getMessage());
    }

    // Fallback static related questions
    return [
        'Can you tell me more about this topic?',
        'What are the official DIU resources for this?',
        'Are there any recent updates related to this?',
    ];
}

function groqModelName(): string
{
    $names = [
        'openai/gpt-oss-120b'     => 'GPT-OSS 120B',
        'openai/gpt-oss-20b'      => 'GPT-OSS 20B',
        'llama-3.3-70b-versatile' => 'Llama 3.3 70B',
        'llama3-70b-8192'         => 'Llama 3 70B',
        'mixtral-8x7b-32768'      => 'Mixtral 8x7B',
        'gemma2-9b-it'            => 'Gemma 2 9B',
    ];
    return $names[groqModel()] ?? groqModel();
}

// ════════════════════════════════════════════════════════════════════════════
//  NOTICE DETECTION
// ════════════════════════════════════════════════════════════════════════════
function isNoticeRelated(string $question): bool
{
    $keywords = [
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
    ];
    // Word-start match: "result(s)" hits, but "prevent"/"eventually" no longer trigger "event"
    foreach ($keywords as $kw) {
        if (preg_match('/\b' . preg_quote($kw, '/') . '/iu', $question)) return true;
    }
    return false;
}

// ════════════════════════════════════════════════════════════════════════════
//  HTTP HELPERS
// ════════════════════════════════════════════════════════════════════════════
function httpGet(string $url, int $timeout = 15, array $extraHeaders = []): ?string
{
    $ch = curl_init($url);
    $headers = array_merge([
        'User-Agent: Mozilla/5.0 (compatible; DIU-Assistant/1.0; +https://diu.edu.bd)',
        'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language: en-US,en;q=0.9,bn;q=0.8',
        'Accept-Encoding: gzip',
        'Cache-Control: no-cache',
    ], $extraHeaders);

    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => $timeout,
        CURLOPT_CONNECTTIMEOUT => 8,
        CURLOPT_HTTPHEADER     => $headers,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
        CURLOPT_SSL_VERIFYPEER => false,  // Some BD sites have cert issues
        CURLOPT_ENCODING       => '',     // Enable decompression
    ]);

    $body = curl_exec($ch);
    $err  = curl_error($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($err) {
        error_log("[DIU] httpGet error for $url: $err");
        return null;
    }
    if ($code < 200 || $code >= 400) {
        error_log("[DIU] httpGet HTTP $code for $url");
        return null;
    }

    return $body ?: null;
}

function resolveUrl(string $href, string $baseUrl): string
{
    if (empty($href)) return '';
    if (str_starts_with($href, 'http://') || str_starts_with($href, 'https://')) return $href;
    if (str_starts_with($href, '//')) return 'https:' . $href;
    if (str_starts_with($href, '/')) {
        $parsed = parse_url($baseUrl);
        return ($parsed['scheme'] ?? 'https') . '://' . ($parsed['host'] ?? '') . $href;
    }
    if (str_starts_with($href, '#') || str_starts_with($href, 'javascript:') || str_starts_with($href, 'mailto:')) return '';
    return rtrim($baseUrl, '/') . '/' . ltrim($href, '/');
}

function cleanText(string $text): string
{
    $text = html_entity_decode($text, ENT_QUOTES | ENT_HTML5, 'UTF-8');
    $text = preg_replace('/\s+/', ' ', $text);
    return trim($text);
}

function getDomainFavicon(string $url): string
{
    if (empty($url)) return '🔗';
    $host = parse_url($url, PHP_URL_HOST) ?? '';
    $host = strtolower($host);
    $map  = [
        'diu.edu.bd'           => '🎓',
        'noticeboard.diu'      => '📋',
        'wikipedia.org'        => '📖',
        'google.com'           => '🔍',
        'youtube.com'          => '▶️',
        'github.com'           => '💻',
        'stackoverflow.com'    => '💡',
        'bdnews24.com'         => '📰',
        'prothomalo.com'       => '📰',
        'thedailystar.net'     => '📰',
    ];
    foreach ($map as $domain => $icon) {
        if (str_contains($host, $domain)) return $icon;
    }
    return '🔗';
}

// ════════════════════════════════════════════════════════════════════════════
//  RESPONSE HELPERS
// ════════════════════════════════════════════════════════════════════════════
function jsonError(string $message, int $code = 400): void
{
    http_response_code($code);
    echo json_encode(['error' => $message], JSON_UNESCAPED_UNICODE);
}