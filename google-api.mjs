// services/google-api.mjs — Implements expanded Google endpoints used in the extended OpenAPI
// Endpoints (GET):
//   /google/flights/search
//   /google/calendar/search
//   /google/explore/search
//
// Behavior: forwards to optional upstreams if set; otherwise returns an empty, valid JSON payload.
// This keeps your agent functional without changing the schema.

import express from "express";

const {
  PORT = 8790,
  ALLOWED_ORIGINS = "*",
  GOOGLE_UPSTREAM_FLIGHTS = "",
  GOOGLE_UPSTREAM_CALENDAR = "",
  GOOGLE_UPSTREAM_EXPLORE = ""
} = process.env;

const app = express();
app.use(express.json({ limit: "2mb" }));

app.use((req,res,next)=>{
  const allow = ALLOWED_ORIGINS==="*" ? "*" : (req.headers.origin || ALLOWED_ORIGINS);
  res.setHeader("Access-Control-Allow-Origin", allow);
  res.setHeader("Access-Control-Allow-Methods","GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers","content-type, authorization");
  if (req.method==="OPTIONS") return res.sendStatus(200);
  next();
});

async function forward(base, path, query) {
  if (!base) return { ok:true, data: { items: [], notes: "no upstream configured" } };
  const url = new URL(path, base);
  Object.entries(query||{}).forEach(([k,v])=> v!=null && url.searchParams.set(k, String(v)));
  const r = await fetch(url, { method: "GET" });
  const t = await r.text();
  try { return { ok:r.ok, data: JSON.parse(t) }; }
  catch { return { ok:r.ok, data: t }; }
}

app.get("/google/flights/search", async (req,res)=>{
  try {
    const out = await forward(GOOGLE_UPSTREAM_FLIGHTS, "/search", req.query);
    res.json(out.data);
  } catch (e) { res.status(500).json({ ok:false, error:String(e) }); }
});

app.get("/google/calendar/search", async (req,res)=>{
  try {
    const out = await forward(GOOGLE_UPSTREAM_CALENDAR, "/search", req.query);
    res.json(out.data);
  } catch (e) { res.status(500).json({ ok:false, error:String(e) }); }
});

app.get("/google/explore/search", async (req,res)=>{
  try {
    const out = await forward(GOOGLE_UPSTREAM_EXPLORE, "/search", req.query);
    res.json(out.data);
  } catch (e) { res.status(500).json({ ok:false, error:String(e) }); }
});

app.get("/healthz", (req,res)=>res.json({ ok:true, endpoints:["/google/flights/search","/google/calendar/search","/google/explore/search"] }));

app.listen(Number(PORT), ()=>console.log(`[google-api] listening on ${PORT}`));
