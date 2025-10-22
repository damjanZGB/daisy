// s3escalator.mjs -- Generic S3 uploader for transcripts, logs, and alerts.
//
// Expected POST /tools/s3escalator JSON payload fields:
// {
//   "type": "transcript|log|alert",         // REQUIRED classification
//   "path": "segment1/segment2",             // REQUIRED additional folder path (slash separated)
//   "sender": "alice",                       // REQUIRED sender identifier (name/alias/id)
//   "fileName": "custom-name.log",           // optional explicit file name (sanitized)
//   "fileEncoding": "base64|utf8",           // optional hint when using the "file" field
//   "file": "...",                           // raw UTF-8 text or base64 depending on fileEncoding
//   "fileBase64": "...",                     // base64 payload
//   "fileData": "...",                       // alias for base64 payload
//   "contentType": "application/json"        // optional S3 Content-Type header
// }
//
// The service authenticates requests via X-Proxy-Token / X-Uploader-Token header.

import http from "node:http";
import url from "node:url";
import crypto from "node:crypto";

import {
  S3Client,
  PutObjectCommand,
  HeadBucketCommand,
} from "@aws-sdk/client-s3";

const PORT = Number(process.env.PORT || 8788);
const REGION = process.env.AWS_REGION || "us-west-2";
const S3_BUCKET = (process.env.S3_BUCKET || "").trim();
const S3_PREFIX = (process.env.S3_PREFIX || "dAisys-diary").replace(/\/+$/, "");
const SHARED_SECRET = (process.env.UPLOADER_TOKEN || "").trim();
const DEFAULT_ALIAS_ID = (process.env.AGENT_ALIAS_ID || "").trim();
const DEFAULT_AGENT_VERSION = (process.env.AGENT_VERSION || "").trim() || "unknown";
const rawOrigin = process.env.ORIGIN || "";
const ALLOW_ORIGINS = String(rawOrigin)
  .split(/[\s,]+/)
  .map(s => s.trim())
  .filter(Boolean);
const ALLOW_ORIGIN_SET = new Set(ALLOW_ORIGINS);
const ALLOW_ORIGIN_ENTRIES = ALLOW_ORIGINS.map(value => {
  if (value === "*") return { value: "*", host: "*" };
  let host = value;
  try {
    host = new URL(value).host;
  } catch (_) {
    host = value.replace(/^https?:\/\//i, "").replace(/\/$/, "");
  }
  return { value, host };
});
function normalizeOriginHost(origin) {
  if (!origin) return "";
  try {
    return new URL(origin).host;
  } catch (_) {
    return origin.replace(/^https?:\/\//i, "").replace(/\/$/, "");
  }
}
const MAX_BODY_SIZE = 5 * 1024 * 1024; // 5 MiB

if (!S3_BUCKET) {
  throw new Error("Missing required environment variable: S3_BUCKET");
}
if (!process.env.AWS_ACCESS_KEY_ID || !process.env.AWS_SECRET_ACCESS_KEY) {
  throw new Error("AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be configured");
}
if (!SHARED_SECRET) {
  throw new Error("Missing required environment variable: UPLOADER_TOKEN");
}

const s3Client = new S3Client({ region: REGION, maxAttempts: 3 });

const logger = {
  info: (...args) => console.log(new Date().toISOString(), "[INFO]", ...args),
  warn: (...args) => console.warn(new Date().toISOString(), "[WARN]", ...args),
  error: (...args) => console.error(new Date().toISOString(), "[ERROR]", ...args),
};

const readiness = {
  ready: false,
  lastUpdated: 0,
  checks: { s3: false },
  errors: [],
};

async function runReadinessCheck() {
  const errors = [];
  let s3Ok = false;
  try {
    const head = new HeadBucketCommand({ Bucket: S3_BUCKET });
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    await s3Client.send(head, { abortSignal: controller.signal });
    clearTimeout(timer);
    s3Ok = true;
  } catch (error) {
    errors.push(`s3:${error?.name || error?.message || "error"}`);
  }
  readiness.checks = { s3: s3Ok };
  readiness.ready = s3Ok;
  readiness.errors = errors;
  readiness.lastUpdated = Date.now();
}

runReadinessCheck().catch(() => {});
setInterval(() => { runReadinessCheck().catch(() => {}); }, 60_000);

function respond(res, status, payload) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(payload));
}

