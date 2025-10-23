// services/proxy.mjs — One-agent, three-alias Return‑Control proxy (provider decided at build time via agent instructions)
// Forwards: /tools/* -> TOOLS_BASE_URL (origin-daisy), /google/* -> GOOGLE_BASE_URL (google-api-daisy)
// No runtime provider switch. The chosen provider is hard-coded in the agent's instructions.

import express from "express";
import fs from "node:fs";
import path from "node:path";
import { BedrockAgentRuntimeClient, InvokeAgentCommand } from "@aws-sdk/client-bedrock-agent-runtime";

const {
  AWS_REGION = "us-west-¸2",
  SUPERVISOR_AGENT_ID,
  SUPERVISOR_AGENT_ALIAS_ID,
  AGENT_ID,
  AGENT_ALIAS_ID,
  PORT = 8787,
  ALLOWED_ORIGINS = "*",
  TOOLS_BASE_URL = "https://origin-daisy.onrender.com",
  GOOGLE_BASE_URL = "https://google-api-daisy.onrender.com",
  FORWARD_TOOLS = "true",
  IATA_DB_PATH = "./iata.json",
} = process.env;

const AGENT = SUPERVISOR_AGENT_ID || AGENT_ID;
const ALIAS = SUPERVISOR_AGENT_ALIAS_ID || AGENT_ALIAS_ID;
const client = new BedrockAgentRuntimeClient({ region: AWS_REGION });

async function httpCall(base, method, path, paramsOrBody={}) {
  const url = new URL(path, base);
  const opts = { method, headers: {} };
  if (method === "GET") {
    Object.entries(paramsOrBody || {}).forEach(([k,v]) => v!=null && url.searchParams.set(k, String(v)));
  } else {
    opts.headers["content-type"] = "application/json";
    opts.body = JSON.stringify(paramsOrBody || {});
  }
  const r = await fetch(url, opts);
  const t = await r.text();
  if (!r.ok) throw new Error(`${method} ${url} -> ${r.status} ${t.slice(0,200)}`);
  try { return JSON.parse(t); } catch { return { ok:false, text:t }; }
}

// ---------------- IATA lookup helpers ----------------
const EARTH_RADIUS_KM = 6371;
let IATA_DATA = null;
let IATA_LIST = null;

function toNumber(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num : undefined;
}

function loadIataData() {
  if (IATA_DATA && IATA_LIST) return;
  const resolved = path.resolve(IATA_DB_PATH);
  try {
    const raw = fs.readFileSync(resolved, "utf8");
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      IATA_DATA = parsed;
      IATA_LIST = Object.entries(parsed).map(([code, record]) => ({
        code: String(code || "").toUpperCase(),
        name: record?.name || "",
        city: record?.city || "",
        country: record?.country || "",
        type: record?.type || "",
        state: record?.state || "",
        timezone: record?.timezone || "",
        icao: record?.icao || "",
        latitude: toNumber(record?.latitude),
        longitude: toNumber(record?.longitude),
      }));
    } else {
      IATA_DATA = {};
      IATA_LIST = [];
    }
  } catch (error) {
    console.warn("[proxy] IATA load failed", { file: resolved, message: error?.message });
    IATA_DATA = {};
    IATA_LIST = [];
  }
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const rLat1 = lat1 * Math.PI / 180;
  const rLat2 = lat2 * Math.PI / 180;
  const a = Math.sin(dLat/2) ** 2 + Math.sin(dLon/2) ** 2 * Math.cos(rLat1) * Math.cos(rLat2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
  return EARTH_RADIUS_KM * c;
}

function normalizeTerm(payload = {}) {
  const term =
    payload.term ??
    payload.code ??
    payload.query ??
    payload.q ??
    "";
  return String(term || "").trim().toUpperCase();
}

function parseLimit(value, fallback = 20) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(Math.round(n), 50);
}

function shapeResult(entry, distanceKm) {
  const result = {
    code: entry.code,
    name: entry.name,
    city: entry.city,
    country: entry.country,
    type: entry.type,
    state: entry.state,
    timezone: entry.timezone,
    icao: entry.icao,
  };
  if (entry.latitude !== undefined) result.latitude = entry.latitude;
  if (entry.longitude !== undefined) result.longitude = entry.longitude;
  if (distanceKm !== undefined) result.distanceKm = Number(distanceKm.toFixed(1));
  return result;
}

function iataLookup(payload = {}) {
  loadIataData();
  const list = Array.isArray(IATA_LIST) ? IATA_LIST : [];

  const term = normalizeTerm(payload);
  const limit = parseLimit(payload.limit, 20);
  const lat = toNumber(payload.lat ?? payload.latitude);
  const lon = toNumber(payload.lon ?? payload.longitude);
  const hasCoords = lat !== undefined && lon !== undefined;

  if (hasCoords) {
    const nearest = [];
    const pushNearest = (entry, distance) => {
      nearest.push({ distance, entry });
      nearest.sort((a, b) => a.distance - b.distance);
      while (nearest.length > limit) nearest.pop();
    };
    const termFilter = term ? term : null;

    for (const entry of list) {
      if (entry.type && entry.type.toLowerCase() !== "airport") continue;
      if (entry.latitude === undefined || entry.longitude === undefined) continue;
      if (termFilter) {
        const code = entry.code;
        const city = entry.city.toUpperCase();
        const name = entry.name.toUpperCase();
        if (!code.includes(termFilter) && !city.includes(termFilter) && !name.includes(termFilter)) continue;
      }
      const distance = haversineKm(lat, lon, entry.latitude, entry.longitude);
      pushNearest(entry, distance);
    }

    return nearest.map(({ entry, distance }) => shapeResult(entry, distance));
  }

  if (!term) return [];

  const scored = [];
  for (const entry of list) {
    const code = entry.code;
    const city = entry.city.toUpperCase();
    const name = entry.name.toUpperCase();

    if (!code.includes(term) && !city.includes(term) && !name.includes(term)) continue;

    if (code === term) return [shapeResult(entry)];

    let score = 100;
    if (city === term || name === term) score = 0;
    else if (city.startsWith(term) || name.startsWith(term)) score = 1;
    else if (code.startsWith(term)) score = 2;
    else if (city.includes(term)) score = 3;
    else if (name.includes(term)) score = 4;
    else score = 5;

    scored.push({ score, entry });
  }

  scored.sort((a, b) => (a.score === b.score ? a.entry.code.localeCompare(b.entry.code) : a.score - b.score));
  return scored.slice(0, limit).map(({ entry }) => shapeResult(entry));
}

