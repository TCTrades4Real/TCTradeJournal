<?php
declare(strict_types=1);
header('Content-Type: application/json');

// Same values as dashboard/auth.js — kept in sync manually since this runs
// server-side and can't import the JS file.
const CLIENT_ID      = '413954462899-jcup1smhjuh6londv3tcsmil17cog1vv.apps.googleusercontent.com';
const ALLOWED_EMAIL  = 'tctrades4real@gmail.com';

const DATA_DIR        = __DIR__ . '/../watchlist_setups';
const DATA_FILE       = DATA_DIR . '/data.json';
const KEY_FILE        = __DIR__ . '/_write_key.php';
const MAX_BODY_BYTES  = 5 * 1024 * 1024;

function respond(int $status, array $body) {
    http_response_code($status);
    echo json_encode($body);
    exit;
}

// KEY_FILE is a plain .php file that just returns the key string. Hitting it
// directly over HTTP executes the `return` and prints nothing, so the key
// never leaks even though it lives inside the public webroot.
function loadWriteKey() {
    if (!is_file(KEY_FILE)) return null;
    $val = include KEY_FILE;
    return (is_string($val) && $val !== '') ? $val : null;
}

function issueWriteKey(): string {
    $existing = loadWriteKey();
    if ($existing !== null) return $existing;
    $key = bin2hex(random_bytes(32));
    $php = "<?php\nreturn " . var_export($key, true) . ";\n";
    file_put_contents(KEY_FILE, $php, LOCK_EX);
    return $key;
}

// Returns null on success, or an error string.
function verifyGoogleIdToken(string $idToken) {
    $url  = 'https://oauth2.googleapis.com/tokeninfo?id_token=' . urlencode($idToken);
    $resp = false;
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 8, CURLOPT_SSL_VERIFYPEER => true]);
        $resp = curl_exec($ch);
        curl_close($ch);
    }
    if ($resp === false || $resp === '') {
        $ctx  = stream_context_create(['http' => ['timeout' => 8, 'ignore_errors' => true]]);
        $resp = @file_get_contents($url, false, $ctx);
    }
    if ($resp === false || $resp === '') return 'could not reach Google to verify sign-in';
    $info = json_decode($resp, true);
    if (!is_array($info) || isset($info['error']))    return 'invalid or expired sign-in';
    if (($info['aud'] ?? '') !== CLIENT_ID)            return 'token audience mismatch';
    if (($info['email'] ?? '') !== ALLOWED_EMAIL)      return 'access denied';
    if (($info['email_verified'] ?? '') !== 'true')    return 'email not verified';
    return null;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    respond(405, ['ok' => false, 'error' => 'POST required']);
}

$raw = file_get_contents('php://input');
if ($raw === false || strlen($raw) > MAX_BODY_BYTES) {
    respond(400, ['ok' => false, 'error' => 'invalid or oversized request body']);
}

$payload = json_decode($raw, true);
if (!is_array($payload)) {
    respond(400, ['ok' => false, 'error' => 'malformed JSON body']);
}

$providedKey = is_string($payload['writeKey'] ?? null) ? $payload['writeKey'] : null;
$idToken     = is_string($payload['idToken'] ?? null)  ? $payload['idToken']  : null;
$storedKey   = loadWriteKey();
$authorized  = false;

if ($providedKey !== null && $storedKey !== null && hash_equals($storedKey, $providedKey)) {
    $authorized = true;
} elseif ($idToken !== null) {
    $err = verifyGoogleIdToken($idToken);
    if ($err === null) {
        $authorized = true;
    } else {
        respond(401, ['ok' => false, 'error' => $err]);
    }
}

if (!$authorized) {
    respond(401, ['ok' => false, 'error' => 'not authorized']);
}

$data = $payload['data'] ?? null;
if (!is_array($data)
    || !isset($data['setupTypes']) || !is_array($data['setupTypes'])
    || !isset($data['callouts'])   || !is_array($data['callouts'])) {
    respond(400, ['ok' => false, 'error' => 'payload missing setupTypes/callouts arrays']);
}

if (!is_dir(DATA_DIR)) {
    @mkdir(DATA_DIR, 0775, true);
}

$json = json_encode(
    ['setupTypes' => $data['setupTypes'], 'callouts' => $data['callouts']],
    JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE
);
if ($json === false) {
    respond(500, ['ok' => false, 'error' => 'failed to encode data']);
}

$tmp = DATA_FILE . '.tmp-' . bin2hex(random_bytes(4));
if (file_put_contents($tmp, $json, LOCK_EX) === false) {
    respond(500, ['ok' => false, 'error' => 'failed to write temp file']);
}
if (!rename($tmp, DATA_FILE)) {
    @unlink($tmp);
    respond(500, ['ok' => false, 'error' => 'failed to save data file']);
}

respond(200, ['ok' => true, 'writeKey' => issueWriteKey()]);
