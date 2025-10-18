// proxy.mjs — Render backend for Bedrock Agent + Amadeus adapter + IATA lookup
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import url from "node:url";
import crypto from "node:crypto";

import { PutObjectCommand, S3Client } from "@aws-sdk/client-s3";

const logger = {
  info: (...args) => console.log(new Date().toISOString(), "[INFO]", ...args),
  warn: (...args) => console.warn(new Date().toISOString(), "[WARN]", ...args),
  error: (...args) => console.error(new Date().toISOString(), "[ERROR]", ...args),
};

const PORT = 8787;
const REGION = process.env.AWS_REGION || "us-west-2";
const AGENT_ID = process.env.AGENT_ID;
const AMADEUS_ENV = process.env.AMADEUS_ENV || "test";
const AMADEUS_HOST =
  AMADEUS_ENV === "production"
    ? "https://api.amadeus.com"
    : "https://test.api.amadeus.com";
const AMADEUS_API_KEY = process.env.AMADEUS_API_KEY || "";
const AMADEUS_API_SECRET = process.env.AMADEUS_API_SECRET || "";
const IATA_DB_PATH = process.env.IATA_DB_PATH || "./iata.json";
const AGENT_ALIAS_ID = (process.env.AGENT_ALIAS_ID || "").trim();
const AWS_ACCESS_KEY_ID = (process.env.AWS_ACCESS_KEY_ID || "").trim();
const AWS_SECRET_ACCESS_KEY = (process.env.AWS_SECRET_ACCESS_KEY || "").trim();
const rawOrigin = process.env.ORIGIN || "";
const ALLOW_ORIGINS = String(rawOrigin)
  .split(",")
  .map(s => s.trim())
  .filter(Boolean);
const ALLOW_ORIGIN_SET = new Set(ALLOW_ORIGINS);
const MAX_BODY_SIZE = 1024 * 1024; // 1 MiB
const TRANSCRIPT_BUCKET = (process.env.TRANSCRIPT_BUCKET || "").trim();
const TRANSCRIPT_PREFIX = (process.env.TRANSCRIPT_PREFIX || "").trim();
const TRANSCRIPT_SCHEMA_VERSION = "2025-10-18";
const transcriptS3Client = TRANSCRIPT_BUCKET
  ? new S3Client({ region: REGION, maxAttempts: 3 })
  : null;

const missingEnv = [];
if (!AGENT_ID) missingEnv.push("AGENT_ID");
if (!AGENT_ALIAS_ID) missingEnv.push("AGENT_ALIAS_ID");
if (!AWS_ACCESS_KEY_ID) missingEnv.push("AWS_ACCESS_KEY_ID");
if (!AWS_SECRET_ACCESS_KEY) missingEnv.push("AWS_SECRET_ACCESS_KEY");
if (!ALLOW_ORIGINS.length) missingEnv.push("ORIGIN");
if (missingEnv.length > 0) {
  throw new Error(`Missing required environment variable(s): ${missingEnv.join(", ")}`);
}

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
  if (ALLOW_ORIGIN_SET.has(origin)) {
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
async function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    let size = 0;
    let completed = false;
    req.on("data", chunk => {
      if (completed) return;
      size += chunk.length;
      if (size > MAX_BODY_SIZE) {
        completed = true;
        req.destroy();
        reject(new Error("Payload too large"));
        return;
      }
      data += chunk;
    });
    req.on("end", () => {
      if (completed) return;
      completed = true;
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch (error) {
        const parseError = new Error("Invalid JSON body");
        parseError.cause = error;
        reject(parseError);
      }
    });
    req.on("error", error => {
      if (completed) return;
      completed = true;
      reject(error);
    });
  });
}

const SLUG_PATTERN = /[^a-z0-9-]+/g;
const MAX_SESSION_SEGMENT = 64;

const toSlug = (value, fallback) => {
  const base = String(value || "").trim().toLowerCase() || fallback || "unknown";
  const slug = base.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").replace(SLUG_PATTERN, "-").replace(/-+/g, "-").replace(/^-|-$/g, "");
  return slug || (fallback || "unknown");
};

const toIsoString = (input, fallbackDate = new Date()) => {
  try {
    const parsed = input ? new Date(input) : null;
    if (parsed && !Number.isNaN(parsed.getTime())) {
      return parsed.toISOString();
    }
  } catch (error) {
    // ignore invalid dates
  }
  return fallbackDate.toISOString();
};

