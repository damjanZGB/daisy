<?php
// public_html/origin/log.php
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: https://origin.daisy.voyage');
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
  http_response_code(204);
  exit;
}

function respond(int $status, array $payload): void
{
  http_response_code($status);
  echo json_encode($payload);
  exit;
}

function deriveToolUrl(string $invokeUrl, string $toolPath): ?string
{
  $trimmed = trim($invokeUrl);
  if ($trimmed === '') {
    return null;
  }
  $base = rtrim($trimmed, '/');
  $suffix = '/invoke';
  if (substr($base, -strlen($suffix)) === $suffix) {
    $base = substr($base, 0, -strlen($suffix));
  }
  return $base . '/' . ltrim($toolPath, '/');
}

function loadConfig(): array
{
  static $cache = null;
  if (is_array($cache)) {
    return $cache;
  }
  $configPath = __DIR__ . '/config.json';
  if (!is_file($configPath)) {
    $cache = [];
    return $cache;
  }
  $raw = file_get_contents($configPath);
  if ($raw === false) {
    $cache = [];
    return $cache;
  }
  $json = json_decode($raw, true);
  $cache = is_array($json) ? $json : [];
  return $cache;
}

function resolveSnitchToken(array $config): ?string
{
  $candidates = [
    $config['snitchUploaderToken'] ?? null,
    $config['transcriptUploaderToken'] ?? null,
    getenv('SNITCH_UPLOAD_TOKEN') ?: null,
    getenv('S3ESCALATOR_TOKEN') ?: null,
    getenv('TRANSCRIPT_UPLOADER_TOKEN') ?: null,
    getenv('UPLOADER_TOKEN') ?: null,
  ];
  foreach ($candidates as $candidate) {
    if (is_string($candidate)) {
      $trimmed = trim($candidate);
      if ($trimmed !== '') {
        return $trimmed;
      }
    }
  }
  return null;
}

function resolveSnitchEndpoints(array $config): array
{
  $candidates = [];
  $configKeys = [
    'snitchUploaderUrl',
    'snitchUploaderURL',
    'transcriptUploaderUrl',
    'transcriptUploaderURL',
    's3EscalatorUrl',
    's3EscalatorURL',
  ];
  foreach ($configKeys as $key) {
    if (!empty($config[$key]) && is_string($config[$key])) {
      $candidates[] = trim($config[$key]);
    }
  }
  $envKeys = [
    'SNITCH_UPLOAD_URL',
    'SNITCH_UPLOADER_URL',
    'S3ESCALATOR_URL',
    'TRANSCRIPT_UPLOADER_URL',
  ];
  foreach ($envKeys as $envKey) {
    $value = getenv($envKey);
    if (is_string($value) && trim($value) !== '') {
      $candidates[] = trim($value);
    }
  }
  $configApi = $config['apiUrl'] ?? null;
  if (is_string($configApi) && trim($configApi) !== '') {
    $derived = deriveToolUrl($configApi, '/tools/s3escalator');
    if ($derived) {
      $candidates[] = $derived;
    }
  }
  $allowLocalFallback = false;
  $configFlag = $config['snitchAllowLocalFallback'] ?? null;
  if (is_bool($configFlag)) {
    $allowLocalFallback = $configFlag;
  } elseif (is_string($configFlag)) {
    $allowLocalFallback = filter_var($configFlag, FILTER_VALIDATE_BOOLEAN, FILTER_NULL_ON_FAILURE) ?? false;
  }
  if (!$allowLocalFallback) {
    $envFlag = getenv('SNITCH_ALLOW_LOCALHOST');
    if ($envFlag !== false) {
      $allowLocalFallback = filter_var($envFlag, FILTER_VALIDATE_BOOLEAN, FILTER_NULL_ON_FAILURE) ?? false;
    }
  }
  $unique = [];
  $seen = [];
  foreach ($candidates as $candidate) {
    $trimmed = trim($candidate);
    if ($trimmed === '') {
      continue;
    }
    if (!preg_match('#^https?://#i', $trimmed)) {
      continue;
    }
    $key = strtolower($trimmed);
    if (isset($seen[$key])) {
      continue;
    }
    $seen[$key] = true;
    $unique[] = $trimmed;
  }
  if (empty($unique) && $allowLocalFallback) {
    $unique[] = 'http://127.0.0.1:8788/tools/s3escalator';
    $unique[] = 'http://localhost:8788/tools/s3escalator';
  }
  return $unique;
}

