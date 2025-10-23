// services/proxy.mjs — Return‑Control proxy compatible with extended (22 APIs) schema
// Node 18+ (ESM). Exposes /invoke and optional /tools/* forwarders.
// Routes Google endpoints individually (no aggregation).

import express from "express";
import fs from "node:fs";
import path from "node:path";
import { BedrockAgentRuntimeClient, InvokeAgentCommand } from "@aws-sdk/client-bedrock-agent-runtime";

const {
  AWS_REGION = "us-east-1",
  SUPERVISOR_AGENT_ID,
  SUPERVISOR_AGENT_ALIAS_ID,
  AGENT_ID,
  AGENT_ALIAS_ID,
  PORT = 8787,
  ALLOWED_ORIGINS = "*",
  TOOLS_BASE_URL = "https://origin-daisy.onrender.com",
  GOOGLE_BASE_URL = "https://google-api-daisy.onrender.com",
  IATA_DB_PATH = "./iata.json",
  FORWARD_TOOLS = "true",
  BRAND_SCOPE = "ANY",
  SINGLE_AIRLINE = "LH"
} = process.env;

const AGENT = SUPERVISOR_AGENT_ID || AGENT_ID;
const ALIAS = SUPERVISOR_AGENT_ALIAS_ID || AGENT_ALIAS_ID;
const client = new BedrockAgentRuntimeClient({ region: AWS_REGION });

// Brand scoping
const LHG = ["LH","LX","OS","SN","EW","4Y","EN"];
const STAR = ["A3","AC","AD","AI","AV","BR","CA","CM","ET","EW","KP","LH","LO","LX","NH","NZ","OS","OZ","OU","SA","SK","SN","SQ","TP","TK","UA"];
function scopeCodes() {
  if (BRAND_SCOPE === "LH_GROUP") return LHG.join(",");
  if (BRAND_SCOPE === "STAR_ALLIANCE") return STAR.join(",");
  if (BRAND_SCOPE === "SINGLE_AIRLINE") return (SINGLE_AIRLINE || "LH").toUpperCase();
  return "";
}

// IATA lookup helpers
const EARTH_RADIUS_KM = 6371;
const DEG_TO_RAD = Math.PI / 180;
let IATA_DB = null;

function loadIata() {
  if (IATA_DB) return IATA_DB;
  const resolved = path.resolve(IATA_DB_PATH);
  try {
    const raw = fs.readFileSync(resolved, "utf8");
    IATA_DB = JSON.parse(raw);
  } catch (error) {
    console.warn("[proxy] failed to load IATA database", { file: resolved, message: error?.message });
    IATA_DB = {};
  }
  return IATA_DB;
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const dLat = (lat2 - lat1) * DEG_TO_RAD;
  const dLon = (lon2 - lon1) * DEG_TO_RAD;
  const rLat1 = lat1 * DEG_TO_RAD;
  const rLat2 = lat2 * DEG_TO_RAD;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.sin(dLon / 2) ** 2 * Math.cos(rLat1) * Math.cos(rLat2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return EARTH_RADIUS_KM * c;
}

function normalizeQueryTerm(payload = {}) {
  const term =
    payload.term ??
    payload.code ??
    payload.q ??
    payload.query ??
    "";
  return String(term || "").trim();
}

function parseLimit(value, fallback = 20) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(Math.round(n), 50);
}

function toNumber(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num : undefined;
}

function shapeResult(code, record, distanceKm) {
  const output = {
    code,
    name: record?.name ?? "",
    city: record?.city ?? "",
    country: record?.country ?? "",
    latitude: record?.latitude ?? null,
    longitude: record?.longitude ?? null,
    timezone: record?.timezone ?? null,
  };
  if (distanceKm != null) {
    output.distanceKm = Number(distanceKm);
  }
  if (record?.type) output.type = record.type;
  if (record?.state) output.state = record.state;
  if (record?.icao) output.icao = record.icao;
  return output;
}

