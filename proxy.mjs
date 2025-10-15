// proxy.mjs — Render backend for Bedrock Agent + Amadeus adapter + IATA lookup
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import url from "node:url";
import crypto from "node:crypto";

const PORT = process.env.PORT || 8787;
const REGION = process.env.AWS_REGION || "us-west-2";
const AGENT_ID = process.env.AGENT_ID;
const ALIAS_IDS = {
  plain: process.env.ALIAS_ID_PLAIN,
  gain: process.env.ALIAS_ID_GAIN,
  brain: process.env.ALIAS_ID_BRAIN,
};
const AMADEUS_ENV = process.env.AMADEUS_ENV || "test";
const AMADEUS_HOST =
  AMADEUS_ENV === "production"
    ? "https://api.amadeus.com"
    : "https://test.api.amadeus.com";
const AMADEUS_API_KEY = process.env.AMADEUS_API_KEY || "";
const AMADEUS_API_SECRET = process.env.AMADEUS_API_SECRET || "";
const IATA_DB_PATH = process.env.IATA_DB_PATH || "./iata.json";
const ALLOW_ORIGINS = (process.env.CORS_ORIGINS || "").split(",").map(s => s.trim()).filter(Boolean);

function cors(res) {
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
}
function aliasFromHost(host) {
  if (!host) return "plain";
  if (host.startsWith("gina.")) return "gain";
  if (host.startsWith("bianca.")) return "brain";
  if (host.startsWith("paul.")) return "plain";
  return "plain";
}
function withCors(req, res) {
  const origin = req.headers.origin || "";
  if (ALLOW_ORIGINS.length === 0) res.setHeader("Access-Control-Allow-Origin", origin || "*");
  else if (ALLOW_ORIGINS.includes(origin)) res.setHeader("Access-Control-Allow-Origin", origin);
  cors(res);
}
async function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", chunk => (data += chunk));
    req.on("end", () => { try { resolve(data ? JSON.parse(data) : {}); } catch (e) { reject(e); } });
    req.on("error", reject);
  });
}

// ---- Minimal SigV4 for Bedrock Agent Runtime ----
function sha256(buf) { return crypto.createHash("sha256").update(buf).digest("hex"); }
function hmac(key, str) { return crypto.createHmac("sha256", key).update(str).digest(); }
function signV4({ service, region, method, hostname, path, headers, body, accessKeyId, secretAccessKey, sessionToken }) {
  const now = new Date();
  const amzDate = now.toISOString().replace(/[:-]|\.\d{3}/g, "");
  const dateStamp = amzDate.slice(0, 8);
  const canonicalHeaders = Object.entries(headers)
    .map(([k, v]) => [k.toLowerCase().trim(), String(v).trim()])
    .sort((a,b) => a[0] < b[0] ? -1 : 1)
    .map(([k, v]) => `${k}:${v}\n`).join("");
  const signedHeaders = Object.keys(headers).map(k => k.toLowerCase()).sort().join(";");
  const payloadHash = sha256(body || "");
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
  return { amzDate, authorization };
}
async function awsInvokeAgent({ aliasId, sessionId, inputText }) {
  const accessKeyId = process.env.AWS_ACCESS_KEY_ID;
  const secretAccessKey = process.env.AWS_SECRET_ACCESS_KEY;
  const sessionToken = process.env.AWS_SESSION_TOKEN;
  const service = "bedrock-agent-runtime";
  const hostname = `${service}.${REGION}.amazonaws.com`;
  const path = `/agents/${AGENT_ID}/agentaliases/${aliasId}/sessions/${encodeURIComponent(sessionId)}/text`;
  const body = JSON.stringify({ inputText });
  const headers = { "content-type": "application/json", "host": hostname, "x-amz-date": "" };
  if (sessionToken) headers["x-amz-security-token"] = sessionToken;
  const { amzDate, authorization } = signV4({ service, region: REGION, method: "POST", hostname, path, headers, body, accessKeyId, secretAccessKey, sessionToken });
  headers["x-amz-date"] = amzDate; headers["authorization"] = authorization;
  const resp = await fetch(`https://${hostname}${path}`, { method: "POST", headers, body });
  if (!resp.ok) throw new Error(`InvokeAgent failed: ${resp.status} ${await resp.text()}`);
  const data = await resp.json().catch(async () => ({ raw: await resp.text() }));
  return data;
}