function validateTranscriptPayload(payload) {
  if (!payload || typeof payload !== "object") {
    return { ok: false, error: "invalid_payload" };
  }
  const schemaVersion = String(payload.schemaVersion || "").trim() || TRANSCRIPT_SCHEMA_VERSION;
  const persona = toSlug(payload.persona, "unknown");
  const variant = toSlug(payload.variant, persona);
  const sessionId = String(payload.sessionId || payload.session || "").trim();
  if (!sessionId) {
    return { ok: false, error: "session_required" };
  }
  if (!Array.isArray(payload.messages) || payload.messages.length === 0) {
    return { ok: false, error: "messages_required" };
  }
  const messages = payload.messages
    .slice(0, 1000)
    .map(msg => ({
      role: String(msg.role || "").trim().toLowerCase() || "assistant",
      text: String(msg.text ?? ""),
      meta: msg.meta === undefined ? null : msg.meta,
      ts: toIsoString(msg.ts),
    }));
  const startedAt = toIsoString(payload.startedAt, new Date(messages[0]?.ts || Date.now()));
  const completedAt = toIsoString(payload.completedAt, new Date());
  const flight = String(payload.flight || payload.flightNumber || "").trim();
  const location = payload.location && typeof payload.location === "object"
    ? {
        label: String(payload.location.label ?? ""),
        tz: String(payload.location.tz ?? ""),
        inferredOrigin: String(payload.location.inferredOrigin ?? ""),
      }
    : null;
  return {
    ok: true,
    schemaVersion,
    persona,
    variant,
    sessionId,
    startedAt,
    completedAt,
    flight,
    location,
    messages,
    raw: payload,
  };
}

function buildTranscriptKey({ persona, variant, sessionId, startedAt }) {
  const started = new Date(startedAt);
  const year = started.getUTCFullYear();
  const month = String(started.getUTCMonth() + 1).padStart(2, "0");
  const day = String(started.getUTCDate()).padStart(2, "0");
  const timestamp = started.toISOString().replace(/[:.]/g, "-");
  const sessionSegment = toSlug(sessionId, "session").slice(0, MAX_SESSION_SEGMENT);
  const personaSegment = toSlug(persona, "unknown");
  const variantSegment = toSlug(variant, personaSegment);
  const prefix = TRANSCRIPT_PREFIX ? `${TRANSCRIPT_PREFIX.replace(/\/+$/, "")}/` : "";
  return `${prefix}${year}/${month}/${day}/${variantSegment}/${timestamp}_${sessionSegment}.json`;
}