function iataLookup(payload = {}) {
  const db = loadIata();
  if (!db || typeof db !== "object") return [];

  const qRaw = normalizeQueryTerm(payload).toUpperCase();
  const limit = parseLimit(payload.limit, 20);
  const lat = toNumber(payload.lat ?? payload.latitude);
  const lon = toNumber(payload.lon ?? payload.longitude);
  const hasCoords = lat !== undefined && lon !== undefined;
  const results = [];

  if (hasCoords) {
    for (const [code, record] of Object.entries(db)) {
      if (!record) continue;
      const recLat = toNumber(record.latitude);
      const recLon = toNumber(record.longitude);
      if (recLat === undefined || recLon === undefined) continue;
      if (qRaw) {
        const codeUpper = code.toUpperCase();
        const name = String(record.name || "").toUpperCase();
        const city = String(record.city || "").toUpperCase();
        if (
          !codeUpper.includes(qRaw) &&
          !name.includes(qRaw) &&
          !city.includes(qRaw)
        ) {
          continue;
        }
      }
      const distanceKm = haversineKm(lat, lon, recLat, recLon);
      results.push({
        code: code.toUpperCase(),
        record,
        distanceKm: Number(distanceKm.toFixed(1)),
      });
    }
    results.sort((a, b) => {
      if (a.distanceKm === b.distanceKm) {
        return a.code.localeCompare(b.code);
      }
      return a.distanceKm - b.distanceKm;
    });
    return results.slice(0, limit).map((entry) =>
      shapeResult(entry.code, entry.record, entry.distanceKm)
    );
  }

  if (!qRaw) return [];

  const scored = [];
  for (const [code, record] of Object.entries(db)) {
    const codeUpper = code.toUpperCase();
    const name = String(record?.name || "").toUpperCase();
    const city = String(record?.city || "").toUpperCase();

    if (
      !codeUpper.includes(qRaw) &&
      !name.includes(qRaw) &&
      !city.includes(qRaw)
    ) {
      continue;
    }

    if (codeUpper === qRaw) {
      return [shapeResult(codeUpper, record)];
    }

    let score = 100;
    if (city === qRaw || name === qRaw) score = 0;
    else if (city.startsWith(qRaw) || name.startsWith(qRaw)) score = 1;
    else if (codeUpper.startsWith(qRaw)) score = 2;
    else if (city.includes(qRaw)) score = 3;
    else if (name.includes(qRaw)) score = 4;
    else score = 5;

    scored.push({ score, code: codeUpper, record });
  }

  scored.sort((a, b) => {
    if (a.score === b.score) return a.code.localeCompare(b.code);
    return a.score - b.score;
  });

  return scored.slice(0, limit).map((entry) =>
    shapeResult(entry.code, entry.record)
  );
}

// HTTP wrapper
async function http(base, method, path, data={}) {
  const url = new URL(path, base);
  const opts = { method };
  if (method === "GET") {
    for (const [k,v] of Object.entries(data||{})) if (v!=null) url.searchParams.set(k, String(v));
  } else {
    opts.headers = { "content-type":"application/json" };
    opts.body = JSON.stringify(data||{});
  }
  const r = await fetch(url, opts);
  const txt = await r.text();
  if (!r.ok) throw new Error(`${method} ${url} -> ${r.status} ${txt.slice(0,200)}`);
  try { return JSON.parse(txt); } catch { return { ok:false, raw:txt }; }
}

// RC loop
async function invokeAgentOnce({ sessionId, text, sessionState }) {
  const cmd = new InvokeAgentCommand({
    agentId: AGENT,
    agentAliasId: ALIAS,
    sessionId,
    inputText: text ?? "",
    enableTrace: true,
    sessionState
  });
  const resp = await client.send(cmd);
  const acc = { text:"", rc:null };
  for await (const ev of resp.completion) {
    if (ev?.chunk?.bytes) acc.text += Buffer.from(ev.chunk.bytes).toString("utf8");
    if (ev?.returnControl) acc.rc = ev.returnControl;
  }
  return acc;
}
function rcResults(invocationId, inputs, results) {
  return [{
    invocationId,
    returnControlInvocationResults: results.map((r,i)=> ({
      actionGroup: inputs[i]?.actionGroup || "unknown",
      apiPath: inputs[i]?.apiPath || "unknown",
      httpMethod: inputs[i]?.httpMethod || "POST",
      result: r
    }))
  }];
}