// ---- Amadeus adapter ----
let tokenCache = { token: null, expiresAt: 0 };
async function amadeusToken() {
  const now = Date.now();
  if (tokenCache.token && now < tokenCache.expiresAt - 30000) return tokenCache.token;
  const form = new URLSearchParams();
  form.set("grant_type", "client_credentials");
  form.set("client_id", AMADEUS_API_KEY);
  form.set("client_secret", AMADEUS_API_SECRET);
  const resp = await fetch(`${AMADEUS_HOST}/v1/security/oauth2/token`, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: form.toString() });
  const json = await resp.json();
  if (!resp.ok) throw new Error(`Amadeus token error: ${resp.status} ${JSON.stringify(json)}`);
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
  try { IATA_DB = JSON.parse(fs.readFileSync(path.resolve(IATA_DB_PATH), "utf8")); } catch { IATA_DB = {}; }
  return IATA_DB;
}
function iataLookup(term) {
  const db = loadIata();
  const q = String(term || "").toUpperCase();
  const out = [];
  for (const [code, rec] of Object.entries(db)) {
    if (code.startsWith(q) || (rec.name || "").toUpperCase().includes(q) || (rec.city || "").toUpperCase().includes(q)) {
      out.push({ code, ...rec });
      if (out.length >= 20) break;
    }
  }
  return out;
}

const server = http.createServer(async (req, res) => {
  const origin = req.headers.origin || "";
  if (!process.env.CORS_ORIGINS || process.env.CORS_ORIGINS.split(",").includes(origin)) {
    res.setHeader("Access-Control-Allow-Origin", origin || "*");
  }
  cors(res);
  if (req.method === "OPTIONS") { res.statusCode = 204; res.end(); return; }
  const { pathname, searchParams } = new url.URL(req.url, "http://localhost");
  try {
    if (req.method === "POST" && pathname === "/invoke") {
      const body = await readBody(req);
      const host = (req.headers["x-forwarded-host"] || req.headers.host || "").toLowerCase();
      const alias = body.alias || aliasFromHost(host);
      const aliasId = ALIAS_IDS[alias];
      if (!aliasId) throw new Error(`Unknown alias '${alias}'. Set ALIAS_ID_${alias.toUpperCase()}`);
      const sessionId = body.sessionId || "sess-" + Math.random().toString(36).slice(2);
      const inputText = body.inputText || "";
      const data = await awsInvokeAgent({ aliasId, sessionId, inputText });
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(data));
      return;
    }
    if (req.method === "POST" && pathname === "/tools/amadeus/search") {
      const q = await readBody(req);
      const token = await amadeusToken();
      const query = buildSearchQuery(q);
      const am = await fetch(`${AMADEUS_HOST}/v2/shopping/flight-offers?${query}`, { headers: { Authorization: `Bearer ${token}` } });
      const json = await am.json();
      if (!am.ok) { res.statusCode = am.status; res.setHeader("Content-Type", "application/json"); res.end(JSON.stringify(json)); return; }
      const normalized = normalizeOffers(json);
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(normalized));
      return;
    }
    if (req.method === "GET" && pathname === "/tools/iata/lookup") {
      const term = searchParams.get("term") || "";
      const matches = iataLookup(term);
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ matches }));
      return;
    }
    if (req.method === "GET" && pathname === "/health") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ ok: true, time: new Date().toISOString() }));
      return;
    }
    res.statusCode = 404; res.end("Not found");
  } catch (err) {
    res.statusCode = 500;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: String(err.message || err) }));
  }
});
server.listen(PORT, () => console.log(`Proxy listening on :${PORT}`));
