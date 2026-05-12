<?php
// Secret token — must match SIM_TOKEN in day.html
define('SIM_TOKEN', 'tc_sim_8f3a2d9e1b7c4f06');

header('Content-Type: application/json');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Headers: X-Sim-Token, Content-Type');
header('Access-Control-Allow-Methods: POST, OPTIONS');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { http_response_code(204); exit; }
if ($_SERVER['REQUEST_METHOD'] !== 'POST') { http_response_code(405); echo json_encode(['error' => 'Method not allowed']); exit; }

$token = $_SERVER['HTTP_X_SIM_TOKEN'] ?? '';
if ($token !== SIM_TOKEN) { http_response_code(403); echo json_encode(['error' => 'Forbidden']); exit; }

$body = json_decode(file_get_contents('php://input'), true);
if (!$body || !isset($body['key'])) { http_response_code(400); echo json_encode(['error' => 'Bad request']); exit; }

$file   = __DIR__ . '/sim_data.json';
$backup = __DIR__ . '/sim_data.bak.json';

$fp = fopen($file, 'c+');
if (!$fp) { http_response_code(500); echo json_encode(['error' => 'Cannot open file']); exit; }

flock($fp, LOCK_EX);

$raw  = stream_get_contents($fp);
$data = $raw ? json_decode($raw, true) : [];
if (!is_array($data)) $data = [];

$key = $body['key'];

if (isset($body['reset']) && $body['reset'] === true) {
    // Reset entire day
    unset($data[$key]);
} elseif (isset($body['symbol'])) {
    $sym = $body['symbol'];
    if ($body['value'] === null) {
        // Delete single symbol entry
        unset($data[$key][$sym]);
        if (isset($data[$key]) && empty($data[$key])) unset($data[$key]);
    } else {
        // Upsert value
        if (!isset($data[$key])) $data[$key] = [];
        $data[$key][$sym] = (float)$body['value'];
    }
}

// Rolling backup before write
copy($file, $backup);

ftruncate($fp, 0);
rewind($fp);
fwrite($fp, json_encode($data, JSON_PRETTY_PRINT));
flock($fp, LOCK_UN);
fclose($fp);

echo json_encode(['ok' => true]);
