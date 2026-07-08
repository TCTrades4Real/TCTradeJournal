<?php
header('Content-Type: application/json');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { exit; }

$SECRET = 'TCJ_VIDEO_TOKEN_2025';
$body   = json_decode(file_get_contents('php://input'), true);

if (($body['token'] ?? '') !== $SECRET) {
    http_response_code(403);
    echo json_encode(['ok' => false, 'error' => 'Forbidden']);
    exit;
}

$file   = __DIR__ . '/trade_videos.json';
$videos = file_exists($file) ? json_decode(file_get_contents($file), true) : [];
if (!is_array($videos)) $videos = [];

$action = $body['action'] ?? '';
$key    = $body['key']    ?? '';

if ($key === '') {
    http_response_code(400);
    echo json_encode(['ok' => false, 'error' => 'Missing key']);
    exit;
}

if ($action === 'set' && !empty($body['url'])) {
    $videos[$key] = $body['url'];
} elseif ($action === 'delete') {
    unset($videos[$key]);
} else {
    http_response_code(400);
    echo json_encode(['ok' => false, 'error' => 'Invalid action']);
    exit;
}

file_put_contents($file, json_encode($videos, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
echo json_encode(['ok' => true]);