// ---- Minimal SigV4 for Bedrock Agent Runtime ----
function sha256(buf) { return crypto.createHash("sha256").update(buf).digest("hex"); }
function hmac(key, str) { return crypto.createHmac("sha256", key).update(str).digest(); }
function signV4({ service, region, method, hostname, path, headers, body, accessKeyId, secretAccessKey }) {
  const now = new Date();
  const amzDate = now.toISOString().replace(/[:-]|\.\d{3}/g, "");
  const dateStamp = amzDate.slice(0, 8);
  const payloadHash = sha256(body || "");
  headers["x-amz-date"] = amzDate;
  headers["x-amz-content-sha256"] = payloadHash;
  const canonicalHeaders = Object.entries(headers)
    .map(([k, v]) => [k.toLowerCase().trim(), String(v).trim()])
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map(([k, v]) => `${k}:${v}\n`).join("");
  const signedHeaders = Object.keys(headers).map(k => k.toLowerCase()).sort().join(";");
  const canonicalRequest = `${method}\n${path}\n\n${canonicalHeaders}\n${signedHeaders}\n${payloadHash}`;
  const algorithm = "AWS4-HMAC-SHA256";
  const credentialScope = `${dateStamp}/${region}/${service}/aws4_request`;
  const stringToSign = `${algorithm}\n${amzDate}\n${credentialScope}\n${sha256(canonicalRequest)}`;
  const kDate = hmac("AWS4" + secretAccessKey, dateStamp);
  const kRegion = hmac(kDate, region);
  const kService = hmac(kRegion, service);
  const kSigning = hmac(kService, "aws4_request");
  const signature = crypto.createHmac("sha256", kSigning).update(stringToSign).digest("hex");
  const authorization = `${algorithm} Credential=${accessKeyId}/${credentialScope}, SignedHeaders=${signedHeaders}, Signature=${signature}`;
  return { amzDate, authorization, payloadHash };
}
function parseEventStreamHeaders(buf) {
  let offset = 0;
  const headers = {};
  while (offset < buf.length) {
    const nameLen = buf[offset];
    offset += 1;
    const name = buf.slice(offset, offset + nameLen).toString("utf8");
    offset += nameLen;
    const type = buf[offset];
    offset += 1;
    let value;
    switch (type) {
      case 0: // bool true
        value = true;
        break;
      case 1: // bool false
        value = false;
        break;
      case 2: // byte
        value = buf.readInt8(offset);
        offset += 1;
        break;
      case 3: // short
        value = buf.readInt16BE(offset);
        offset += 2;
        break;
      case 4: // int
        value = buf.readInt32BE(offset);
        offset += 4;
        break;
      case 5: // long
        value = buf.readBigInt64BE(offset);
        offset += 8;
        break;
      case 6: { // byte array
        const len = buf.readUInt16BE(offset);
        offset += 2;
        value = buf.slice(offset, offset + len);
        offset += len;
        break;
      }
      case 7: { // string
        const len = buf.readUInt16BE(offset);
        offset += 2;
        value = buf.slice(offset, offset + len).toString("utf8");
        offset += len;
        break;
      }
      case 8: { // timestamp (epoch millis)
        const ms = Number(buf.readBigInt64BE(offset));
        offset += 8;
        value = new Date(ms);
        break;
      }
      case 9: { // uuid
        const bytes = buf.slice(offset, offset + 16);
        offset += 16;
        const hex = bytes.toString("hex");
        value = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
        break;
      }
      default: {
        throw new Error(`Unsupported event stream header type ${type}`);
      }
    }
    headers[name] = value;
  }
  return headers;
}
function parseEventStream(buffer) {
  const messages = [];
  let offset = 0;
  while (offset + 8 <= buffer.length) {
    const totalLen = buffer.readUInt32BE(offset);
    offset += 4;
    if (totalLen < 16 || offset + totalLen - 4 > buffer.length) {
      break;
    }
    const headersLen = buffer.readUInt32BE(offset);
    offset += 4;
    // Skip prelude CRC (4 bytes) per AWS event stream spec
    offset += 4;
    if (headersLen < 0 || offset + headersLen > buffer.length) {
      break;
    }
    const headersBuf = buffer.slice(offset, offset + headersLen);
    offset += headersLen;
    const payloadLen = totalLen - headersLen - 16;
    if (payloadLen < 0 || offset + payloadLen + 4 > buffer.length) {
      break;
    }
    const payload = buffer.slice(offset, offset + payloadLen);
    offset += payloadLen;
    offset += 4; // skip message CRC
    let headers = {};
    try {
      headers = parseEventStreamHeaders(headersBuf);
    } catch (err) {
      headers = { __parseError: err.message };
    }
    messages.push({ headers, payload });
  }
  return messages;
}
function decodeAgentEventStream(buffer) {
  const messages = parseEventStream(buffer);
  const chunks = [];
  const events = [];
  let finalResponse = null;
  for (const { headers, payload } of messages) {
    const eventType = headers[":event-type"] || headers.eventType;
    const messageType = headers[":message-type"] || headers.messageType;
    const payloadText = payload.toString("utf8");
    const eventRecord = { headers };
    if (payload.length > 0) eventRecord.payload = payloadText;
    let json;
    if (payloadText) {
      try {
        json = JSON.parse(payloadText);
        eventRecord.json = json;
      } catch (error) {
        // leave as raw payload text
      }
    }
    events.push(eventRecord);
    if (messageType !== "event") continue;
    if (!payloadText) continue;
    if (!json) continue;
    const byteSources = [];
    if (typeof json.bytes === "string") byteSources.push(json.bytes);
    if (typeof json.chunk?.bytes === "string") byteSources.push(json.chunk.bytes);
    if (Array.isArray(json.bytesList)) {
      for (const item of json.bytesList) {
        if (typeof item === "string") byteSources.push(item);
      }
    }
    for (const base64 of byteSources) {
      try {
        const buffer = Buffer.from(base64, "base64");
        const decoded = buffer.toString("utf8");
        if (decoded) chunks.push(decoded);
        if (decoded) {
          if (!eventRecord.decodedText) eventRecord.decodedText = [];
          eventRecord.decodedText.push(decoded);
        }
      } catch (error) {
        // ignore invalid base64
      }
    }
    const collectTextArray = arr => {
      if (!Array.isArray(arr)) return;
      const text = arr.map(part => part?.text || "").join("");
      if (text) chunks.push(text);
    };
    collectTextArray(json.outputText);
    collectTextArray(json.response?.outputText);
    if (typeof json.text === "string") {
      chunks.push(json.text);
    }
    if (eventType === "final-response") {
      finalResponse = json;
    }
  }
  const combined = chunks.join("");
  const askUserQuestions = [];
  const askUserTag = /<user[\w.\-]*askuser\b[^>]*question="([^"]+)"[^>]*\/?>/gi;
  let cleanedText = combined.replace(askUserTag, (_, question) => {
    const decoded = question
      .replace(/&quot;/g, '"')
      .replace(/&apos;/g, "'")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">");
    askUserQuestions.push(decoded);
    return decoded;
  });
  cleanedText = cleanedText.trim();
  if (!cleanedText && askUserQuestions.length > 0) {
    cleanedText = askUserQuestions[askUserQuestions.length - 1];
  }
  const result = { text: cleanedText, events };
  if (askUserQuestions.length > 0) result.askUserQuestions = askUserQuestions;
  if (finalResponse) result.finalResponse = finalResponse;
  return result;
}
async function awsInvokeAgent({
  aliasId,
  sessionId,
  inputText,
  sessionAttributes,
  promptSessionAttributes,
}) {
  const service = "bedrock";
  const hostname = `bedrock-agent-runtime.${REGION}.amazonaws.com`;
  const path = `/agents/${encodeURIComponent(AGENT_ID)}/agentAliases/${encodeURIComponent(aliasId)}/sessions/${encodeURIComponent(sessionId)}/text`;
  const payload = { inputText };
  const sessionState = {};
  if (sessionAttributes && Object.keys(sessionAttributes).length > 0) {
    sessionState.sessionAttributes = sessionAttributes;
  }
  if (promptSessionAttributes && Object.keys(promptSessionAttributes).length > 0) {
    sessionState.promptSessionAttributes = promptSessionAttributes;
  }
  if (Object.keys(sessionState).length > 0) {
    payload.sessionState = sessionState;
  }
  const body = JSON.stringify(payload);
  const headers = { "content-type": "application/json", "host": hostname };
  const { amzDate, authorization, payloadHash } = signV4({
    service,
    region: REGION,
    method: "POST",
    hostname,
    path,
    headers,
    body,
    accessKeyId: AWS_ACCESS_KEY_ID,
    secretAccessKey: AWS_SECRET_ACCESS_KEY,
  });
  headers["x-amz-date"] = amzDate; headers["x-amz-content-sha256"] = payloadHash; headers["authorization"] = authorization;
  const resp = await fetch(`https://${hostname}${path}`, { method: "POST", headers, body });
  const arrayBuffer = await resp.arrayBuffer();
  const rawBuffer = Buffer.from(arrayBuffer);
  if (!resp.ok) {
    const errorText = rawBuffer.toString("utf8");
    logger.error("InvokeAgent failed", {
      status: resp.status,
      response: errorText.slice(0, 500),
    });
    throw new Error(`InvokeAgent failed: ${resp.status} ${errorText}`);
  }
  const contentType = resp.headers.get("content-type") || "";
  if (contentType.includes("eventstream")) {
    return decodeAgentEventStream(rawBuffer);
  }
  const text = rawBuffer.toString("utf8");
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

// ---- Amadeus adapter ----
let tokenCache = { token: null, expiresAt: 0 };
async function amadeusToken() {
  if (!AMADEUS_API_KEY || !AMADEUS_API_SECRET) {
    throw new Error("Configure AMADEUS_API_KEY and AMADEUS_API_SECRET directly in proxy.mjs");
  }
  const now = Date.now();
  if (tokenCache.token && now < tokenCache.expiresAt - 30000) return tokenCache.token;
  const form = new URLSearchParams();
  form.set("grant_type", "client_credentials");
  form.set("client_id", AMADEUS_API_KEY);
  form.set("client_secret", AMADEUS_API_SECRET);
  const resp = await fetch(`${AMADEUS_HOST}/v1/security/oauth2/token`, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: form.toString() });
  const json = await resp.json();
  if (!resp.ok) {
    logger.error("Amadeus token request failed", { status: resp.status, response: JSON.stringify(json).slice(0, 500) });
    throw new Error(`Amadeus token error: ${resp.status}`);
  }
  tokenCache.token = json.access_token; tokenCache.expiresAt = Date.now() + (json.expires_in || 600) * 1000;
  return tokenCache.token;
}
function buildSearchQuery(q) {
  const params = new URLSearchParams();
  params.set("originLocationCode", q.originLocationCode);
  params.set("destinationLocationCode", q.destinationLocationCode);
  params.set("departureDate", q.departureDate);
  params.set("adults", String(q.adults || 1));
  if (q.returnDate) params.set("returnDate", q.returnDate);
  if (q.currencyCode) params.set("currencyCode", q.currencyCode);
  if (q.max) params.set("max", String(q.max));
  if (q.nonStop !== undefined) params.set("nonStop", String(q.nonStop));
  if (q.travelClass) params.set("travelClass", q.travelClass);
  if (q.children) params.set("children", String(q.children));
  if (q.infants) params.set("infants", String(q.infants));
  return params.toString();
}
function normalizeOffers(json) {
  const offers = (json.data || []).map(o => ({
    id: o.id,
    price: o.price?.total,
    currency: o.price?.currency,
    itineraries: (o.itineraries || []).map(it => ({
      duration: it.duration,
      segments: it.segments?.map(s => ({
        carrierCode: s.carrierCode,
        number: s.number,
        departure: s.departure?.iataCode,
        departureTime: s.departure?.at,
        arrival: s.arrival?.iataCode,
        arrivalTime: s.arrival?.at,
        aircraft: s.aircraft?.code,
      })) || []
    }))
  }));
  return { offers, raw: json };
}
let IATA_DB = null;
const EARTH_RADIUS_KM = 6371;
const toRadians = (deg) => (deg * Math.PI) / 180;
function haversineDistanceKm(lat1, lon1, lat2, lon2) {
  const dLat = toRadians(lat2 - lat1);
  const dLon = toRadians(lon2 - lon1);
  const rLat1 = toRadians(lat1);
  const rLat2 = toRadians(lat2);
  const a = Math.sin(dLat / 2) ** 2 + Math.sin(dLon / 2) ** 2 * Math.cos(rLat1) * Math.cos(rLat2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return EARTH_RADIUS_KM * c;
}
function loadIata() {
  if (IATA_DB) return IATA_DB;
  try {
    IATA_DB = JSON.parse(fs.readFileSync(path.resolve(IATA_DB_PATH), "utf8"));
  } catch (error) {
    logger.warn("Unable to load IATA database file", { file: IATA_DB_PATH, message: error.message });
    IATA_DB = {};
  }
  return IATA_DB;
}
function iataLookup({ term, lat, lon, limit = 20 } = {}) {
  const db = loadIata();
  const q = String(term || "").trim().toUpperCase();
  const latNum = Number(lat);
  const lonNum = Number(lon);
  const hasCoords = Number.isFinite(latNum) && Number.isFinite(lonNum);
  const results = [];

  if (hasCoords) {
    for (const [code, rec] of Object.entries(db)) {
      if ((rec.type || "").toLowerCase() !== "airport") continue;
      const recLat = Number(rec.latitude);
      const recLon = Number(rec.longitude);
      if (!Number.isFinite(recLat) || !Number.isFinite(recLon)) continue;
      const airportCode = code.toUpperCase();
      const name = String(rec.name || "").toUpperCase();
      const city = String(rec.city || "").toUpperCase();
      if (q && !airportCode.includes(q) && !name.includes(q) && !city.includes(q)) continue;
      const distanceKm = haversineDistanceKm(latNum, lonNum, recLat, recLon);
      results.push({
        code: airportCode,
        ...rec,
        distanceKm: Number(distanceKm.toFixed(1)),
      });
    }
    results.sort((a, b) => (a.distanceKm === b.distanceKm ? a.code.localeCompare(b.code) : a.distanceKm - b.distanceKm));
    return results.slice(0, limit);
  }

  if (!q) return [];

  const scored = [];
  for (const [code, rec] of Object.entries(db)) {
    const airportCode = code.toUpperCase();
    const name = String(rec.name || "").toUpperCase();
    const city = String(rec.city || "").toUpperCase();

    if (!airportCode.includes(q) && !name.includes(q) && !city.includes(q)) {
      continue;
    }

    if (airportCode === q) {
      return [{ code: airportCode, ...rec }];
    }

    let score = 100;
    if (city === q || name === q) score = 0;
    else if (city.startsWith(q) || name.startsWith(q)) score = 1;
    else if (airportCode.startsWith(q)) score = 2;
    else if (city.includes(q)) score = 3;
    else if (name.includes(q)) score = 4;
    else score = 5;

    scored.push({ score, code: airportCode, record: rec });
  }

  scored.sort((a, b) => (a.score === b.score ? a.code.localeCompare(b.code) : a.score - b.score));

  return scored.slice(0, limit).map(({ code, record }) => ({ code, ...record }));
}

const WEEKDAY_INDEX = {
  sunday: 0,
  monday: 1,
  tuesday: 2,
  wednesday: 3,
  thursday: 4,
  friday: 5,
  saturday: 6,
};

const MONTH_INDEX = {
  january: 1,
  jan: 1,
  february: 2,
  feb: 2,
  march: 3,
  mar: 3,
  april: 4,
  apr: 4,
  may: 5,
  june: 6,
  jun: 6,
  july: 7,
  jul: 7,
  august: 8,
  aug: 8,
  september: 9,
  sept: 9,
  sep: 9,
  october: 10,
  oct: 10,
  november: 11,
  nov: 11,
  december: 12,
  dec: 12,
};

const PAD = n => String(n).padStart(2, "0");

function makeUtcDate(year, month, day) {
  return new Date(Date.UTC(year, month - 1, day));
}

function isoFromDate(date) {
  return `${date.getUTCFullYear()}-${PAD(date.getUTCMonth() + 1)}-${PAD(date.getUTCDate())}`;
}

function addDays(date, days) {
  const copy = new Date(date.getTime());
  copy.setUTCDate(copy.getUTCDate() + days);
  return copy;
}

function diffInDays(a, b) {
  const ms = a.getTime() - b.getTime();
  return Math.round(ms / 86400000);
}

function parseNumericDate(text) {
  const dotMatch = text.match(/^(\d{1,2})[.\-/ ](\d{1,2})[.\-/ ](\d{2,4})$/);
  if (!dotMatch) return null;
  let [ , dStr, mStr, yStr ] = dotMatch;
  let year = parseInt(yStr, 10);
  const day = parseInt(dStr, 10);
  const month = parseInt(mStr, 10);
  if (year < 100) {
    year += year >= 70 ? 1900 : 2000;
  }
  if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) return null;
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  return makeUtcDate(year, month, day);
}

