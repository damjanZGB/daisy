// services/google-api.mjs — Google endpoints microservice backed by searchapi.io
// Exposes GET/POST under /google/flights/*, /google/calendar/*, /google/explore/*

import express from "express";

const {
  PORT = 8790,
  ALLOWED_ORIGINS = "*",
  SEARCHAPI_KEY = "",
  SEARCHAPI_BASE = "https://www.searchapi.io/api/v1/search"
} = process.env;

const app = express();
app.use(express.json({ limit: "4mb" }));

app.use((req,res,next)=>{
  const allow = ALLOWED_ORIGINS==="*" ? "*" : (req.headers.origin || ALLOWED_ORIGINS);
  res.setHeader("Access-Control-Allow-Origin", allow);
  res.setHeader("Access-Control-Allow-Methods","GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers","content-type, authorization");
  if (req.method==="OPTIONS") return res.sendStatus(200);
  next();
});

function collectParams(req){
  const isGet = req.method === "GET";
  const q = req.query || {};
  const b = (req.body && typeof req.body === "object") ? req.body : {};
  const bodyParams = b.params && typeof b.params === "object" ? b.params : b;
  return { ...(isGet ? q : bodyParams) };
}

async function callSearchApi(engine, params){
  const url = new URL(SEARCHAPI_BASE);
  const final = { ...params, engine };
  if (!final.api_key && SEARCHAPI_KEY) final.api_key = SEARCHAPI_KEY;
  Object.entries(final).forEach(([k,v]) => v!=null && url.searchParams.set(k, String(v)));
  const r = await fetch(url, { method: "GET" });
  const t = await r.text();
  try { return { ok:r.ok, status:r.status, data: JSON.parse(t) }; }
  catch { return { ok:r.ok, status:r.status, data: t }; }
}

function send(res, out){
  res.status(out.ok ? 200 : (out.status || 500)).json(out.data);
}

app.all("/google/flights/*", async (req,res)=>{
  try { const out = await callSearchApi("google_flights", collectParams(req)); return send(res,out); }
  catch(e){ return res.status(500).json({ error:String(e) }); }
});
app.all("/google/calendar/*", async (req,res)=>{
  try { const out = await callSearchApi("google_flights_calendar", collectParams(req)); return send(res,out); }
  catch(e){ return res.status(500).json({ error:String(e) }); }
});
app.all("/google/explore/*", async (req,res)=>{
  try { const out = await callSearchApi("google_travel_explore", collectParams(req)); return send(res,out); }
  catch(e){ return res.status(500).json({ error:String(e) }); }
});

app.get("/healthz",(req,res)=>res.json({ ok:true, service:"google-api", provider:"searchapi.io" }));
app.listen(Number(PORT), ()=>console.log(`[google-api] up on ${PORT}`));