function setCorsHeaders(res) {
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type,X-Proxy-Token,X-Uploader-Token");
  res.setHeader("Vary", "Origin");
}
function enforceCors(req, res) {
  const origin = req.headers.origin || "";
  if (!origin) {
    setCorsHeaders(res);
    return true;
  }
  if (ALLOW_ORIGIN_SET.has("*")) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    setCorsHeaders(res);
    return true;
  }
  const originHost = normalizeOriginHost(origin);
  const match = ALLOW_ORIGIN_ENTRIES.find(entry => {
    if (entry.value === "*") return true;
    if (entry.value === origin) return true;
    if (entry.host && entry.host === originHost) return true;
    return false;
  });
  if (match) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    setCorsHeaders(res);
    return true;
  }
  logger.warn("Blocked request from disallowed origin", { origin, path: req.url || "" });
  res.statusCode = 403;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify({ error: "origin_not_allowed" }));
  return false;
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    let size = 0;
    let done = false;
    req.on("data", chunk => {
      if (done) return;
      size += chunk.length;
      if (size > MAX_BODY_SIZE) {
        done = true;
        reject(new Error("Payload too large"));
        req.destroy();
        return;
      }
      data += chunk;
    });
    req.on("end", () => {
      if (done) return;
      done = true;
      if (!data) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(data));
      } catch (error) {
        const parseError = new Error("Invalid JSON body");
        parseError.cause = error;
        reject(parseError);
      }
    });
    req.on("error", error => {
      if (done) return;
      done = true;
      reject(error);
    });
  });
}

const SLUG_PATTERN = /[^a-z0-9-]+/g;

