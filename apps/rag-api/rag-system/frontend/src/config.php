<?php
/**
 * RAG System - PHP Frontend Configuration
 */

// FastAPI backend URL (change in production)
define('RAG_API_BASE', getenv('RAG_API_BASE') ?: 'http://localhost:8000');

// App settings
define('APP_NAME', 'RAG Intelligence');
define('APP_VERSION', '1.0.0');
define('MAX_FILE_SIZE_MB', 10);
define('ALLOWED_EXTENSIONS', ['pdf', 'docx', 'txt', 'md']);

// Security
define('API_TIMEOUT', 60);   // seconds
define('RATE_LIMIT_WINDOW', 60);
define('RATE_LIMIT_MAX', 20);

/**
 * Make a request to the RAG FastAPI backend.
 *
 * @param string $endpoint  e.g. '/query'
 * @param string $method    GET | POST | DELETE
 * @param array  $data      JSON body (for POST)
 * @param array  $files     ['file' => ['name'=>..., 'tmp_name'=>..., 'type'=>...]]
 * @return array ['status' => int, 'body' => mixed]
 */
function rag_api(string $endpoint, string $method = 'GET', array $data = [], array $files = []): array
{
    $url = RAG_API_BASE . $endpoint;
    $ch  = curl_init($url);

    $headers = [];
    $timeout = API_TIMEOUT;

    if (!empty($files)) {
        // multipart/form-data for file upload
        $postFields = [];
        foreach ($data as $k => $v) {
            $postFields[$k] = $v;
        }
        foreach ($files as $field => $file) {
            $postFields[$field] = new CURLFile($file['tmp_name'], $file['type'], $file['name']);
        }
        curl_setopt($ch, CURLOPT_POSTFIELDS, $postFields);
        $timeout = 120;
    } elseif ($method === 'POST') {
        $headers[] = 'Content-Type: application/json';
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
    }

    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_HTTPHEADER     => $headers,
        CURLOPT_TIMEOUT        => $timeout,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_SSL_VERIFYPEER => false, // set true in production with proper certs
    ]);

    $response   = curl_exec($ch);
    $httpStatus = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $curlError  = curl_error($ch);
    curl_close($ch);

    if ($curlError) {
        return ['status' => 0, 'body' => ['error' => 'Connection failed: ' . $curlError]];
    }

    $decoded = json_decode($response, true);
    return ['status' => $httpStatus, 'body' => $decoded ?? ['raw' => $response]];
}

/**
 * Simple rate limiter using PHP sessions.
 */
function check_rate_limit(): bool
{
    if (!isset($_SESSION['rl_window'])) {
        $_SESSION['rl_window'] = time();
        $_SESSION['rl_count']  = 0;
    }
    if (time() - $_SESSION['rl_window'] > RATE_LIMIT_WINDOW) {
        $_SESSION['rl_window'] = time();
        $_SESSION['rl_count']  = 0;
    }
    $_SESSION['rl_count']++;
    return $_SESSION['rl_count'] <= RATE_LIMIT_MAX;
}

/**
 * Sanitize user output for HTML display.
 */
function h(string $str): string
{
    return htmlspecialchars($str, ENT_QUOTES | ENT_HTML5, 'UTF-8');
}