async function invokeOnce({ sessionId, text, sessionState }) {
  const cmd = new InvokeAgentCommand({
    agentId: AGENT,
    agentAliasId: ALIAS,
    sessionId,
    inputText: text ?? "",
    enableTrace: true,
    sessionState
  });
  const resp = await client.send(cmd);
  const acc = { text: "", rc: null };
  for await (const ev of resp.completion) {
    if (ev?.chunk?.bytes) acc.text += Buffer.from(ev.chunk.bytes).toString("utf8");
    if (ev?.returnControl) acc.rc = ev.returnControl;
  }
  return acc;
}

function rcResults(invocationId, inputs, results) {
  return [{
    invocationId,
    returnControlInvocationResults: results.map((r, i) => ({
      actionGroup: inputs[i]?.actionGroup || "unknown",
      apiPath: inputs[i]?.apiPath || "unknown",
      httpMethod: inputs[i]?.httpMethod || "POST",
      result: r
    }))
  }];
}

async function executeInput(input) {
  const path = input.apiPath || input.endpoint || input.operation || "";
  const method = (input.httpMethod || input.method || "post").toUpperCase();
  const q = input.parameters || input.query || {};
  const b = input.requestBody || input.body || {};

  if (path.startsWith("/tools/iata/lookup")) {
    const payload = method === "GET" ? q : b;
    return { matches: iataLookup(payload) };
  }
  if (path.startsWith("/tools/")) {
    const verb = method === "GET" ? "GET" : "POST";
    return await httpCall(TOOLS_BASE_URL, verb, path, verb==="GET"?q:b);
  }
  if (path.startsWith("/google/")) {
    const verb = method === "GET" ? "GET" : "POST";
    return await httpCall(GOOGLE_BASE_URL, verb, path, verb==="GET"?q:b);
  }
  // Fallback to tools base
  const verb = method === "GET" ? "GET" : "POST";
  return await httpCall(TOOLS_BASE_URL, verb, path, verb==="GET"?q:b);
}

export async function handleChat({ sessionId, text, persona={} }) {
  let sid = sessionId || String(Date.now());
  let state = { attributes: { persona } };
  let out = "";
  for (let hop=0; hop<6; hop++) {
    const { text: chunk, rc } = await invokeOnce({ sessionId: sid, text: hop===0?text:"", sessionState: state });
    if (chunk) out += chunk;
    if (!rc) break;
    const invId = rc.invocationId;
    const inputs = rc.invocationInputs || [];
    const results = [];
    for (const inp of inputs) {
      try { results.push({ ok:true, data: await executeInput(inp) }); }
      catch (e) { results.push({ ok:false, error:String(e) }); }
    }
    state = { ...state, returnControlInvocationResults: rcResults(invId, inputs, results) };
  }
  return { text: out.trim() };
}

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

app.get("/healthz",(req,res)=>res.json({ok:true, agent:AGENT, alias:ALIAS}));
app.post("/invoke", async (req,res)=>{
  try { res.json(await handleChat(req.body||{})); }
  catch(e){ res.status(500).json({ ok:false, error:String(e) }); }
});

app.get("/tools/iata/lookup", (req, res) => {
  try {
    const matches = iataLookup(req.query || {});
    res.set("Cache-Control", "public, max-age=300");
    res.json({ matches });
  } catch (error) {
    console.error("[proxy] /tools/iata/lookup GET failed", error);
    res.status(500).json({ error: "iata_lookup_failed" });
  }
});

app.post("/tools/iata/lookup", (req, res) => {
  try {
    const matches = iataLookup(req.body || {});
    res.set("Cache-Control", "public, max-age=300");
    res.json({ matches });
  } catch (error) {
    console.error("[proxy] /tools/iata/lookup POST failed", error);
    res.status(500).json({ error: "iata_lookup_failed" });
  }
});

app.head("/tools/iata/lookup", (_req, res) => res.status(200).end());

// Optional: forward /tools/* to TOOLS_BASE_URL for front-ends
if (/^true$/i.test(FORWARD_TOOLS||"true")) {
  app.all("/tools/*", async (req,res,next)=>{
    if (req.path === "/tools/iata/lookup") return next();
    try {
      const target = new URL(req.originalUrl, TOOLS_BASE_URL);
      const r = await fetch(target, { method:req.method, headers:{ "content-type":req.headers["content-type"]||"application/json" }, body: req.method==="GET"?undefined:JSON.stringify(req.body||{}) });
      const t = await r.text(); res.status(r.status).set("content-type", r.headers.get("content-type")||"application/json").send(t);
    } catch (e) { res.status(502).json({ ok:false, error:String(e) }); }
  });
}

app.listen(Number(PORT), ()=>console.log(`[proxy] up on ${PORT}`));