function toSlug(value, fallback) {
  const base = String(value || "").trim().toLowerCase() || fallback || "unknown";
  const slug = base
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(SLUG_PATTERN, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
  return slug || (fallback || "unknown");
}

function sanitizePath(path) {
  return String(path || "")
    .split(/[\/]+/)
    .map(part => toSlug(part, "segment"))
    .filter(Boolean);
}

function sanitizeFileName(name) {
  if (!name) return "";
  return String(name)
    .trim()
    .replace(/[^\w.\-+=@()\[\]{}!#$%&'`~^ ]+/g, "_")
    .replace(/\s+/g, "_");
}

function extensionFromContentType(contentType) {
  const ct = String(contentType || "").toLowerCase();
  if (!ct.includes("/")) return "txt";
  const main = ct.split(";")[0].trim();
  if (main === "text/plain") return "txt";
  if (main === "application/json") return "json";
  if (main === "text/html") return "html";
  if (main.endsWith("/csv")) return "csv";
  const sub = main.split("/")[1] || "dat";
  return sub.replace(/[^a-z0-9]/g, "") || "dat";
}

function detectBase64(str) {
  if (typeof str !== "string") return false;
  const cleaned = str.replace(/\s+/g, "");
  if (cleaned.length === 0 || cleaned.length % 4 !== 0) return false;
  return /^[A-Za-z0-9+/]+={0,2}$/.test(cleaned);
}

function decodeFilePayload(body) {
  if (!body || typeof body !== "object") return null;
  const encoding = String(body.fileEncoding || "").toLowerCase();
  if (typeof body.file === "string") {
    if (encoding === "base64") {
      return Buffer.from(body.file, "base64");
    }
    if (encoding === "utf8" || encoding === "text") {
      return Buffer.from(body.file, "utf8");
    }
    if (detectBase64(body.file)) {
      try { return Buffer.from(body.file, "base64"); } catch (_) {}
    }
    return Buffer.from(body.file, "utf8");
  }
  const base64 = body.fileBase64 || body.fileData;
  if (typeof base64 === "string") {
    return Buffer.from(base64, "base64");
  }
  return null;
}

const MAX_SESSION_SEGMENT = 64;

async function handleUpload(req, res) {
  const token = String(req.headers["x-proxy-token"] || req.headers["x-uploader-token"] || "").trim();
  if (!token || token !== SHARED_SECRET) {
    respond(res, 403, { error: "forbidden" });
    return;
  }

  let payload;
  try {
    payload = await readBody(req);
  } catch (error) {
    const status = error.message === "Payload too large" ? 413 : 400;
    respond(res, status, { error: error.message });
    return;
  }

  const typeIndicator = toSlug(payload?.type, "");
  if (!typeIndicator) {
    respond(res, 400, { error: "type_required" });
    return;
  }

  const pathModeRaw = String(payload?.pathMode || "").trim().toLowerCase();
  const absolutePathMode = pathModeRaw === "absolute" || pathModeRaw === "abs";

  const pathSegments = sanitizePath(payload?.path);
  if (pathSegments.length === 0) {
    respond(res, 400, { error: "path_required" });
    return;
  }

  const sender = String(payload?.sender || "").trim() || DEFAULT_ALIAS_ID || "unknown";
  if (!sender) {
    respond(res, 400, { error: "sender_required" });
    return;
  }
  const senderSlug = toSlug(sender, "sender");

  let contentType = String(payload?.contentType || "").trim();
  if (!contentType) {
    contentType = "text/plain; charset=utf-8";
  }

  const fileBuffer = decodeFilePayload(payload);
  if (!fileBuffer) {
    respond(res, 400, { error: "file_required" });
    return;
  }

  const isoTimestamp = new Date().toISOString();
  const isoDate = isoTimestamp.slice(0, 10);
  const timeSlug = isoTimestamp.replace(/[:.]/g, "-");

  let fileName = sanitizeFileName(payload?.fileName);
  if (!fileName) {
    const ext = extensionFromContentType(contentType);
    fileName = `${senderSlug}_${typeIndicator}_${timeSlug}.${ext}`;
  }

  const segments = [];
  if (S3_PREFIX) segments.push(S3_PREFIX);
  if (absolutePathMode) {
    segments.push(...pathSegments);
  } else {
    segments.push(typeIndicator);
    segments.push(...pathSegments);
    segments.push(senderSlug);
    segments.push(isoDate);
  }
  const key = `${segments.join("/")}/${fileName}`;

  try {
    await s3Client.send(new PutObjectCommand({
      Bucket: S3_BUCKET,
      Key: key,
      Body: fileBuffer,
      ContentType: contentType,
      ServerSideEncryption: "AES256",
      Metadata: {
        type: typeIndicator,
        sender: senderSlug.slice(0, MAX_SESSION_SEGMENT),
        alias: String(DEFAULT_ALIAS_ID || "").slice(0, MAX_SESSION_SEGMENT),
        version: String(DEFAULT_AGENT_VERSION || "unknown").slice(0, MAX_SESSION_SEGMENT),
      },
    }));
    respond(res, 200, { ok: true, key, bytes: fileBuffer.length });
    logger.info("S3 upload success", { key, bytes: fileBuffer.length });
  } catch (error) {
    logger.error("Upload failed", { message: error?.message || String(error) });
    respond(res, 502, { error: "upload_failed" });
  }
}

const server = http.createServer(async (req, res) => {
  const requestId = crypto.randomUUID();
  const startedAt = Date.now();
  if (!enforceCors(req, res)) { return; }

  if (req.method === "OPTIONS") {
    res.statusCode = 204;
    setCorsHeaders(res);
    res.end();
    return;
  }

  logger.info(`[${requestId}] ${req.method} ${req.url}`);

  let parsed;
  try {
    parsed = new url.URL(req.url, "http://localhost");
  } catch {
    respond(res, 400, { error: "invalid_url" });
    return;
  }

  try {
    if (req.method === "POST" && parsed.pathname === "/tools/s3escalator") {
      await handleUpload(req, res);
      return;
    }
    if ((req.method === "GET" || req.method === "HEAD") && parsed.pathname === "/health") {
      if (req.method === "HEAD") { res.statusCode = 200; res.end(); return; }
      respond(res, 200, { ok: true, time: new Date().toISOString() });
      return;
    }
    if ((req.method === "GET" || req.method === "HEAD") && parsed.pathname === "/ready") {
      const ageMs = Date.now() - (readiness.lastUpdated || 0);
      const body = {
        ok: !!readiness.ready,
        time: new Date().toISOString(),
        lastUpdated: readiness.lastUpdated ? new Date(readiness.lastUpdated).toISOString() : null,
        checks: readiness.checks,
        errors: readiness.errors,
        ageMs,
      };
      res.statusCode = readiness.ready ? 200 : 503;
      if (req.method === "HEAD") { res.end(); return; }
      respond(res, res.statusCode, body);
      return;
    }
    respond(res, 404, { error: "not_found" });
  } catch (error) {
    logger.error(`[${requestId}] Request failed`, {
      message: error?.message || String(error),
      stack: error?.stack,
    });
    if (!res.headersSent) {
      respond(res, 500, { error: "internal_error" });
    }
  } finally {
    logger.info(`[${requestId}] Completed with status ${res.statusCode} in ${Date.now() - startedAt}ms`);
  }
});

server.listen(PORT, () => {
  logger.info(`s3escalator listening on :${PORT}`, { bucket: S3_BUCKET, prefix: S3_PREFIX });
});

