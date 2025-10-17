// proxy.mjs — Render backend for Bedrock Agent + Amadeus adapter + IATA lookup
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import url from "node:url";
import crypto from "node:crypto";

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
    events.push({ headers, payload: payloadText });
    if (messageType !== "event") continue;
    if (!payloadText) continue;
    let json;
    try {
      json = JSON.parse(payloadText);
    } catch (error) {
      continue;
    }
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
        const decoded = Buffer.from(base64, "base64").toString("utf8");
        if (decoded) chunks.push(decoded);
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
async function awsInvokeAgent({ aliasId, sessionId, inputText }) {
  const service = "bedrock";
  const hostname = `bedrock-agent-runtime.${REGION}.amazonaws.com`;
  const path = `/agents/${encodeURIComponent(AGENT_ID)}/agentAliases/${encodeURIComponent(aliasId)}/sessions/${encodeURIComponent(sessionId)}/text`;
  const body = JSON.stringify({ inputText });
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
function iataLookup(term) {
  const db = loadIata();
  const q = String(term || "").trim().toUpperCase();
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

  return scored.slice(0, 20).map(({ code, record }) => ({ code, ...record }));
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
      const data = await awsInvokeAgent({ aliasId: AGENT_ALIAS_ID, sessionId, inputText });
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

    if (req.method === "GET" && pathname === "/tools/iata/lookup") {
      const term = searchParams.get("term") || "";
      const matches = iataLookup(term);
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ matches }));
      logger.info(`[${requestId}] IATA lookup returned ${matches.length} results`);
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
server.listen(PORT, () => logger.info(`Proxy listening on :${PORT}`, { origins: [...ALLOW_ORIGIN_SET] }));