function getReferenceDate(referenceDate, timeZone) {
  if (referenceDate) {
    const trimmed = referenceDate.trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) {
      const parsed = new Date(`${trimmed}T00:00:00Z`);
      if (!Number.isNaN(parsed.getTime())) return parsed;
    }
    const numeric = parseNumericDate(trimmed);
    if (numeric) return numeric;
  }
  const now = new Date();
  if (timeZone) {
    try {
      const formatter = new Intl.DateTimeFormat("en-CA", {
        timeZone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      });
      const formatted = formatter.format(now); // YYYY-MM-DD
      const parts = formatted.split("-");
      if (parts.length === 3) {
        const year = parseInt(parts[0], 10);
        const month = parseInt(parts[1], 10);
        const day = parseInt(parts[2], 10);
        if (Number.isFinite(year) && Number.isFinite(month) && Number.isFinite(day)) {
          return makeUtcDate(year, month, day);
        }
      }
    } catch (_) {
      logger.warn("Invalid timezone supplied to datetime interpret", { timeZone });
    }
  }
  return makeUtcDate(now.getUTCFullYear(), now.getUTCMonth() + 1, now.getUTCDate());
}

function parseIsoDatePhrase(phrase) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(phrase)) {
    const parsed = new Date(`${phrase}T00:00:00Z`);
    if (!Number.isNaN(parsed.getTime())) {
      return { date: parsed, explanation: "ISO date provided", confidence: 1 };
    }
  }
  return null;
}

