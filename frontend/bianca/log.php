<?php
// public_html/origin/log.php
declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: https://origin.daisy.voyage');
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { http_response_code(204); exit; }

$raw = file_get_contents('php://input');
if (!$raw) { http_response_code(400); echo json_encode(['error'=>'empty body']); exit; }
$in = json_decode($raw, true);
$filename = $in['filename'] ?? '';
$chunk = $in['chunk'] ?? '';

if (!preg_match('/^LH\\d{4}\\.log$/', $filename)) {
  http_response_code(400); echo json_encode(['error'=>'bad filename']); exit;
}

$dir = __DIR__ . '/logs';
if (!is_dir($dir)) { mkdir($dir, 0775, true); }

$path = $dir . '/' . $filename;
$fh = fopen($path, 'ab');
if (!$fh) { http_response_code(500); echo json_encode(['error'=>'open failed']); exit; }
flock($fh, LOCK_EX);
fwrite($fh, $chunk);
flock($fh, LOCK_UN);
fclose($fh);

echo json_encode(['ok'=>true, 'file'=>"/logs/$filename"]);
