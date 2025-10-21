// antiPhrase.mjs -- Natural language time phrase parser service.
//
// POST /tools/antiPhaser payload:
// {
//   "text": "next Friday to Sunday",
//   "timezone": "Europe/Berlin" // optional (defaults to UTC)
// }
//
// Response: { ok, input, timezone, departDate, returnDate, details }
// The service authenticates cross-origin callers using the ORIGIN env variable (comma/space separated list).

import http from "node:http";
import url from "node:url";
import crypto from "node:crypto";
import * as chrono from "chrono-node";
import { DateTime } from "luxon";

const PORT = Number(process.env.PORT || 8789);
const REGION = process.env.AWS_REGION || "us-west-2"; // unused but kept for parity
const DEFAULT_TIMEZONE = (process.env.DEFAULT_TIMEZONE || "UTC").trim() || "UTC";
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
  try { return new URL(origin).host; }
  catch (_) { return origin.replace(/^https?:\/\//i, "").replace(/\/$/, ""); }
}

const MAX_BODY_SIZE = 256 * 1024; // 256 KiB

const logger = {
  info: (...args) => console.log(new Date().toISOString(), "[INFO]", ...args),
  warn: (...args) => console.warn(new Date().toISOString(), "[WARN]", ...args),
  error: (...args) => console.error(new Date().toISOString(), "[ERROR]", ...args),
};

const readiness = {
  ready: true,
  lastUpdated: Date.now(),
  checks: { chrono: true },
  errors: [],
};

function setCorsHeaders(res) {
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
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

function parseTimePhrase(text, timezone) {
  const tz = timezone || DEFAULT_TIMEZONE;
  const results = chrono.parse(text, undefined, { timezone: tz });
  const first = results[0];
  if (!first) {
    return { departDate: undefined, returnDate: undefined, details: null };
  }
  const toISO = d => d ? DateTime.fromJSDate(d, { zone: tz }).toISODate() : undefined;
  const start = first.start?.date();
  const end = first.end?.date();
  const details = {
    knownValues: first.start?.knownValues ?? null,
    impliedValues: first.start?.impliedValues ?? null,
    text: first.text,
  };
  if (end) {
    details.endKnownValues = first.end?.knownValues ?? null;
    details.endImpliedValues = first.end?.impliedValues ?? null;
  }
  return {
    departDate: toISO(start),
    returnDate: toISO(end),
    details,
  };
}

async function handleAntiPhaser(req, res, body) {
  const text = String(body?.text || "").trim();
  if (!text) {
    res.statusCode = 400;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "text_required" }));
    return;
  }
  const timezone = String(body?.timezone || body?.tz || DEFAULT_TIMEZONE).trim() || DEFAULT_TIMEZONE;
  try {
    const result = parseTimePhrase(text, timezone);
    if (!result.departDate && !result.returnDate) {
      res.statusCode = 422;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ ok: false, error: "unable_to_parse", input: text, timezone }));
      return;
    }
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ ok: true, input: text, timezone, ...result }));
  } catch (error) {
    logger.error("antiPhaser error", { message: error?.message || String(error) });
    res.statusCode = 500;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "internal_error" }));
  }
}

const server = http.createServer(async (req, res) => {
  const requestId = crypto.randomUUID();
  const startedAt = Date.now();
  logger.info(`[${requestId}] ${req.method} ${req.url}`);

  if (!enforceCors(req, res)) { return; }

  if (req.method === "OPTIONS") {
    res.statusCode = 204;
    setCorsHeaders(res);
    res.end();
    return;
  }

  let parsed;
  try {
    parsed = new url.URL(req.url, "http://localhost");
  } catch (_) {
    res.statusCode = 400;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "invalid_url" }));
    return;
  }

  try {
    if (req.method === "POST" && parsed.pathname === "/tools/antiPhaser") {
      let body;
      try {
        body = await readBody(req);
      } catch (error) {
        const status = error.message === "Payload too large" ? 413 : 400;
        res.statusCode = status;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: error.message }));
        return;
      }
      await handleAntiPhaser(req, res, body);
      return;
    }

    if (req.method === "GET" && parsed.pathname === "/tools/antiPhaser") {
      const text = parsed.searchParams.get("text");
      const timezone = parsed.searchParams.get("timezone") || parsed.searchParams.get("tz") || DEFAULT_TIMEZONE;
      await handleAntiPhaser(req, res, { text, timezone });
      return;
    }

    if ((req.method === "GET" || req.method === "HEAD") && parsed.pathname === "/health") {
      if (req.method === "HEAD") { res.statusCode = 200; res.end(); return; }
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ ok: true, time: new Date().toISOString() }));
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
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(body));
      return;
    }

    res.statusCode = 404;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "not_found" }));
  } catch (error) {
    logger.error(`[${requestId}] Request failed`, { message: error?.message || String(error), stack: error?.stack });
    if (!res.headersSent) {
      res.statusCode = 500;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: "internal_error" }));
    }
  } finally {
    logger.info(`[${requestId}] Completed with status ${res.statusCode} in ${Date.now() - startedAt}ms`);
  }
});

server.listen(PORT, () => {
  logger.info(`antiPhaser listening on :${PORT}`, { timezone: DEFAULT_TIMEZONE, origins: [...ALLOW_ORIGIN_SET] });
});
