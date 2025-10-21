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

function loadConfigApiUrl(): ?string
{
  $configPath = __DIR__ . '/config.json';
  if (!is_file($configPath)) {
    return null;
  }
  $raw = file_get_contents($configPath);
  if ($raw === false) {
    return null;
  }
  $json = json_decode($raw, true);
  if (!is_array($json)) {
    return null;
  }
  $apiUrl = $json['apiUrl'] ?? null;
  return is_string($apiUrl) ? trim($apiUrl) : null;
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

function uploadZipThroughTool(string $toolUrl, string $shellPretty, string $zipPath, string $pathFragment, string $fileName): array
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
  $json = json_encode($payload);
  if ($json === false) {
    respond(500, ['error' => 'payload_encoding_failed']);
  }
  $ch = curl_init($toolUrl);
  if ($ch === false) {
    respond(500, ['error' => 'curl_init_failed']);
  }
  curl_setopt($ch, CURLOPT_POST, true);
  curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
  curl_setopt($ch, CURLOPT_HTTPHEADER, ['Content-Type: application/json']);
  curl_setopt($ch, CURLOPT_POSTFIELDS, $json);
  $body = curl_exec($ch);
  $err = curl_error($ch);
  $status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
  curl_close($ch);
  if ($body === false) {
    respond(502, ['error' => 'upload_failed', 'detail' => $err ?: 'curl_exec_failed']);
  }
  $decoded = json_decode($body, true);
  if ($status < 200 || $status >= 300) {
    $detail = is_array($decoded) ? $decoded : $body;
    respond($status ?: 502, ['error' => 'upload_failed', 'detail' => $detail]);
  }
  if (!is_array($decoded)) {
    $decoded = ['raw' => $body];
  }
  return $decoded;
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
  $year = $now->format('Y');
  $month = $now->format('m');
  $zipFileName = sprintf('%s_TRANSCRIPT_%s.zip', $shellUpper, $now->format('d-m-Y-H-i'));
  $zipPath = $logsDir . '/' . $zipFileName;

  createZipArchive($zipPath, $logFiles);

  $apiUrl = loadConfigApiUrl();
  if (!$apiUrl) {
    respond(500, ['error' => 'api_url_missing']);
  }
  $toolUrl = deriveToolUrl($apiUrl, '/tools/s3escalator');
  if (!$toolUrl) {
    respond(500, ['error' => 'tool_url_unavailable']);
  }

  $pathFragment = sprintf('dAisys-diary/transcripts/%s/%s/%s', $shellPretty, $year, $month);
  $uploadResponse = uploadZipThroughTool($toolUrl, $shellPretty, $zipPath, $pathFragment, $zipFileName);

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
