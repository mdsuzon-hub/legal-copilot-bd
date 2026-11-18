<?php

/**
 * PDF RAG Frontend - Perplexity-style UI
 * Connects to the FastAPI backend at http://localhost:8000
 * 
 * API Endpoints used:
 * - POST /upload          - Upload PDF
 * - POST /chat            - Ask question (non-streaming)
 * - POST /chat/stream     - SSE streaming answer
 * - GET  /status          - Index status
 * - POST /reset           - Reset index
 * - GET  /history         - Session history
 * - DELETE /history       - Clear history
 * - GET  /suggest         - Follow-up suggestions
 * - POST /config          - Configure Groq API key
 * 
 * Session management: PHP session stores session_id for API
 */

session_start();

// Configuration
define('API_BASE_URL', getenv('RAG_API_URL') ?: 'http://localhost:8000');
define('SESSION_TTL', 3600);

// Helper: Make API request
function apiRequest($endpoint, $method = 'GET', $data = null, $stream = false)
{
    $url = API_BASE_URL . $endpoint;
    $ch = curl_init();

    $headers = ['Content-Type: application/json'];

    if ($stream) {
        // For streaming, return raw curl handle with write function
        $ch = curl_init();
        curl_setopt($ch, CURLOPT_URL, $url);
        curl_setopt($ch, CURLOPT_POST, true);
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
        curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, false);
        curl_setopt($ch, CURLOPT_WRITEFUNCTION, function ($ch, $data) {
            echo $data;
            ob_flush();
            flush();
            return strlen($data);
        });
        curl_exec($ch);
        curl_close($ch);
        return null;
    }

    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);

    if ($method === 'POST') {
        curl_setopt($ch, CURLOPT_POST, true);
        if ($data) {
            curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
            curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
        }
    } elseif ($method === 'DELETE') {
        curl_setopt($ch, CURLOPT_CUSTOMREQUEST, 'DELETE');
    } elseif ($method === 'GET' && $data) {
        $url .= '?' . http_build_query($data);
        curl_setopt($ch, CURLOPT_URL, $url);
    }

    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($httpCode >= 400) {
        $error = json_decode($response, true);
        throw new Exception($error['detail'] ?? "API error (HTTP $httpCode)");
    }

    return json_decode($response, true);
}

// Get or create session ID for API
function getSessionId()
{
    if (!isset($_SESSION['rag_session_id'])) {
        $_SESSION['rag_session_id'] = 'web_' . uniqid();
    }
    return $_SESSION['rag_session_id'];
}

// Handle API actions from frontend
$action = $_POST['action'] ?? $_GET['action'] ?? null;
$response = ['success' => false, 'error' => null];

try {
    switch ($action) {
        case 'upload':
            if (!isset($_FILES['file']) || $_FILES['file']['error'] !== UPLOAD_ERR_OK) {
                throw new Exception('File upload failed');
            }

            $tmpFile = $_FILES['file']['tmp_name'];
            $filename = $_FILES['file']['name'];

            // Forward to API using multipart form
            $ch = curl_init();
            curl_setopt($ch, CURLOPT_URL, API_BASE_URL . '/upload?reset=true');
            curl_setopt($ch, CURLOPT_POST, true);
            curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($ch, CURLOPT_POSTFIELDS, [
                'file' => new CURLFile($tmpFile, 'application/pdf', $filename)
            ]);
            $result = curl_exec($ch);
            $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            curl_close($ch);

            if ($httpCode >= 400) {
                $err = json_decode($result, true);
                throw new Exception($err['detail'] ?? 'Upload failed');
            }

            $response = ['success' => true, 'data' => json_decode($result, true)];
            break;

        case 'chat':
            $question = $_POST['question'] ?? '';
            if (empty($question)) throw new Exception('Question required');

            $data = apiRequest('/chat', 'POST', [
                'question' => $question,
                'session_id' => getSessionId(),
                'top_k' => 8,
                'debug' => false
            ]);

            $response = ['success' => true, 'data' => $data];
            break;

        case 'chat_stream':
            header('Content-Type: text/event-stream');
            header('Cache-Control: no-cache');
            header('X-Accel-Buffering: no');
            ob_implicit_flush(true);
            ob_end_flush();

            $question = $_POST['question'] ?? '';
            if (empty($question)) {
                echo "data: " . json_encode(['type' => 'error', 'detail' => 'Question required']) . "\n\n";
                exit;
            }

            apiRequest('/chat/stream', 'POST', [
                'question' => $question,
                'session_id' => getSessionId(),
                'top_k' => 8,
                'debug' => false
            ], true);
            exit;

        case 'status':
            $data = apiRequest('/status', 'GET');
            $response = ['success' => true, 'data' => $data];
            break;

        case 'reset':
            $data = apiRequest('/reset', 'POST', ['confirm' => true]);
            $response = ['success' => true, 'data' => $data];
            break;

        case 'history':
            $data = apiRequest('/history', 'GET', ['session_id' => getSessionId()]);
            $response = ['success' => true, 'data' => $data];
            break;

        case 'clear_history':
            $data = apiRequest('/history', 'DELETE', ['session_id' => getSessionId()]);
            $response = ['success' => true, 'data' => $data];
            break;

        case 'suggest':
            $data = apiRequest('/suggest', 'GET', ['session_id' => getSessionId()]);
            $response = ['success' => true, 'data' => $data];
            break;

        case 'config':
            $apiKey = $_POST['groq_api_key'] ?? '';
            if (empty($apiKey)) throw new Exception('API key required');
            $data = apiRequest('/config', 'POST', ['groq_api_key' => $apiKey]);
            $response = ['success' => true, 'data' => $data];
            break;

        default:
            // Serve HTML page
            $statusData = @apiRequest('/status', 'GET');
            $isReady = $statusData && $statusData['ready'];
            $pdfName = $statusData['pdf_name'] ?? '';
            $chunkCount = $statusData['chunk_count'] ?? 0;
            $groqConfigured = $statusData['groq_configured'] ?? false;

            renderPage($isReady, $pdfName, $chunkCount, $groqConfigured);
            exit;
    }
} catch (Exception $e) {
    $response = ['success' => false, 'error' => $e->getMessage()];
}

if ($action) {
    header('Content-Type: application/json');
    echo json_encode($response);
    exit;
}