function ensureLogsDir(): string
{
  $dir = __DIR__ . '/logs';
  if (!is_dir($dir)) {
    mkdir($dir, 0775, true);
  }
  return $dir;
}

function collectLogFiles(string $dir): array
{
  $files = glob($dir . '/*.log');
  if (!is_array($files)) {
    return [];
  }
  return array_values(array_filter($files, fn($f) => is_file($f)));
}

function createZipArchive(string $zipPath, array $sourceFiles): void
{
  if (!class_exists(ZipArchive::class)) {
    respond(500, ['error' => 'zip_extension_missing']);
  }
  $zip = new ZipArchive();
  if ($zip->open($zipPath, ZipArchive::CREATE | ZipArchive::OVERWRITE) !== true) {
    respond(500, ['error' => 'zip_creation_failed']);
  }
  foreach ($sourceFiles as $file) {
    $zip->addFile($file, basename($file));
  }
  $zip->close();
}

function uploadZipThroughTool(array $toolUrls, string $authToken, string $shellPretty, string $zipPath, string $pathFragment, string $fileName, bool $absolutePath = false): array
{
  $contents = file_get_contents($zipPath);
  if ($contents === false) {
    respond(500, ['error' => 'zip_read_failed']);
  }
  $payload = [
    'type' => 'transcript',
    'path' => $pathFragment,
    'sender' => $shellPretty,
    'fileName' => $fileName,
    'filename' => $fileName,
    'contentType' => 'application/zip',
    'fileBase64' => base64_encode($contents),
  ];
  if ($absolutePath) {
    $payload['pathMode'] = 'absolute';
  }
  $json = json_encode($payload);
  if ($json === false) {
    respond(500, ['error' => 'payload_encoding_failed']);
  }
  $attempts = [];
  foreach ($toolUrls as $toolUrl) {
    $ch = curl_init($toolUrl);
    if ($ch === false) {
      $attempts[] = ['status' => 500, 'detail' => 'curl_init_failed', 'url' => $toolUrl];
      continue;
    }
    $headers = ['Content-Type: application/json'];
    if ($authToken !== null && $authToken !== '') {
      $headers[] = 'X-Uploader-Token: ' . $authToken;
    }
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
    curl_setopt($ch, CURLOPT_POSTFIELDS, $json);
    curl_setopt($ch, CURLOPT_TIMEOUT, 20);
    $body = curl_exec($ch);
    $err = curl_error($ch);
    $status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($body === false) {
      $attempts[] = ['status' => 502, 'detail' => $err ?? 'curl_exec_failed', 'url' => $toolUrl];
      continue;
    }
    $decoded = json_decode($body, true);
    if ($status < 200 || $status >= 300) {
      $detail = is_array($decoded) ? $decoded : $body;
      $attempts[] = ['status' => $status ?? 502, 'detail' => $detail, 'url' => $toolUrl];
      continue;
    }
    if (!is_array($decoded)) {
      $decoded = ['raw' => $body];
    }
    $decoded['_target'] = $toolUrl;
    return $decoded;
  }
  if (empty($attempts)) {
    $attempts[] = ['status' => 502, 'detail' => 'no_tool_url', 'url' => null];
  }
  $primary = $attempts[0];
  respond((int)($primary['status'] ?? 502), ['error' => 'upload_failed', 'attempts' => $attempts]);
}