function parseNumericPhrase(phrase) {
  const parsed = parseNumericDate(phrase);
  if (!parsed) return null;
  return { date: parsed, explanation: "Numeric date interpreted", confidence: 0.95 };
}

function parseMonthPhrase(phrase, referenceDate) {
  const monthMatch = phrase.match(/^(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+([A-Za-z]+)(?:\s+(\d{4}))?$/i)
    || phrase.match(/^([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(\d{4}))?$/i);
  if (monthMatch) {
    const parts = monthMatch.slice(1).map(p => p ? p.trim() : p);
    const hasLeadingDay = /^\d/.test(parts[0]);
    const day = parseInt(hasLeadingDay ? parts[0] : parts[1], 10);
    const monthToken = hasLeadingDay ? parts[1] : parts[0];
    const yearToken = hasLeadingDay ? parts[2] : parts[2];
    const month = MONTH_INDEX[monthToken?.toLowerCase() || ""];
    if (!month || Number.isNaN(day) || day < 1 || day > 31) return null;
    let year = yearToken ? parseInt(yearToken, 10) : referenceDate.getUTCFullYear();
    if (!Number.isFinite(year)) year = referenceDate.getUTCFullYear();
    let candidate = makeUtcDate(year, month, Math.min(day, 31));
    if (diffInDays(candidate, referenceDate) < 0) {
      candidate = makeUtcDate(year + 1, month, Math.min(day, 31));
    }
    return { date: candidate, explanation: "Month and day interpreted", confidence: 0.9 };
  }
  const monthOnly = phrase.match(/^([A-Za-z]+)$/i);
  if (monthOnly) {
    const month = MONTH_INDEX[monthOnly[1].toLowerCase()];
    if (!month) return null;
    let year = referenceDate.getUTCFullYear();
    let candidate = makeUtcDate(year, month, 1);
    if (diffInDays(candidate, referenceDate) < 0) {
      candidate = makeUtcDate(year + 1, month, 1);
    }
    return { date: candidate, explanation: "Month interpreted as first day", confidence: 0.6 };
  }
  return null;
}

function parseWeekdayPhrase(phrase, referenceDate) {
  const tokens = phrase.split(/\s+/).map(t => t.toLowerCase());
  const weekdayToken = tokens.find(t => WEEKDAY_INDEX.hasOwnProperty(t));
  if (!weekdayToken) return null;
  const targetIndex = WEEKDAY_INDEX[weekdayToken];
  const hasNext = tokens.includes("next") || tokens.includes("upcoming") || tokens.includes("following");
  const hasThis = tokens.includes("this");
  const referenceDow = referenceDate.getUTCDay();
  let delta = (targetIndex - referenceDow + 7) % 7;
  if (hasNext || delta === 0) {
    delta += 7;
  } else if (hasThis && delta !== 0) {
    // keep delta as-is (within the same week)
  } else if (!hasNext && delta === 0) {
    delta = 7;
  }
  const candidate = addDays(referenceDate, delta);
  return { date: candidate, explanation: `Next occurrence of ${weekdayToken}`, confidence: 0.85 };
}

function parseRelativePhrase(phrase, referenceDate) {
  const lower = phrase.toLowerCase();
  if (lower === "today") {
    return { date: referenceDate, explanation: "Today", confidence: 0.8 };
  }
  if (lower === "tomorrow") {
    return { date: addDays(referenceDate, 1), explanation: "Tomorrow", confidence: 0.8 };
  }
  if (lower === "day after tomorrow") {
    return { date: addDays(referenceDate, 2), explanation: "Day after tomorrow", confidence: 0.8 };
  }
  const inDays = lower.match(/^in\s+(\d{1,2})\s+days?$/);
  if (inDays) {
    const offset = parseInt(inDays[1], 10);
    if (Number.isFinite(offset)) {
      return { date: addDays(referenceDate, offset), explanation: `In ${offset} days`, confidence: 0.75 };
    }
  }
  return null;
}

function rollForwardRecentPast(candidate, referenceDate, threshold = 6) {
  if (diffInDays(candidate, referenceDate) < 0) {
    const delta = diffInDays(referenceDate, candidate);
    if (delta > 0 && delta <= threshold) {
      return { date: addDays(candidate, 7), explanation: "Rolled forward to upcoming week" };
    }
  }
  return null;
}

function interpretDatePhrase({ phrase, referenceDate, timeZone }) {
  const original = typeof phrase === "string" ? phrase.trim() : "";
  if (!original) {
    return { success: false, reason: "empty_phrase" };
  }
  const reference = getReferenceDate(referenceDate ? String(referenceDate) : null, timeZone ? String(timeZone) : undefined);

  const attempts = [
    parseIsoDatePhrase,
    parseNumericPhrase,
    (p) => parseMonthPhrase(p, reference),
    (p) => parseWeekdayPhrase(p, reference),
    (p) => parseRelativePhrase(p, reference),
  ];

  let best = null;
  for (const attempt of attempts) {
    const result = attempt(original);
    if (result && result.date) {
      best = result;
      break;
    }
  }

  if (!best) {
    return { success: false, reason: "unrecognised_phrase" };
  }

  let candidate = best.date;
  const rollForward = rollForwardRecentPast(candidate, reference);
  let explanation = best.explanation;
  if (rollForward) {
    candidate = rollForward.date;
    explanation = `${explanation}; ${rollForward.explanation}`;
  }

  const isoDate = isoFromDate(candidate);
  return {
    success: true,
    isoDate,
    confidence: best.confidence ?? 0.7,
    explanation,
  };
}

const server = http.createServer(async (req, res) => {
  const requestId = crypto.randomUUID();
  const startedAt = Date.now();
  logger.info(`[${requestId}] ${req.method} ${req.url}`);

  if (!enforceCors(req, res)) {
    return;
  }

  if (req.method === "OPTIONS") {
    res.statusCode = 204;
    setCorsHeaders(res);
    res.end();
    logger.info(`[${requestId}] Completed OPTIONS in ${Date.now() - startedAt}ms`);
    return;
  }

  let parsedUrl;
  try {
    parsedUrl = new url.URL(req.url, "http://localhost");
  } catch (error) {
    res.statusCode = 400;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "invalid_url" }));
    logger.warn(`[${requestId}] Invalid request URL`, { url: req.url, message: error.message });
    return;
  }

  const { pathname, searchParams } = parsedUrl;

  try {
    if (req.method === "POST" && pathname === "/invoke") {
      let body;
      try {
        body = await readBody(req);
      } catch (error) {
        const status = error.message === "Payload too large" ? 413 : 400;
        res.statusCode = status;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: error.message }));
        logger.warn(`[${requestId}] Invalid request body`, { message: error.message });
        return;
      }
      const sessionId = body.sessionId || `sess-${crypto.randomUUID()}`;
      const inputText = typeof body.inputText === "string" ? body.inputText : String(body.inputText ?? "");
      const defaultOriginRaw = typeof body.defaultOrigin === "string" ? body.defaultOrigin.trim() : "";
      const defaultOrigin = defaultOriginRaw.toUpperCase();
      const locationLabel = typeof body.locationLabel === "string" ? body.locationLabel.trim() : "";
      const promptAttributes = {};
      const sessionAttributes = {};
      if (defaultOrigin) {
        sessionAttributes.default_origin = defaultOrigin;
        promptAttributes.default_origin = defaultOrigin;
      }
      if (locationLabel) {
        promptAttributes.default_origin_label = locationLabel;
      }
      const data = await awsInvokeAgent({
        aliasId: AGENT_ALIAS_ID,
        sessionId,
        inputText,
        sessionAttributes: Object.keys(sessionAttributes).length > 0 ? sessionAttributes : undefined,
        promptSessionAttributes: Object.keys(promptAttributes).length > 0 ? promptAttributes : undefined,
      });
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(data));
      logger.info(`[${requestId}] Agent invocation succeeded`, { sessionId });
      return;
    }

    if (req.method === "POST" && pathname === "/tools/amadeus/search") {
      if (!AMADEUS_API_KEY || !AMADEUS_API_SECRET) {
        res.statusCode = 503;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: "amadeus_not_configured" }));
        logger.warn(`[${requestId}] Amadeus search attempted without credentials`);
        return;
      }
      let q;
      try {
        q = await readBody(req);
      } catch (error) {
        const status = error.message === "Payload too large" ? 413 : 400;
        res.statusCode = status;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: error.message }));
        logger.warn(`[${requestId}] Invalid Amadeus request body`, { message: error.message });
        return;
      }
      if (typeof q !== "object" || q === null) {
        res.statusCode = 400;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: "invalid_payload" }));
        logger.warn(`[${requestId}] Amadeus payload not an object`);
        return;
      }
      const requiredFields = ["originLocationCode", "destinationLocationCode", "departureDate"];
      const missing = requiredFields.filter(field => !q[field]);
      if (missing.length > 0) {
        res.statusCode = 400;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: "missing_fields", fields: missing }));
        logger.warn(`[${requestId}] Amadeus payload missing fields`, { missing });
        return;
      }
      const token = await amadeusToken();
      const query = buildSearchQuery(q);
      const am = await fetch(`${AMADEUS_HOST}/v2/shopping/flight-offers?${query}`, { headers: { Authorization: `Bearer ${token}` } });
      const json = await am.json();
      if (!am.ok) {
        res.statusCode = am.status;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(json));
        logger.warn(`[${requestId}] Amadeus search failed`, { status: am.status });
        return;
      }
      const normalized = normalizeOffers(json);
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(normalized));
      logger.info(`[${requestId}] Amadeus search succeeded`);
      return;
    }

    if (req.method === "POST" && pathname === "/log/transcript") {
      if (!transcriptS3Client || !TRANSCRIPT_BUCKET) {
        res.statusCode = 503;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: "transcript_logging_disabled" }));
        logger.warn(`[${requestId}] Transcript upload attempted without S3 configuration`);
        return;
      }
      let payload;
      try {
        payload = await readBody(req);
      } catch (error) {
        const status = error.message === "Payload too large" ? 413 : 400;
        res.statusCode = status;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: error.message }));
        logger.warn(`[${requestId}] Invalid transcript payload`, { message: error.message });
        return;
      }
      const validated = validateTranscriptPayload(payload);
      if (!validated.ok) {
        res.statusCode = 400;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: validated.error }));
        logger.warn(`[${requestId}] Transcript validation failed`, { reason: validated.error });
        return;
      }
      const key = buildTranscriptKey(validated);
      const objectBody = JSON.stringify({
        schemaVersion: validated.schemaVersion,
        persona: validated.persona,
        variant: validated.variant,
        sessionId: validated.sessionId,
        flight: validated.flight || null,
        startedAt: validated.startedAt,
        completedAt: validated.completedAt,
        location: validated.location,
        messages: validated.messages,
        extra: validated.raw.extra ?? null,
      });
      const metadata = {
        persona: validated.persona,
        variant: validated.variant,
        sessionid: validated.sessionId.slice(0, MAX_SESSION_SEGMENT),
        schemaversion: validated.schemaVersion,
      };
      try {
        await transcriptS3Client.send(new PutObjectCommand({
          Bucket: TRANSCRIPT_BUCKET,
          Key: key,
          Body: objectBody,
          ContentType: "application/json",
          ServerSideEncryption: "AES256",
          Metadata: metadata,
        }));
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ ok: true, key }));
        logger.info(`[${requestId}] Transcript stored`, { key, persona: validated.persona });
      } catch (error) {
        logger.error(`[${requestId}] Transcript upload failed`, { message: error.message });
        res.statusCode = 502;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: "transcript_upload_failed" }));
      }
      return;
    }

    if (req.method === "GET" && pathname === "/tools/iata/lookup") {
      const term = searchParams.get("term") || "";
      const latStr = searchParams.get("lat");
      const lonStr = searchParams.get("lon");
      const limitStr = searchParams.get("limit");
      const lat = latStr === null ? undefined : Number(latStr);
      const lon = lonStr === null ? undefined : Number(lonStr);
      const limit = limitStr === null ? undefined : Number(limitStr);
      const matches = iataLookup({
        term,
        lat: Number.isFinite(lat) ? lat : undefined,
        lon: Number.isFinite(lon) ? lon : undefined,
        limit: Number.isFinite(limit) && limit > 0 ? limit : 20,
      });
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ matches }));
      logger.info(`[${requestId}] IATA lookup returned ${matches.length} results`);
      return;
    }

    if (req.method === "POST" && pathname === "/tools/datetime/interpret") {
      let body;
      try {
        body = await readBody(req);
      } catch (error) {
        res.statusCode = 400;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: error.message }));
        logger.warn(`[${requestId}] Invalid datetime interpret payload`, { message: error.message });
        return;
      }
      const phrase = typeof body.phrase === "string" ? body.phrase : "";
      const referenceDate = typeof body.referenceDate === "string" ? body.referenceDate : undefined;
      const timeZone = typeof body.timeZone === "string" ? body.timeZone : (typeof body.timezone === "string" ? body.timezone : undefined);
      if (!phrase.trim()) {
        res.statusCode = 400;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: "phrase_required" }));
        logger.warn(`[${requestId}] datetime interpret missing phrase`);
        return;
      }
      const result = interpretDatePhrase({ phrase, referenceDate, timeZone });
      if (!result.success) {
        res.statusCode = 422;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: result.reason || "unrecognised_phrase" }));
        logger.warn(`[${requestId}] datetime interpret failed`, { phrase, reason: result.reason });
        return;
      }
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({
        phrase,
        referenceDate: referenceDate || null,
        timeZone: timeZone || null,
        isoDate: result.isoDate,
        confidence: result.confidence,
        explanation: result.explanation,
      }));
      logger.info(`[${requestId}] datetime interpret succeeded`, { phrase, isoDate: result.isoDate });
      return;
    }

    if (req.method === "GET" && pathname === "/health") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ ok: true, time: new Date().toISOString() }));
      return;
    }

    res.statusCode = 404;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "not_found" }));
    logger.warn(`[${requestId}] Route not found`, { method: req.method, path: pathname });
  } catch (err) {
    logger.error(`[${requestId}] Request processing failed`, {
      message: err.message,
      stack: err.stack ? err.stack.split("\n").slice(0, 5).join(" | ") : undefined,
    });
    if (!res.headersSent) {
      res.statusCode = res.statusCode >= 400 ? res.statusCode : 500;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: "internal_error", requestId }));
    }
  } finally {
    logger.info(`[${requestId}] Completed with status ${res.statusCode} in ${Date.now() - startedAt}ms`);
  }
});

const entryPath = process.argv[1] ? path.resolve(process.argv[1]) : null;
const modulePath = path.resolve(url.fileURLToPath(import.meta.url));
const shouldStartServer = entryPath && entryPath === modulePath;

if (shouldStartServer) {
  server.listen(PORT, () => logger.info(`Proxy listening on :${PORT}`, { origins: [...ALLOW_ORIGIN_SET] }));
} else {
  logger.info("Proxy server initialization skipped (module import mode)");
}

export { iataLookup, loadIata, interpretDatePhrase };