// HTML rendering function
function renderPage($isReady, $pdfName, $chunkCount, $groqConfigured)
{
    $sessionId = getSessionId();
?>
    <!DOCTYPE html>
    <html lang="en">

    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>PDF RAG - Perplexity Style</title>
        <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='%2320808D'/><path d='M8 8h16v2H8zm0 7h16v2H8zm0 7h10v2H8z' fill='white'/></svg>">
        <link rel="preconnect" href="https://fonts.googleapis.com">
            <link rel="stylesheet" href="scorces/test.css">
        <link href="https://fonts.googleapis.com/css2?family=Sora:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg: #0a0a0f;
                --bg2: #111217;
                --bg3: #1a1c23;
                --border: #2a2c35;
                --accent: #20808d;
                --accent-hover: #2a9d8f;
                --text: #e8edf2;
                --text2: #b4bec8;
                --text3: #6c7a8a;
                --text4: #4a5568;
                --danger: #e53e3e;
                --code-bg: #0d1117;
            }

            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }

            body {
                font-family: 'Sora', sans-serif;
                background: var(--bg);
                color: var(--text);
                height: 100vh;
                overflow: hidden;
            }

            /* Sidebar */
            #sidebar {
                position: fixed;
                left: 0;
                top: 0;
                width: 280px;
                height: 100%;
                background: var(--bg2);
                border-right: 1px solid var(--border);
                display: flex;
                flex-direction: column;
                z-index: 100;
                transition: transform 0.25s ease;
            }

            #sidebar.collapsed {
                transform: translateX(-280px);
            }

            @media (max-width: 768px) {
                #sidebar {
                    transform: translateX(-280px);
                }

                #sidebar.mob-open {
                    transform: translateX(0);
                }
            }

            .sb-head {
                padding: 20px;
                border-bottom: 1px solid var(--border);
            }

            .logo-wrap {
                display: flex;
                align-items: center;
                gap: 10px;
            }

            .logo-icon {
                width: 28px;
                height: 28px;
                background: var(--accent);
                border-radius: 8px;
                display: flex;
                align-items: center;
                justify-content: center;
            }

            .logo-icon svg {
                width: 16px;
                height: 16px;
                fill: white;
            }

            .logo-txt {
                font-weight: 700;
                font-size: 18px;
            }

            .sb-new {
                margin: 16px;
                padding: 12px;
                background: var(--bg3);
                border: 1px solid var(--border);
                border-radius: 12px;
                color: var(--text);
                font-weight: 500;
                display: flex;
                align-items: center;
                gap: 8px;
                cursor: pointer;
                transition: all 0.2s;
            }

            .sb-new:hover {
                background: var(--accent);
                border-color: var(--accent);
            }

            .sb-new svg {
                width: 18px;
                height: 18px;
            }

            .sb-scroll {
                flex: 1;
                overflow-y: auto;
                padding: 8px 12px;
            }

            .sb-sec {
                font-size: 11px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                color: var(--text3);
                margin: 16px 0 8px 8px;
            }

            .hi {
                padding: 10px 12px;
                border-radius: 10px;
                margin: 2px 0;
                display: flex;
                align-items: center;
                gap: 10px;
                cursor: pointer;
                transition: background 0.15s;
                font-size: 13px;
            }

            .hi:hover {
                background: var(--bg3);
            }

            .hi.active {
                background: rgba(32, 128, 141, 0.15);
                color: var(--accent);
            }

            .hi svg {
                width: 16px;
                height: 16px;
                opacity: 0.6;
                flex-shrink: 0;
            }

            .hi span {
                flex: 1;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            .hd {
                background: none;
                border: none;
                color: var(--text3);
                cursor: pointer;
                opacity: 0;
                transition: opacity 0.15s;
            }

            .hi:hover .hd {
                opacity: 1;
            }

            .sb-footer {
                padding: 16px;
                border-top: 1px solid var(--border);
            }

            .sb-fbtn {
                width: 100%;
                padding: 10px;
                background: var(--bg3);
                border: none;
                border-radius: 10px;
                color: var(--text2);
                display: flex;
                align-items: center;
                gap: 10px;
                cursor: pointer;
                margin-bottom: 12px;
            }

            .u-btn {
                width: 100%;
                padding: 10px;
                background: var(--bg3);
                border: none;
                border-radius: 12px;
                display: flex;
                align-items: center;
                gap: 12px;
                cursor: pointer;
            }

            .u-av {
                width: 36px;
                height: 36px;
                background: var(--accent);
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                font-weight: 600;
            }

            .u-info {
                text-align: left;
            }

            .u-name {
                font-size: 13px;
                font-weight: 500;
            }

            .u-plan {
                font-size: 11px;
                color: var(--text3);
            }

            /* Main */
            #main {
                margin-left: 280px;
                height: 100vh;
                display: flex;
                flex-direction: column;
                transition: margin-left 0.25s ease;
            }

            #sidebar.collapsed+#main {
                margin-left: 0;
            }

            @media (max-width: 768px) {
                #main {
                    margin-left: 0;
                }
            }

            #navbar {
                height: 60px;
                border-bottom: 1px solid var(--border);
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 0 20px;
                background: var(--bg);
            }

            .nb-toggle {
                background: none;
                border: none;
                color: var(--text2);
                cursor: pointer;
                display: none;
            }

            @media (max-width: 768px) {
                .nb-toggle {
                    display: block;
                }
            }

            .nb-toggle svg {
                width: 22px;
                height: 22px;
            }

            .nb-title {
                font-weight: 600;
            }

            .nb-actions {
                display: flex;
                align-items: center;
                gap: 12px;
            }

            .nb-btn,
            .nb-icon {
                background: var(--bg2);
                border: 1px solid var(--border);
                border-radius: 8px;
                padding: 6px 12px;
                color: var(--text2);
                cursor: pointer;
                display: flex;
                align-items: center;
                gap: 6px;
                font-size: 12px;
            }

            .nb-icon {
                padding: 6px;
            }

            /* Model Selector */
            .model-selector {
                position: relative;
                display: flex;
                align-items: center;
                gap: 8px;
                background: var(--bg2);
                border: 1px solid var(--border);
                border-radius: 30px;
                padding: 5px 12px;
                cursor: pointer;
            }

            .model-dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: #20808d;
                box-shadow: 0 0 6px #20808d80;
            }

            .model-name {
                font-size: 13px;
                font-weight: 500;
            }

            .model-arrow {
                width: 14px;
                height: 14px;
            }

            .model-dropdown {
                position: absolute;
                top: 100%;
                right: 0;
                margin-top: 8px;
                background: var(--bg2);
                border: 1px solid var(--border);
                border-radius: 16px;
                width: 280px;
                z-index: 200;
                display: none;
                box-shadow: 0 10px 25px rgba(0, 0, 0, 0.3);
            }

            .model-dropdown.open {
                display: block;
            }

            .md-header {
                padding: 10px 14px;
                font-size: 11px;
                font-weight: 600;
                color: var(--text3);
                text-transform: uppercase;
            }

            .md-item {
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 10px 14px;
                cursor: pointer;
                transition: background 0.1s;
            }

            .md-item:hover {
                background: var(--bg3);
            }

            .md-item.selected {
                background: rgba(32, 128, 141, 0.1);
            }

            .md-icon {
                width: 32px;
                height: 32px;
                border-radius: 8px;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 16px;
            }

            .md-info {
                flex: 1;
            }

            .md-label {
                font-size: 13px;
                font-weight: 500;
            }

            .md-desc {
                font-size: 11px;
                color: var(--text3);
            }

            .md-badge {
                font-size: 10px;
                padding: 2px 6px;
                background: rgba(32, 128, 141, 0.15);
                border-radius: 12px;
                color: var(--accent);
            }

            .md-sep {
                height: 1px;
                background: var(--border);
                margin: 6px 0;
            }

            /* Chat Area */
            #chat-area {
                flex: 1;
                overflow-y: auto;
                padding: 20px;
            }

            /* Home */
            #home {
                max-width: 800px;
                margin: 0 auto;
                padding-top: 80px;
            }

            .home-hero {
                text-align: center;
                margin-bottom: 40px;
            }

            .home-icon {
                width: 56px;
                height: 56px;
                background: var(--accent);
                border-radius: 20px;
                display: flex;
                align-items: center;
                justify-content: center;
                margin: 0 auto 20px;
            }

            .home-icon svg {
                width: 28px;
                height: 28px;
                fill: white;
            }

            .home-title {
                font-size: 32px;
                font-weight: 700;
                margin-bottom: 12px;
            }

            .home-title em {
                color: var(--accent);
                font-style: normal;
            }

            .home-sub {
                color: var(--text2);
            }

            .input-box {
                background: var(--bg2);
                border: 1px solid var(--border);
                border-radius: 24px;
                padding: 12px 16px;
                transition: border-color 0.2s;
            }

            .input-box:focus-within {
                border-color: var(--accent);
            }

            .ib-top {
                margin-bottom: 10px;
            }

            .qi {
                width: 100%;
                background: transparent;
                border: none;
                color: var(--text);
                font-family: inherit;
                font-size: 15px;
                resize: none;
                outline: none;
            }

            .ib-bot {
                display: flex;
                align-items: center;
                gap: 8px;
                flex-wrap: wrap;
            }

            .ib-act {
                background: var(--bg3);
                border: none;
                border-radius: 20px;
                padding: 6px 12px;
                color: var(--text2);
                font-size: 12px;
                display: flex;
                align-items: center;
                gap: 6px;
                cursor: pointer;
            }

            .ib-act.active {
                background: var(--accent);
                color: white;
            }

            .ib-act svg {
                width: 14px;
                height: 14px;
            }

            .ib-spacer {
                flex: 1;
            }

            .send-btn {
                background: var(--accent);
                border: none;
                border-radius: 50%;
                width: 34px;
                height: 34px;
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
            }

            .send-btn svg {
                width: 16px;
                height: 16px;
                fill: white;
            }

            .send-btn.stop-mode {
                background: var(--danger);
            }

            .chips {
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
                margin-top: 30px;
                justify-content: center;
            }

            .chip {
                background: var(--bg2);
                border: 1px solid var(--border);
                border-radius: 40px;
                padding: 8px 16px;
                font-size: 13px;
                display: flex;
                align-items: center;
                gap: 8px;
                cursor: pointer;
                transition: all 0.2s;
            }

            .chip:hover {
                background: var(--bg3);
                border-color: var(--accent);
            }

            .chip svg {
                width: 14px;
                height: 14px;
            }

            /* Chat View */
            #chat-view {
                max-width: 800px;
                margin: 0 auto;
            }

            .msg-group {
                margin-bottom: 24px;
            }

            .u-msg {
                display: flex;
                justify-content: flex-end;
            }

            .u-bubble {
                background: var(--bg3);
                border-radius: 24px;
                padding: 12px 18px;
                max-width: 80%;
            }

            .ai-msg {
                background: transparent;
            }

            .ai-head {
                display: flex;
                align-items: center;
                gap: 10px;
                margin-bottom: 12px;
            }

            .ai-av {
                width: 28px;
                height: 28px;
                background: var(--accent);
                border-radius: 8px;
                display: flex;
                align-items: center;
                justify-content: center;
            }

            .ai-av svg {
                width: 14px;
                height: 14px;
                fill: white;
            }

            .model-tag {
                font-size: 11px;
                padding: 2px 8px;
                background: var(--bg2);
                border-radius: 12px;
                color: var(--text2);
            }

            .ai-body {
                font-size: 14.5px;
                line-height: 1.6;
                color: var(--text2);
            }

            .ai-body h1,
            .ai-body h2,
            .ai-body h3 {
                color: var(--text);
                margin: 16px 0 8px;
            }

            .ai-body p {
                margin: 10px 0;
            }

            .ai-body code {
                background: var(--code-bg);
                padding: 2px 5px;
                border-radius: 4px;
                font-family: 'JetBrains Mono', monospace;
                font-size: 12px;
            }

            .ai-body pre {
                background: var(--code-bg);
                padding: 12px;
                border-radius: 12px;
                overflow-x: auto;
                margin: 12px 0;
            }

            .ai-body pre code {
                background: none;
                padding: 0;
            }

            .ai-body ul,
            .ai-body ol {
                margin: 10px 0 10px 20px;
            }

            .ai-body blockquote {
                border-left: 3px solid var(--accent);
                padding-left: 16px;
                margin: 12px 0;
                color: var(--text3);
            }

            .ai-body a {
                color: var(--accent);
                text-decoration: none;
            }

            .src-grid {
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
                margin: 12px 0;
            }

            .src-card {
                background: var(--bg2);
                border: 1px solid var(--border);
                border-radius: 12px;
                padding: 10px;
                width: calc(33% - 10px);
                min-width: 150px;
            }

            .src-num {
                width: 20px;
                height: 20px;
                background: var(--bg3);
                border-radius: 6px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                font-size: 11px;
            }

            .src-title {
                font-size: 12px;
                font-weight: 500;
                margin: 6px 0 2px;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            .src-url {
                font-size: 10px;
                color: var(--text3);
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }

            .ai-acts {
                display: flex;
                gap: 8px;
                margin: 16px 0 8px;
            }

            .ai-act {
                background: var(--bg2);
                border: none;
                border-radius: 20px;
                padding: 5px 12px;
                font-size: 12px;
                color: var(--text2);
                display: flex;
                align-items: center;
                gap: 6px;
                cursor: pointer;
            }

            .ai-act.active {
                color: var(--accent);
            }

            .related-wrap {
                margin-top: 12px;
            }

            .related-head {
                display: flex;
                align-items: center;
                gap: 8px;
                cursor: pointer;
                color: var(--text3);
                font-size: 12px;
            }

            .related-list {
                margin-top: 8px;
            }

            .rq {
                display: flex;
                align-items: center;
                gap: 10px;
                padding: 8px 12px;
                background: var(--bg2);
                border-radius: 12px;
                margin-bottom: 6px;
                cursor: pointer;
                transition: background 0.15s;
            }

            .rq:hover {
                background: var(--bg3);
            }

            .rq-icon svg {
                width: 14px;
                height: 14px;
                opacity: 0.6;
            }

            .rq-text {
                flex: 1;
                font-size: 13px;
            }

            .typing-wrap {
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 12px;
                color: var(--text3);
            }

            .dots {
                display: flex;
                gap: 4px;
            }

            .dots span {
                width: 6px;
                height: 6px;
                background: var(--accent);
                border-radius: 50%;
                animation: bounce 1.4s infinite;
            }

            .dots span:nth-child(2) {
                animation-delay: 0.2s;
            }

            .dots span:nth-child(3) {
                animation-delay: 0.4s;
            }

            @keyframes bounce {

                0%,
                60%,
                100% {
                    transform: translateY(0);
                }

                30% {
                    transform: translateY(-6px);
                }
            }

            .skel .sl {
                height: 14px;
                background: var(--bg3);
                border-radius: 8px;
                margin: 8px 0;
                animation: pulse 1.5s infinite;
            }

            @keyframes pulse {

                0%,
                100% {
                    opacity: 0.4;
                }

                50% {
                    opacity: 0.8;
                }
            }

            #bottom-bar {
                padding: 16px 20px;
                border-top: 1px solid var(--border);
                background: var(--bg);
            }

            /* Modals */
            .overlay {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.8);
                backdrop-filter: blur(4px);
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 300;
                visibility: hidden;
                opacity: 0;
                transition: 0.2s;
            }

            .overlay.open {
                visibility: visible;
                opacity: 1;
            }

            .modal {
                background: var(--bg2);
                border-radius: 24px;
                padding: 32px;
                width: 420px;
                max-width: 90%;
                position: relative;
            }

            .modal-x {
                position: absolute;
                top: 16px;
                right: 16px;
                background: none;
                border: none;
                cursor: pointer;
            }

            .modal-logo {
                display: flex;
                align-items: center;
                gap: 8px;
                justify-content: center;
                margin-bottom: 20px;
            }

            .modal-h {
                font-size: 24px;
                font-weight: 600;
                text-align: center;
                margin-bottom: 8px;
            }

            .modal-p {
                text-align: center;
                color: var(--text3);
                margin-bottom: 24px;
            }

            .g-btn {
                width: 100%;
                padding: 12px;
                background: var(--bg3);
                border: 1px solid var(--border);
                border-radius: 40px;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
                cursor: pointer;
            }

            .divider {
                text-align: center;
                margin: 20px 0;
                color: var(--text3);
                font-size: 12px;
            }

            .m-input {
                width: 100%;
                padding: 12px;
                background: var(--bg3);
                border: 1px solid var(--border);
                border-radius: 12px;
                margin-bottom: 12px;
                color: var(--text);
            }

            .m-btn {
                width: 100%;
                padding: 12px;
                background: var(--accent);
                border: none;
                border-radius: 40px;
                color: white;
                font-weight: 600;
                cursor: pointer;
            }

            .m-foot {
                text-align: center;
                margin-top: 20px;
                font-size: 12px;
                color: var(--text3);
            }

            #settings {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.8);
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 300;
                visibility: hidden;
                opacity: 0;
                transition: 0.2s;
            }

            #settings.open {
                visibility: visible;
                opacity: 1;
            }

            .settings-sheet {
                background: var(--bg2);
                border-radius: 24px;
                width: 500px;
                max-width: 90%;
                max-height: 80vh;
                overflow-y: auto;
                padding: 24px;
            }

            .s-title {
                font-size: 20px;
                font-weight: 600;
                margin-bottom: 20px;
            }

            .s-grp {
                margin-bottom: 24px;
            }

            .s-grp-lbl {
                font-size: 12px;
                font-weight: 600;
                color: var(--accent);
                margin-bottom: 12px;
                text-transform: uppercase;
            }

            .s-row {
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 10px 0;
                border-bottom: 1px solid var(--border);
            }

            .s-row-lbl {
                font-size: 14px;
                font-weight: 500;
            }

            .s-row-desc {
                font-size: 11px;
                color: var(--text3);
            }

            .tog {
                width: 40px;
                height: 22px;
                background: var(--bg3);
                border-radius: 30px;
                cursor: pointer;
                transition: 0.2s;
                position: relative;
            }

            .tog::after {
                content: '';
                width: 18px;
                height: 18px;
                background: var(--text3);
                border-radius: 50%;
                position: absolute;
                top: 2px;
                left: 3px;
                transition: 0.2s;
            }

            .tog.on {
                background: var(--accent);
            }

            .tog.on::after {
                left: 19px;
                background: white;
            }

            .s-sel {
                background: var(--bg3);
                border: 1px solid var(--border);
                border-radius: 8px;
                padding: 6px 10px;
                color: var(--text);
            }

            .danger-btn {
                background: transparent;
                border: 1px solid var(--danger);
                border-radius: 8px;
                padding: 6px 12px;
                color: var(--danger);
                cursor: pointer;
            }

            #toast {
                position: fixed;
                bottom: 20px;
                left: 50%;
                transform: translateX(-50%);
                background: var(--bg3);
                border: 1px solid var(--border);
                border-radius: 40px;
                padding: 8px 20px;
                font-size: 13px;
                z-index: 400;
                opacity: 0;
                transition: 0.2s;
                pointer-events: none;
            }

            #toast.show {
                opacity: 1;
            }

            .mob-overlay {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.5);
                z-index: 90;
                display: none;
            }

            .mob-overlay.show {
                display: block;
            }

            .upload-area {
                border: 2px dashed var(--border);
                border-radius: 20px;
                padding: 30px;
                text-align: center;
                margin-bottom: 20px;
                cursor: pointer;
                transition: all 0.2s;
            }

            .upload-area:hover {
                border-color: var(--accent);
                background: rgba(32, 128, 141, 0.05);
            }

            .pdf-status {
                background: var(--bg2);
                border-radius: 12px;
                padding: 12px;
                margin-bottom: 20px;
                font-size: 13px;
            }

            .api-key-input {
                display: flex;
                gap: 8px;
                margin-bottom: 16px;
            }

            .api-key-input input {
                flex: 1;
                background: var(--bg3);
                border: 1px solid var(--border);
                border-radius: 12px;
                padding: 10px;
                color: var(--text);
            }
        </style>
    </head>

    <body>

        <div id="sidebar">
            <div class="sb-head">
                <div class="logo-wrap">
                    <div class="logo-icon"><svg viewBox="0 0 24 24">
                            <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
                        </svg></div>
                    <div class="logo-txt">PDF RAG</div>
                </div>
            </div>

            <button class="sb-new" id="new-chat-btn">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <line x1="12" y1="5" x2="12" y2="19" />
                    <line x1="5" y1="12" x2="19" y2="12" />
                </svg>
                New Thread
            </button>

            <div class="sb-scroll">
                <div class="sb-sec">Today</div>
                <div id="hist-today"></div>
                <div class="sb-sec" id="hist-older-label" style="display:none">Earlier</div>
                <div id="hist-older"></div>
            </div>

            <div class="sb-footer">
                <button class="sb-fbtn" onclick="openSettings()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <circle cx="12" cy="12" r="3" />
                        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                    </svg>
                    Settings
                </button>
                <button class="u-btn" onclick="openLogin()">
                    <div class="u-av" id="u-av">G</div>
                    <div class="u-info">
                        <div class="u-name" id="u-name">Guest User</div>
                        <div class="u-plan" id="u-plan">Free · 5 queries/day</div>
                    </div>
                </button>
            </div>
        </div>
        <div class="mob-overlay" id="mob-overlay" onclick="closeSidebar()"></div>

        <div id="main">
            <div id="navbar">
                <button class="nb-toggle" onclick="toggleSidebar()">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="3" y1="6" x2="21" y2="6" />
                        <line x1="3" y1="12" x2="21" y2="12" />
                        <line x1="3" y1="18" x2="21" y2="18" />
                    </svg>
                </button>
                <div class="nb-title" id="nb-title">New Thread</div>
                <div class="nb-actions">
                    <div class="model-selector" id="model-selector" onclick="toggleModelDrop()">
                        <div class="model-dot" id="model-dot"></div>
                        <span class="model-name" id="model-name-display">Sonar Pro</span>
                        <svg class="model-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                            <polyline points="6 9 12 15 18 9" />
                        </svg>
                        <div class="model-dropdown" id="model-dropdown">
                            <div class="md-header">Select Model</div>
                            <div class="md-item selected" data-model="sonar-pro" onclick="selectModel(this)">
                                <div class="md-icon" style="background:rgba(32,128,141,.15)">🔍</div>
                                <div class="md-info">
                                    <div class="md-label">Sonar Pro</div>
                                    <div class="md-desc">Best accuracy · Real-time web</div>
                                </div>
                                <span class="md-badge">Default</span>
                            </div>
                            <div class="md-item" data-model="sonar" onclick="selectModel(this)">
                                <div class="md-icon" style="background:rgba(32,128,141,.1)">⚡</div>
                                <div class="md-info">
                                    <div class="md-label">Sonar</div>
                                    <div class="md-desc">Fast · Everyday queries</div>
                                </div>
                            </div>
                            <div class="md-sep"></div>
                            <div class="md-header">Research</div>
                            <div class="md-item" data-model="deep-research" onclick="selectModel(this)">
                                <div class="md-icon" style="background:rgba(168,85,247,.15)">🧠</div>
                                <div class="md-info">
                                    <div class="md-label">Deep Research</div>
                                    <div class="md-desc">Multi-step · Long reports</div>
                                </div>
                            </div>
                        </div>
                    </div>
                    <button class="nb-btn" id="share-btn" style="display:none" onclick="shareThread()">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <circle cx="18" cy="5" r="3" />
                            <circle cx="6" cy="12" r="3" />
                            <circle cx="18" cy="19" r="3" />
                            <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
                            <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
                        </svg>
                        Share
                    </button>
                    <button class="nb-icon" title="Sign in" onclick="openLogin()">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                            <circle cx="12" cy="7" r="4" />
                        </svg>
                    </button>
                </div>
            </div>

            <div id="chat-area">
                <div id="home">
                    <div class="home-hero">
                        <div class="home-icon"><svg viewBox="0 0 24 24">
                                <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
                            </svg></div>
                        <div class="home-title">Ask <em>anything</em> from your PDFs</div>
                        <div class="home-sub">Upload a document and start asking questions. RAG-powered answers with citations.</div>
                    </div>

                    <!-- PDF Upload Status -->
                    <div class="pdf-status" id="pdf-status">
                        <div id="upload-status">
                            <?php if ($isReady && $pdfName): ?>
                                ✅ Indexed: <strong><?= htmlspecialchars($pdfName) ?></strong> (<?= $chunkCount ?> chunks)
                            <?php else: ?>
                                ⚠️ No PDF loaded. Upload a document to start chatting.
                            <?php endif; ?>
                        </div>
                        <div class="upload-area" onclick="document.getElementById('pdf-input').click()">
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                                <polyline points="17 8 12 3 7 8" />
                                <line x1="12" y1="3" x2="12" y2="15" />
                            </svg>
                            <p>Click to upload PDF</p>
                            <small style="color:var(--text3)">.pdf up to 50MB</small>
                        </div>
                        <input type="file" id="pdf-input" accept="application/pdf" style="display:none" onchange="uploadPDF(this)">
                        <div class="api-key-input" style="margin-top: 12px;">
                            <input type="text" id="groq-key" placeholder="Groq API Key (optional)">
                            <button class="sb-fbtn" style="margin:0; width:auto" onclick="setGroqKey()">Set Key</button>
                        </div>
                    </div>

                    <div class="home-input">
                        <div class="input-box" id="home-input-box">
                            <div class="ib-top">
                                <textarea class="qi" id="home-qi" placeholder="Ask something about your PDF..." rows="1"></textarea>
                            </div>
                            <div class="ib-bot">
                                <button class="ib-act" onclick="toggleFocus(this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <circle cx="11" cy="11" r="8" />
                                        <line x1="21" y1="21" x2="16.65" y2="16.65" />
                                    </svg>Focus</button>
                                <button class="ib-act" id="web-btn" onclick="toggleWeb(this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <circle cx="12" cy="12" r="10" />
                                        <line x1="2" y1="12" x2="22" y2="12" />
                                        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
                                    </svg>Web</button>
                                <div class="ib-spacer"></div>
                                <button class="send-btn" id="home-send" onclick="handleHomeSend()"><svg viewBox="0 0 24 24">
                                        <path d="M22 2L11 13M22 2L15 22 11 13 2 9l20-7z" />
                                    </svg></button>
                            </div>
                        </div>
                    </div>

                    <div class="chips">
                        <button class="chip" onclick="setQ('What is the main topic of this document?')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
                            </svg>Main topic</button>
                        <button class="chip" onclick="setQ('Summarize the key points from this PDF')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M4 4h16v16H4z" />
                                <line x1="8" y1="9" x2="16" y2="9" />
                                <line x1="8" y1="13" x2="12" y2="13" />
                            </svg>Summarize</button>
                        <button class="chip" onclick="setQ('What conclusions does the author draw?')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
                            </svg>Conclusions</button>
                    </div>
                </div>

                <div id="chat-view" style="display:none">
                    <div class="chat-wrap" id="chat-wrap"></div>
                </div>
            </div>

            <div id="bottom-bar" style="display:none">
                <div class="input-box" id="chat-input-box">
                    <div class="ib-top">
                        <textarea class="qi" id="chat-qi" placeholder="Ask a follow-up..." rows="1"></textarea>
                    </div>
                    <div class="ib-bot">
                        <button class="ib-act" onclick="toggleFocus(this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <circle cx="11" cy="11" r="8" />
                                <line x1="21" y1="21" x2="16.65" y2="16.65" />
                            </svg>Focus</button>
                        <button class="ib-act" onclick="toggleWeb(this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                <circle cx="12" cy="12" r="10" />
                                <line x1="2" y1="12" x2="22" y2="12" />
                                <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
                            </svg>Web</button>
                        <div class="ib-spacer"></div>
                        <span style="font-size:10.5px;color:var(--text4);margin-right:4px" id="char-count"></span>
                        <button class="send-btn" id="chat-send" onclick="handleChatSend()"><svg viewBox="0 0 24 24">
                                <path d="M22 2L11 13M22 2L15 22 11 13 2 9l20-7z" />
                            </svg></button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Modals -->
        <div class="overlay" id="login-modal" onclick="if(event.target===this)closeLogin()">
            <div class="modal">
                <button class="modal-x" onclick="closeLogin()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="18" y1="6" x2="6" y2="18" />
                        <line x1="6" y1="6" x2="18" y2="18" />
                    </svg></button>
                <div class="modal-logo">
                    <div class="logo-icon"><svg viewBox="0 0 24 24">
                            <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
                        </svg></div>
                    <div class="logo-txt">PDF RAG</div>
                </div>
                <div class="modal-h">Welcome back</div>
                <p class="modal-p">Sign in to sync your threads and access Pro models.</p>
                <button class="g-btn" onclick="loginGuest()">
                    <svg viewBox="0 0 24 24" width="17" height="17">
                        <circle cx="12" cy="8" r="4" fill="#20808d" />
                        <path d="M5 20v-2a7 7 0 0 1 14 0v2" fill="#20808d" />
                    </svg>
                    Continue as Guest
                </button>
                <div class="divider"><span>or continue with email</span></div>
                <input class="m-input" type="email" placeholder="Email address" id="m-email">
                <button class="m-btn" onclick="loginEmail()">Continue</button>
                <div class="m-foot"><a href="#">Terms</a> · <a href="#">Privacy</a></div>
            </div>
        </div>

        <div id="settings" onclick="if(event.target===this)closeSettings()">
            <div class="settings-sheet">
                <div class="s-title">Settings</div>
                <div class="s-grp">
                    <div class="s-grp-lbl">Appearance</div>
                    <div class="s-row">
                        <div class="s-row-info">
                            <div class="s-row-lbl">Dark mode</div>
                            <div class="s-row-desc">Always use dark theme</div>
                        </div>
                        <div class="tog on" onclick="this.classList.toggle('on')"></div>
                    </div>
                    <div class="s-row">
                        <div class="s-row-info">
                            <div class="s-row-lbl">Compact density</div>
                            <div class="s-row-desc">Reduce spacing between messages</div>
                        </div>
                        <div class="tog" onclick="this.classList.toggle('on')"></div>
                    </div>
                </div>
                <div class="s-grp">
                    <div class="s-grp-lbl">AI & Search</div>
                    <div class="s-row">
                        <div class="s-row-info">
                            <div class="s-row-lbl">Streaming responses</div>
                            <div class="s-row-desc">Show text word by word</div>
                        </div>
                        <div class="tog on" id="stream-tog" onclick="this.classList.toggle('on')"></div>
                    </div>
                    <div class="s-row">
                        <div class="s-row-info">
                            <div class="s-row-lbl">Show sources</div>
                            <div class="s-row-desc">Display reference cards</div>
                        </div>
                        <div class="tog on" id="src-tog" onclick="this.classList.toggle('on')"></div>
                    </div>
                </div>
                <div class="s-grp">
                    <div class="s-grp-lbl">Privacy & Data</div>
                    <div class="s-row">
                        <div class="s-row-info">
                            <div class="s-row-lbl">Clear all history</div>
                            <div class="s-row-desc">Delete all saved threads</div>
                        </div><button class="danger-btn" onclick="clearHistory()">Clear all</button>
                    </div>
                    <div class="s-row">
                        <div class="s-row-info">
                            <div class="s-row-lbl">Reset PDF index</div>
                            <div class="s-row-desc">Remove all indexed documents</div>
                        </div><button class="danger-btn" onclick="resetIndex()">Reset Index</button>
                    </div>
                </div>
            </div>
        </div>

        <div id="toast"></div>

        <script>
            // ═══════════════════════ STATE ══════════════════════════════════
            let state = {
                chats: JSON.parse(localStorage.getItem('pdf_chats') || '[]'),
                current: null,
                streaming: false,
                streamSource: null,
                model: {
                    id: 'sonar-pro',
                    name: 'Sonar Pro',
                    dot: '#20808d'
                },
                focusOn: false,
                webOn: true,
                loggedIn: false,
                user: {
                    name: 'Guest User',
                    initial: 'G',
                    plan: 'Free · 5 queries/day'
                },
                pdfReady: <?= $isReady ? 'true' : 'false' ?>,
                pdfName: '<?= addslashes($pdfName) ?>'
            };

            // ═══════════════════════ INIT ════════════════════════════════════
            document.addEventListener('DOMContentLoaded', () => {
                setupTA('home-qi', handleHomeSend);
                setupTA('chat-qi', handleChatSend);
                renderHistory();
                if (window.innerWidth <= 768) document.getElementById('sidebar').classList.add('collapsed');
                document.getElementById('chat-qi').addEventListener('input', updateCharCount);

                // Check API status periodically
                checkStatus();
                setInterval(checkStatus, 30000);
            });

            function setupTA(id, onEnter) {
                const ta = document.getElementById(id);
                if (!ta) return;
                ta.addEventListener('input', () => growTA(ta));
                ta.addEventListener('keydown', e => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        onEnter();
                    }
                });
            }

            function growTA(ta) {
                ta.style.height = 'auto';
                ta.style.height = Math.min(ta.scrollHeight, 200) + 'px';
            }

            function updateCharCount() {
                const ta = document.getElementById('chat-qi');
                const n = ta.value.length;
                const el = document.getElementById('char-count');
                el.textContent = n > 100 ? n : '';
            }

            function checkStatus() {
                fetch('?action=status')
                    .then(r => r.json())
                    .then(res => {
                        if (res.success) {
                            state.pdfReady = res.data.ready;
                            state.pdfName = res.data.pdf_name || '';
                            const statusDiv = document.getElementById('upload-status');
                            if (statusDiv) {
                                if (state.pdfReady && state.pdfName) {
                                    statusDiv.innerHTML = `✅ Indexed: <strong>${escapeHtml(state.pdfName)}</strong> (${res.data.chunk_count} chunks)`;
                                } else {
                                    statusDiv.innerHTML = `⚠️ No PDF loaded. Upload a document to start chatting.`;
                                }
                            }
                        }
                    })
                    .catch(console.error);
            }

            // ═══════════════════════ SIDEBAR ════════════════════════════════
            function toggleSidebar() {
                const s = document.getElementById('sidebar');
                const o = document.getElementById('mob-overlay');
                if (window.innerWidth <= 768) {
                    s.classList.toggle('mob-open');
                    o.classList.toggle('show');
                } else {
                    s.classList.toggle('collapsed');
                }
            }

            function closeSidebar() {
                document.getElementById('sidebar')?.classList.remove('mob-open');
                document.getElementById('mob-overlay')?.classList.remove('show');
            }

            // ═══════════════════════ HISTORY ════════════════════════════════
            function renderHistory() {
                const today = state.chats.filter(c => isToday(c.ts));
                const older = state.chats.filter(c => !isToday(c.ts));
                document.getElementById('hist-today').innerHTML = today.map(histHTML).join('');
                document.getElementById('hist-older').innerHTML = older.map(histHTML).join('');
                document.getElementById('hist-older-label').style.display = older.length ? '' : 'none';
            }

            function histHTML(c) {
                const active = state.current?.id === c.id ? 'active' : '';
                return `<div class="hi ${active}" onclick="loadChat('${c.id}')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        <span>${escapeHtml(c.title)}</span>
        <button class="hd" onclick="delChat('${c.id}',event)" title="Delete">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/></svg>
        </button>
    </div>`;
            }

            function isToday(ts) {
                return new Date(ts).toDateString() === new Date().toDateString();
            }

            function delChat(id, e) {
                e.stopPropagation();
                state.chats = state.chats.filter(c => c.id !== id);
                saveChats();
                if (state.current?.id === id) newChat();
                else renderHistory();
            }

            function clearHistory() {
                if (!confirm('Delete all chat history?')) return;
                state.chats = [];
                saveChats();
                newChat();
                closeSettings();
                toast('History cleared');
            }

            function saveChats() {
                localStorage.setItem('pdf_chats', JSON.stringify(state.chats));
                renderHistory();
            }

            // ═══════════════════════ CHAT MGMT ══════════════════════════════
            function newChat() {
                if (state.streaming) stopStream();
                state.current = null;
                document.getElementById('home').style.display = '';
                document.getElementById('chat-view').style.display = 'none';
                document.getElementById('bottom-bar').style.display = 'none';
                document.getElementById('share-btn').style.display = 'none';
                document.getElementById('nb-title').textContent = 'New Thread';
                document.getElementById('home-qi').value = '';
                growTA(document.getElementById('home-qi'));
                renderHistory();
                closeSidebar();
            }
            document.getElementById('new-chat-btn').addEventListener('click', newChat);

            function loadChat(id) {
                const chat = state.chats.find(c => c.id === id);
                if (!chat) return;
                if (state.streaming) stopStream();
                state.current = chat;
                showChatUI();
                const wrap = document.getElementById('chat-wrap');
                wrap.innerHTML = '';
                chat.messages.forEach(m => {
                    if (m.role === 'user') wrap.appendChild(mkUserBubble(m.content));
                    else wrap.appendChild(mkAiBubble(m.content, m.sources || [], m.related || [], m.model || 'Sonar Pro'));
                });
                document.getElementById('nb-title').textContent = chat.title;
                renderHistory();
                scrollBot();
                setTimeout(() => document.getElementById('chat-qi')?.focus(), 100);
            }

            function showChatUI() {
                document.getElementById('home').style.display = 'none';
                document.getElementById('chat-view').style.display = 'block';
                document.getElementById('bottom-bar').style.display = 'block';
                document.getElementById('share-btn').style.display = 'flex';
            }

            // ═══════════════════════ PDF UPLOAD ═════════════════════════════
            async function uploadPDF(input) {
                const file = input.files[0];
                if (!file) return;
                if (!file.name.endsWith('.pdf')) {
                    toast('Please upload a PDF file');
                    return;
                }
                const formData = new FormData();
                formData.append('action', 'upload');
                formData.append('file', file);

                toast('Uploading and indexing PDF...');
                try {
                    const res = await fetch('', {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) {
                        toast(`Indexed ${data.data.pdf} (${data.data.chunks} chunks)`);
                        state.pdfReady = true;
                        state.pdfName = data.data.pdf;
                        document.getElementById('upload-status').innerHTML = `✅ Indexed: <strong>${escapeHtml(state.pdfName)}</strong> (${data.data.chunks} chunks)`;
                    } else {
                        toast('Upload failed: ' + (data.error || 'Unknown error'));
                    }
                } catch (e) {
                    toast('Upload error: ' + e.message);
                }
                input.value = '';
            }
            async function setGroqKey() {
                const key = document.getElementById('groq-key').value.trim();
                if (!key) {
                    toast('Enter API key');
                    return;
                }
                const formData = new FormData();
                formData.append('action', 'config');
                formData.append('groq_api_key', key);
                try {
                    const res = await fetch('', {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) toast('API key configured');
                    else toast('Failed: ' + data.error);
                } catch (e) {
                    toast('Error: ' + e.message);
                }
            }
            async function resetIndex() {
                if (!confirm('Delete all indexed PDF chunks?')) return;
                const formData = new FormData();
                formData.append('action', 'reset');
                try {
                    const res = await fetch('', {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) {
                        toast('Index reset');
                        state.pdfReady = false;
                        state.pdfName = '';
                        document.getElementById('upload-status').innerHTML = '⚠️ No PDF loaded. Upload a document to start chatting.';
                    } else toast('Reset failed');
                } catch (e) {
                    toast('Error: ' + e.message);
                }
            }

            // ═══════════════════════ SEND ════════════════════════════════════
            function handleHomeSend() {
                const ta = document.getElementById('home-qi');
                const text = ta.value.trim();
                if (!text || state.streaming) return;
                if (!state.pdfReady && !confirm('No PDF loaded. Continue without document context?')) return;
                ta.value = '';
                growTA(ta);
                beginChat(text);
            }

            function handleChatSend() {
                if (state.streaming) {
                    stopStream();
                    return;
                }
                const ta = document.getElementById('chat-qi');
                const text = ta.value.trim();
                if (!text) return;
                ta.value = '';
                growTA(ta);
                updateCharCount();
                addTurn(text);
            }

            function setQ(text) {
                document.getElementById('home-qi').value = text;
                growTA(document.getElementById('home-qi'));
                document.getElementById('home-qi').focus();
            }
            async function beginChat(text) {
                state.current = {
                    id: Date.now().toString(),
                    title: text.slice(0, 52) + (text.length > 52 ? '…' : ''),
                    ts: Date.now(),
                    messages: []
                };
                state.chats.unshift(state.current);
                saveChats();
                showChatUI();
                addTurn(text);
            }
            async function addTurn(text) {
                if (state.streaming) return;
                state.streaming = true;
                updateSendBtns(true);

                const wrap = document.getElementById('chat-wrap');
                state.current.messages.push({
                    role: 'user',
                    content: text
                });
                wrap.appendChild(mkUserBubble(text));
                scrollBot();

                // Typing indicator
                const aiGroup = document.createElement('div');
                aiGroup.className = 'msg-group';
                aiGroup.innerHTML = `<div class="ai-msg">
        <div class="ai-head"><div class="ai-av"><svg viewBox="0 0 24 24" style="width:13px;height:13px;fill:white"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg></div>PDF Assistant<span class="model-tag">${state.model.name}</span></div>
        <div class="typing-wrap"><div class="dots"><span></span><span></span><span></span></div><span>Analyzing document...</span></div>
    </div>`;
                wrap.appendChild(aiGroup);
                scrollBot();

                // Send to API with streaming
                const formData = new FormData();
                formData.append('action', 'chat_stream');
                formData.append('question', text);

                const useStreaming = document.getElementById('stream-tog')?.classList.contains('on') !== false;

                if (useStreaming) {
                    const eventSource = new EventSource('?action=chat_stream&question=' + encodeURIComponent(text));
                    // SSE not ideal with POST, use fetch streaming instead
                    await streamViaFetch(text, aiGroup);
                } else {
                    await nonStreamChat(text, aiGroup);
                }
            }
            async function streamViaFetch(question, aiGroup) {
                try {
                    const res = await fetch('', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded'
                        },
                        body: 'action=chat_stream&question=' + encodeURIComponent(question)
                    });
                    const reader = res.body.getReader();
                    const decoder = new TextDecoder();
                    let answer = '';
                    let sources = [];
                    let pages = [];

                    // Replace typing with skeleton
                    aiGroup.querySelector('.ai-msg').innerHTML = `
            <div class="ai-head"><div class="ai-av"><svg viewBox="0 0 24 24" style="width:13px;height:13px;fill:white"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg></div>PDF Assistant<span class="model-tag">${state.model.name}</span></div>
            <div class="ai-body streaming"></div>
            <div class="ai-acts"></div>
        `;
                    const bodyDiv = aiGroup.querySelector('.ai-body');
                    scrollBot();

                    while (true) {
                        const {
                            done,
                            value
                        } = await reader.read();
                        if (done) break;
                        const chunk = decoder.decode(value);
                        const lines = chunk.split('\n');
                        for (const line of lines) {
                            if (line.startsWith('data: ')) {
                                try {
                                    const data = JSON.parse(line.slice(6));
                                    if (data.type === 'token') {
                                        answer += data.content;
                                        bodyDiv.innerHTML = md2html(answer);
                                        scrollBot();
                                    } else if (data.type === 'done') {
                                        pages = data.pages || [];
                                        // Show sources mockup
                                        const srcHtml = `<div class="src-grid"><div class="src-card"><div class="src-num">1</div><div class="src-title">PDF Document</div><div class="src-url">Pages: ${pages.join(', ')}</div></div></div>`;
                                        aiGroup.querySelector('.ai-head').insertAdjacentHTML('afterend', srcHtml);
                                        // Actions
                                        const actsDiv = aiGroup.querySelector('.ai-acts');
                                        actsDiv.innerHTML = mkActionsHTML(answer);
                                        // Related suggestions
                                        loadSuggestions(aiGroup, answer);
                                    }
                                } catch (e) {}
                            }
                        }
                    }

                    // Save
                    state.current.messages.push({
                        role: 'assistant',
                        content: answer,
                        sources: [],
                        related: [],
                        model: state.model.name
                    });
                    state.current.ts = Date.now();
                    saveChats();
                    document.getElementById('nb-title').textContent = state.current.title;
                } catch (e) {
                    aiGroup.querySelector('.ai-msg').innerHTML += `<div style="color:var(--danger)">Error: ${e.message}</div>`;
                } finally {
                    state.streaming = false;
                    updateSendBtns(false);
                    scrollBot();
                }
            }
            async function nonStreamChat(question, aiGroup) {
                const formData = new FormData();
                formData.append('action', 'chat');
                formData.append('question', question);
                try {
                    const res = await fetch('', {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success && data.data) {
                        const answer = data.data.answer;
                        aiGroup.querySelector('.ai-msg').innerHTML = `
                <div class="ai-head"><div class="ai-av"><svg viewBox="0 0 24 24" style="width:13px;height:13px;fill:white"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg></div>PDF Assistant<span class="model-tag">${state.model.name}</span></div>
                <div class="src-grid"><div class="src-card"><div class="src-num">1</div><div class="src-title">PDF Document</div><div class="src-url">Pages: ${(data.data.pages || []).join(', ')}</div></div></div>
                <div class="ai-body">${md2html(answer)}</div>
                <div class="ai-acts">${mkActionsHTML(answer)}</div>
            `;
                        state.current.messages.push({
                            role: 'assistant',
                            content: answer,
                            sources: [],
                            related: [],
                            model: state.model.name
                        });
                        saveChats();
                    } else {
                        aiGroup.querySelector('.ai-msg').innerHTML += `<div style="color:var(--danger)">Error: ${data.error || 'Unknown'}</div>`;
                    }
                } catch (e) {
                    aiGroup.querySelector('.ai-msg').innerHTML += `<div style="color:var(--danger)">Error: ${e.message}</div>`;
                } finally {
                    state.streaming = false;
                    updateSendBtns(false);
                    scrollBot();
                }
            }
            async function loadSuggestions(aiGroup, answer) {
                try {
                    const res = await fetch('?action=suggest');
                    const data = await res.json();
                    if (data.success && data.data.suggestions && data.data.suggestions.length) {
                        const relDiv = document.createElement('div');
                        relDiv.innerHTML = mkRelatedHTML(data.data.suggestions);
                        aiGroup.querySelector('.ai-msg').appendChild(relDiv);
                        relDiv.querySelectorAll('.rq').forEach(el => {
                            el.addEventListener('click', () => {
                                document.getElementById('chat-qi').value = el.getAttribute('data-q');
                                growTA(document.getElementById('chat-qi'));
                                handleChatSend();
                            });
                        });
                    }
                } catch (e) {}
            }

            function stopStream() {
                if (state.streamSource) state.streamSource.close();
                state.streaming = false;
                updateSendBtns(false);
            }

            function updateSendBtns(streaming) {
                ['home-send', 'chat-send'].forEach(id => {
                    const btn = document.getElementById(id);
                    if (!btn) return;
                    if (id === 'chat-send') {
                        btn.classList.toggle('stop-mode', streaming);
                        btn.innerHTML = streaming ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>` : `<svg viewBox="0 0 24 24"><path d="M22 2L11 13M22 2L15 22 11 13 2 9l20-7z"/></svg>`;
                    }
                    btn.disabled = streaming && id !== 'chat-send';
                });
            }

            // ═══════════════════════ BUILDERS ════════════════════════════════
            function mkUserBubble(text) {
                const d = document.createElement('div');
                d.className = 'msg-group';
                d.innerHTML = `<div class="u-msg"><div class="u-bubble">${escapeHtml(text)}</div></div>`;
                return d;
            }

            function mkAiBubble(content, sources, related, modelName) {
                const d = document.createElement('div');
                d.className = 'msg-group';
                d.innerHTML = `<div class="ai-msg">
        <div class="ai-head"><div class="ai-av"><svg viewBox="0 0 24 24" style="width:13px;height:13px;fill:white"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg></div>PDF Assistant<span class="model-tag">${escapeHtml(modelName)}</span></div>
        <div class="ai-body">${md2html(content)}</div>
        <div class="ai-acts">${mkActionsHTML(content)}</div>
    </div>`;
                return d;
            }

            function mkSourcesHTML(sources) {
                if (!sources || !sources.length) return '';
                return `<div class="src-grid">${sources.map((s, i) => `<div class="src-card"><div class="src-num">${i+1}</div><div class="src-title">${escapeHtml(s.title || 'Source')}</div><div class="src-url">${escapeHtml(s.url || 'PDF')}</div></div>`).join('')}</div>`;
            }

            function mkRelatedHTML(related) {
                if (!related || !related.length) return '';
                const items = related.map(q => `<div class="rq" data-q="${escapeHtml(q)}"><div class="rq-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><div class="rq-text">${escapeHtml(q)}</div><svg class="rq-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg></div>`).join('');
                return `<div class="related-wrap"><div class="related-head" onclick="toggleRelated(this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>Related<svg class="related-toggle open" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg></div><div class="related-list">${items}</div></div>`;
            }

            function mkActionsHTML(content) {
                return `<button class="ai-act" onclick="copyAct(this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy</button>
            <button class="ai-act" onclick="thumbAct(this,'up')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg></button>
            <button class="ai-act" onclick="shareAct()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>Share</button>`;
            }

            function toggleRelated(head) {
                const tog = head.querySelector('.related-toggle');
                const list = head.nextElementSibling;
                const open = tog.classList.contains('open');
                tog.classList.toggle('open', !open);
                list.style.display = open ? 'none' : '';
            }

            // ═══════════════════════ ACTIONS ════════════════════════════════
            function copyAct(btn) {
                const content = btn.closest('.ai-msg')?.querySelector('.ai-body')?.innerText || '';
                navigator.clipboard.writeText(content).then(() => toast('Copied!'));
            }

            function thumbAct(btn, dir) {
                btn.classList.toggle('active');
                toast(dir === 'up' ? 'Thanks for feedback' : 'Feedback noted');
            }

            function shareAct() {
                shareThread();
            }

            function shareThread() {
                navigator.clipboard.writeText(window.location.href).then(() => toast('Link copied'));
            }

            function toast(msg, d = 2500) {
                const t = document.getElementById('toast');
                t.textContent = msg;
                t.classList.add('show');
                clearTimeout(t._to);
                t._to = setTimeout(() => t.classList.remove('show'), d);
            }

            // ═══════════════════════ TOGGLES ════════════════════════════════
            function toggleFocus(btn) {
                btn.classList.toggle('active');
                toast(btn.classList.contains('active') ? 'Focus mode on' : 'Focus mode off');
            }

            function toggleWeb(btn) {
                btn.classList.toggle('active');
                state.webOn = btn.classList.contains('active');
                toast(state.webOn ? 'Web search on' : 'Web search off');
            }

            function toggleModelDrop() {
                const dd = document.getElementById('model-dropdown');
                dd.classList.toggle('open');
            }

            function closeModelDrop() {
                document.getElementById('model-dropdown').classList.remove('open');
            }

            function selectModel(el) {
                document.querySelectorAll('.md-item').forEach(i => i.classList.remove('selected'));
                el.classList.add('selected');
                const id = el.getAttribute('data-model');
                const names = {
                    'sonar-pro': 'Sonar Pro',
                    'sonar': 'Sonar',
                    'deep-research': 'Deep Research'
                };
                state.model = {
                    id,
                    name: names[id] || 'Sonar Pro',
                    dot: '#20808d'
                };
                document.getElementById('model-name-display').textContent = state.model.name;
                closeModelDrop();
                toast(`Model: ${state.model.name}`);
            }

            // ═══════════════════════ LOGIN ═══════════════════════════════════
            function openLogin() {
                document.getElementById('login-modal').classList.add('open');
            }

            function closeLogin() {
                document.getElementById('login-modal').classList.remove('open');
            }

            function loginGuest() {
                state.loggedIn = true;
                state.user = {
                    name: 'Guest User',
                    initial: 'G',
                    plan: 'Guest'
                };
                applyUser();
                closeLogin();
                toast('Signed in as Guest');
            }

            function loginEmail() {
                const em = document.getElementById('m-email').value || 'guest';
                const name = em.split('@')[0];
                state.loggedIn = true;
                state.user = {
                    name,
                    initial: name[0].toUpperCase(),
                    plan: 'Free'
                };
                applyUser();
                closeLogin();
                toast('Signed in as ' + name);
            }

            function applyUser() {
                document.getElementById('u-av').textContent = state.user.initial;
                document.getElementById('u-name').textContent = state.user.name;
                document.getElementById('u-plan').textContent = state.user.plan;
            }

            function openSettings() {
                document.getElementById('settings').classList.add('open');
            }

            function closeSettings() {
                document.getElementById('settings').classList.remove('open');
            }

            // ═══════════════════════ UTILS ════════════════════════════════════
            function escapeHtml(s) {
                return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
            }

            function scrollBot() {
                const a = document.getElementById('chat-area');
                a.scrollTo({
                    top: a.scrollHeight,
                    behavior: 'smooth'
                });
            }

            // ═══════════════════════ MARKDOWN ════════════════════════════════
            function md2html(md) {
                let h = escapeHtml(md);
                h = h.replace(/```(\w*)\n?([\s\S]*?)```/g, '<pre><code class="language-$1">$2</code></pre>');
                h = h.replace(/`([^`\n]+)`/g, '<code>$1</code>');
                h = h.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
                h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
                h = h.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
                h = h.replace(/^### (.+)$/gm, '<h3>$1</h3>');
                h = h.replace(/^## (.+)$/gm, '<h2>$1</h2>');
                h = h.replace(/^# (.+)$/gm, '<h1>$1</h1>');
                h = h.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');
                h = h.replace(/^(\d+)\. (.+)$/gm, '<li>$2</li>');
                h = h.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ol>$1</ol>');
                h = h.replace(/^[-*+] (.+)$/gm, '<li>$1</li>');
                h = h.replace(/((?:<li>(?!.*<\/ol>).*<\/li>\n?)+)/g, '<ul>$1</ul>');
                h = h.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
                h = h.replace(/\n{2,}/g, '</p><p>');
                h = '<p>' + h + '</p>';
                h = h.replace(/<p><\/p>/g, '');
                return h;
            }
        </script>
    </body>

    </html>
<?php
}
?>