function handleSnitchAction(): void
{
  $shell = basename(__DIR__);
  $shellPretty = ucfirst($shell);
  $shellUpper = strtoupper($shellPretty);

  $logsDir = ensureLogsDir();
  $logFiles = collectLogFiles($logsDir);

  if (empty($logFiles)) {
    respond(200, ['ok' => true, 'result' => 'no_logs']);
  }

  $now = new DateTimeImmutable('now');
  $dateFolder = $now->format('Y-m-d');
  $zipFileName = sprintf('%s_TRANSCRIPT_%s.zip', $shellUpper, $now->format('d-m-Y-H-i'));
  $zipPath = $logsDir . '/' . $zipFileName;

  createZipArchive($zipPath, $logFiles);

  $config = loadConfig();
  $toolUrls = resolveSnitchEndpoints($config);
  if (empty($toolUrls)) {
    respond(500, ['error' => 'snitch_endpoint_missing']);
  }
  $authToken = resolveSnitchToken($config);

  $pathFragment = sprintf('transcripts/%s/%s', strtolower($shellPretty), $dateFolder);
  $uploadResponse = uploadZipThroughTool(
    $toolUrls,
    $authToken ?? '',
    $shellPretty,
    $zipPath,
    $pathFragment,
    $zipFileName,
    true
  );

  foreach ($logFiles as $file) {
    @unlink($file);
  }

  respond(200, ['ok' => true, 'uploaded' => $uploadResponse, 'zip' => '/logs/' . $zipFileName]);
}

$raw = file_get_contents('php://input');
if ($raw === false || $raw === '') {
  respond(400, ['error' => 'empty body']);
}
$in = json_decode($raw, true);
if (!is_array($in)) {
  respond(400, ['error' => 'invalid body']);
}

if (($in['action'] ?? '') === 'snitch') {
  handleSnitchAction();
}

$filename = $in['filename'] ?? '';
$chunk = (string)($in['chunk'] ?? '');
$locationLabel = isset($in['locationLabel']) ? trim((string)$in['locationLabel']) : '';
$timeZone = isset($in['timeZone']) ? trim((string)$in['timeZone']) : '';
$inferredOrigin = isset($in['inferredOrigin']) ? trim((string)$in['inferredOrigin']) : '';

if (!preg_match('/^LH\d{4}\.log$/', $filename)) {
  respond(400, ['error' => 'bad filename']);
}

$dir = ensureLogsDir();
$path = $dir . '/' . $filename;
$needsHeaderAugment = !file_exists($path) || filesize($path) === 0;

if ($needsHeaderAugment && ($locationLabel !== '' || $timeZone !== '' || $inferredOrigin !== '')) {
  $locationLines = [];
  if ($locationLabel !== '') { $locationLines[] = 'LOCATION: ' . $locationLabel; }
  if ($timeZone !== '') { $locationLines[] = 'TIMEZONE: ' . $timeZone; }
  if ($inferredOrigin !== '') { $locationLines[] = 'DEFAULT ORIGIN: ' . $inferredOrigin; }
  if ($locationLines && strpos($chunk, '---') !== false) {
    $locationBlock = implode(PHP_EOL, $locationLines);
    $chunk = preg_replace('/---/', $locationBlock . PHP_EOL . '---', $chunk, 1);
  }
}

if (function_exists('mb_convert_encoding')) {
  if (!mb_detect_encoding($chunk, 'UTF-8', true)) {
    $chunk = mb_convert_encoding($chunk, 'UTF-8', 'UTF-8,ISO-8859-1');
  }
}
$chunk = str_replace("\r\n", "\n", $chunk);

$fh = fopen($path, 'ab');
if (!$fh) {
  respond(500, ['error' => 'open failed']);
}
flock($fh, LOCK_EX);
fwrite($fh, $chunk);
flock($fh, LOCK_UN);
fclose($fh);

respond(200, ['ok' => true, 'file' => '/logs/' . $filename]);