// Input router
async function executeInput(input) {
  const path = input.apiPath || input.operation || "";
  const method = (input.httpMethod || "POST").toUpperCase();
  const params = input.parameters || {};
  const body = input.requestBody || {};

  // Core tools
  if (path.startsWith("/tools/iata/lookup")) {
    const lookupPayload = method === "GET" ? params : body;
    return { matches: iataLookup(lookupPayload) };
  }
  if (path.startsWith("/tools/antiPhaser")) {
    // Prefer POST but support GET shape
    if (method === "GET") return await http(TOOLS_BASE_URL, "GET", "/tools/antiPhaser", params);
    return await http(TOOLS_BASE_URL, "POST", "/tools/antiPhaser", body);
  }
  if (path.startsWith("/tools/amadeus/search")) {
    const payload = { ...(method==="GET"?params:body), max:10 };
    const allow = scopeCodes();
    if (allow) payload.includedAirlineCodes = payload.includedAirlineCodes || allow;
    return await http(TOOLS_BASE_URL, "POST", "/tools/amadeus/search", payload);
  }
  if (path.startsWith("/tools/amadeus/flex")) {
    const q = { ...(method==="GET"?params:body), oneWay:true };
    if (q.limit==null || Number(q.limit)>10) q.limit = 10;
    return await http(TOOLS_BASE_URL, "GET", "/tools/amadeus/flex", q);
  }
  if (path.startsWith("/tools/derDrucker/wannaCandy")) {
    return await http(TOOLS_BASE_URL, "POST", "/tools/derDrucker/wannaCandy", body);
  }
  if (path.startsWith("/tools/derDrucker/generateTickets")) {
    return await http(TOOLS_BASE_URL, "POST", "/tools/derDrucker/generateTickets", body);
  }
  if (path.startsWith("/tools/s3escalator")) {
    return await http(TOOLS_BASE_URL, "POST", "/tools/s3escalator", body);
  }
  if (path.startsWith("/tools/give_me_tools")) {
    return await http(TOOLS_BASE_URL, "GET", "/tools/give_me_tools", params);
  }

  // Google endpoints (expanded)
  if (path.startsWith("/google/flights/")) {
    const sub = path.replace("/google/flights","") || "/search";
    if (method === "GET") return await http(GOOGLE_BASE_URL, "GET", "/google/flights"+sub, params);
    return await http(GOOGLE_BASE_URL, "POST", "/google/flights"+sub, body);
  }
  if (path.startsWith("/google/calendar/")) {
    const sub = path.replace("/google/calendar","") || "/search";
    if (method === "GET") return await http(GOOGLE_BASE_URL, "GET", "/google/calendar"+sub, params);
    return await http(GOOGLE_BASE_URL, "POST", "/google/calendar"+sub, body);
  }
  if (path.startsWith("/google/explore/")) {
    const sub = path.replace("/google/explore","") || "/search";
    if (method === "GET") return await http(GOOGLE_BASE_URL, "GET", "/google/explore"+sub, params);
    return await http(GOOGLE_BASE_URL, "POST", "/google/explore"+sub, body);
  }

  // Fallback to tools base
  if (method === "GET") return await http(TOOLS_BASE_URL, "GET", path, params);
  return await http(TOOLS_BASE_URL, "POST", path, body);
}

export async function handleChat({ sessionId, text, persona={} }) {
  let sid = sessionId || String(Date.now());
  let state = { attributes: { persona } };
  let final = "";
  for (let hop=0; hop<6; hop++) {
    const { text: chunk, rc } = await invokeAgentOnce({ sessionId: sid, text: hop===0?text:"", sessionState: state });
    if (chunk) final += chunk;
    if (!rc) break;
    const inputs = rc.invocationInputs || [];
    const results = [];
    for (const inp of inputs) {
      try { results.push({ ok:true, data: await executeInput(inp) }); }
      catch (e) { results.push({ ok:false, error:String(e) }); }
    }
    state = { ...state, returnControlInvocationResults: rcResults(rc.invocationId, inputs, results) };
  }
  return { text: final.trim() };
}

// HTTP server
const app = express();
app.use(express.json({ limit: "5mb" }));

// CORS
app.use((req,res,next)=>{
  const allow = ALLOWED_ORIGINS==="*" ? "*" : (req.headers.origin || ALLOWED_ORIGINS);
  res.setHeader("Access-Control-Allow-Origin", allow);
  res.setHeader("Access-Control-Allow-Methods","GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers","content-type, authorization");
  if (req.method==="OPTIONS") return res.sendStatus(200);
  next();
});

app.get("/healthz",(req,res)=>res.json({ ok:true, agent:AGENT, alias:ALIAS, toolsBase:TOOLS_BASE_URL, googleBase:GOOGLE_BASE_URL }));

app.get("/tools/iata/lookup", (req, res) => {
  try {
    const matches = iataLookup(req.query || {});
    res.json({ matches });
  } catch (error) {
    console.error("[proxy] /tools/iata/lookup GET failed", error);
    res.status(500).json({ error: "iata_lookup_failed" });
  }
});

app.post("/tools/iata/lookup", (req, res) => {
  try {
    const matches = iataLookup(req.body || {});
    res.json({ matches });
  } catch (error) {
    console.error("[proxy] /tools/iata/lookup POST failed", error);
    res.status(500).json({ error: "iata_lookup_failed" });
  }
});

app.head("/tools/iata/lookup", (_req, res) => {
  res.status(200).end();
});

app.post("/invoke", async (req,res)=>{
  try { res.json(await handleChat(req.body||{})); }
  catch(e){ res.status(500).json({ ok:false, error:String(e) }); }
});

// Optional forwarders so front-ends can hit this host for tools too
if (/^true$/i.test(FORWARD_TOOLS||"true")) {
  app.all("/tools/*", async (req,res)=>{
    try {
      const target = new URL(req.originalUrl, TOOLS_BASE_URL);
      const r = await fetch(target, { method:req.method, headers:{ "content-type": req.headers["content-type"]||"application/json" }, body: req.method==="GET"?undefined:JSON.stringify(req.body||{}) });
      const t = await r.text();
      res.status(r.status).set("content-type", r.headers.get("content-type")||"application/json").send(t);
    } catch (e) { res.status(502).json({ ok:false, error:String(e) }); }
  });
}

app.listen(Number(PORT), ()=>console.log(`[proxy] listening on ${PORT}`));
