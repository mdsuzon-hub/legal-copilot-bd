<?php
/**
 * RAG System - PHP API Handler
 * All AJAX requests from the frontend go through this file.
 */

session_start();
require_once __DIR__ . '/config.php';

header('Content-Type: application/json');
header('X-Content-Type-Options: nosniff');

// Rate limiting
if (!check_rate_limit()) {
    http_response_code(429);
    echo json_encode(['error' => 'Rate limit exceeded. Please wait a moment.']);
    exit;
}

$action = $_POST['action'] ?? $_GET['action'] ?? '';

switch ($action) {

    // ── Query ────────────────────────────────────────────────────────────────
    case 'query':
        $query = trim($_POST['query'] ?? '');
        $top_k = (int)($_POST['top_k'] ?? 5);

        if (empty($query) || strlen($query) > 2000) {
            http_response_code(400);
            echo json_encode(['error' => 'Invalid query (1-2000 chars required)']);
            exit;
        }

        $result = rag_api('/query', 'POST', [
            'query' => $query,
            'top_k' => min(max($top_k, 1), 20),
        ]);

        http_response_code($result['status'] ?: 500);
        echo json_encode($result['body']);
        break;

    // ── Upload ───────────────────────────────────────────────────────────────
    case 'upload':
        if (empty($_FILES['file'])) {
            http_response_code(400);
            echo json_encode(['error' => 'No file provided']);
            exit;
        }

        $file = $_FILES['file'];
        $ext  = strtolower(pathinfo($file['name'], PATHINFO_EXTENSION));

        if (!in_array($ext, ALLOWED_EXTENSIONS)) {
            http_response_code(400);
            echo json_encode(['error' => 'File type not allowed. Use: ' . implode(', ', ALLOWED_EXTENSIONS)]);
            exit;
        }

        if ($file['size'] > MAX_FILE_SIZE_MB * 1024 * 1024) {
            http_response_code(413);
            echo json_encode(['error' => 'File too large (max ' . MAX_FILE_SIZE_MB . ' MB)']);
            exit;
        }

        if ($file['error'] !== UPLOAD_ERR_OK) {
            http_response_code(500);
            echo json_encode(['error' => 'Upload error: ' . $file['error']]);
            exit;
        }

        $result = rag_api('/upload', 'POST', [], ['file' => $file]);
        http_response_code($result['status'] ?: 500);
        echo json_encode($result['body']);
        break;

    // ── Ingest text ──────────────────────────────────────────────────────────
    case 'ingest_text':
        $text     = trim($_POST['text'] ?? '');
        $filename = trim($_POST['filename'] ?? 'manual_input.txt');

        if (strlen($text) < 20) {
            http_response_code(400);
            echo json_encode(['error' => 'Text too short (minimum 20 characters)']);
            exit;
        }

        // Send as multipart form
        $ch = curl_init(RAG_API_BASE . '/ingest-text');
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST           => true,
            CURLOPT_POSTFIELDS     => ['text' => $text, 'filename' => $filename],
            CURLOPT_TIMEOUT        => API_TIMEOUT,
        ]);
        $response   = curl_exec($ch);
        $httpStatus = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        http_response_code($httpStatus ?: 500);
        echo $response;
        break;

    // ── List documents ───────────────────────────────────────────────────────
    case 'list_documents':
        $result = rag_api('/documents', 'GET');
        http_response_code($result['status'] ?: 500);
        echo json_encode($result['body']);
        break;

    // ── Delete document ──────────────────────────────────────────────────────
    case 'delete_document':
        $doc_id = trim($_POST['doc_id'] ?? '');
        if (empty($doc_id) || !preg_match('/^[a-f0-9\-]{36}$/', $doc_id)) {
            http_response_code(400);
            echo json_encode(['error' => 'Invalid document ID']);
            exit;
        }
        $result = rag_api('/documents/' . urlencode($doc_id), 'DELETE');
        http_response_code($result['status'] ?: 500);
        echo json_encode($result['body']);
        break;

    // ── Stats ────────────────────────────────────────────────────────────────
    case 'stats':
        $result = rag_api('/stats', 'GET');
        http_response_code($result['status'] ?: 500);
        echo json_encode($result['body']);
        break;

    // ── Health ───────────────────────────────────────────────────────────────
    case 'health':
        $result = rag_api('/health', 'GET');
        http_response_code($result['status'] ?: 500);
        echo json_encode($result['body']);
        break;

    default:
        http_response_code(400);
        echo json_encode(['error' => 'Unknown action']);
}
