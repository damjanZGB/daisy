<?php
// public_html/origin/log.php
declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: https://origin.daisy.voyage');
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { http_response_code(204); exit; }

$raw = file_get_contents('php://input');
if (!$raw) { http_response_code(400); echo json_encode(['error' => 'empty body']); exit; }
$in = json_decode($raw, true);
if (!is_array($in)) { http_response_code(400); echo json_encode(['error' => 'invalid body']); exit; }

$filename = $in['filename'] ?? '';
$chunk = (string)($in['chunk'] ?? '');
$locationLabel = isset($in['locationLabel']) ? trim((string)$in['locationLabel']) : '';
$timeZone = isset($in['timeZone']) ? trim((string)$in['timeZone']) : '';
$inferredOrigin = isset($in['inferredOrigin']) ? trim((string)$in['inferredOrigin']) : '';

if (!preg_match('/^LH\d{4}\.log$/', $filename)) {
  http_response_code(400); echo json_encode(['error' => 'bad filename']); exit;
}

$dir = __DIR__ . '/logs';
if (!is_dir($dir)) { mkdir($dir, 0775, true); }

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
$chunk = str_replace("
", "
", $chunk);

$fh = fopen($path, 'ab');
if (!$fh) { http_response_code(500); echo json_encode(['error' => 'open failed']); exit; }
flock($fh, LOCK_EX);
fwrite($fh, $chunk);
flock($fh, LOCK_UN);
fclose($fh);

echo json_encode(['ok' => true, 'file' => "/logs/$filename"]);
