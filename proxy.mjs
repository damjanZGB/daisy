// services/proxy.mjs — One-agent, three-alias Return‑Control proxy (provider decided at build time via agent instructions)
// Forwards: /tools/* -> TOOLS_BASE_URL (origin-daisy), /google/* -> GOOGLE_BASE_URL (google-api-daisy)
// No runtime provider switch. The chosen provider is hard-coded in the agent's instructions.

import express from "express";
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
  FORWARD_TOOLS = "true",
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

// Optional: forward /tools/* to TOOLS_BASE_URL for front-ends
if (/^true$/i.test(FORWARD_TOOLS||"true")) {
  app.all("/tools/*", async (req,res)=>{
    try {
      const target = new URL(req.originalUrl, TOOLS_BASE_URL);
      const r = await fetch(target, { method:req.method, headers:{ "content-type":req.headers["content-type"]||"application/json" }, body: req.method==="GET"?undefined:JSON.stringify(req.body||{}) });
      const t = await r.text(); res.status(r.status).set("content-type", r.headers.get("content-type")||"application/json").send(t);
    } catch (e) { res.status(502).json({ ok:false, error:String(e) }); }
  });
}

app.listen(Number(PORT), ()=>console.log(`[proxy] up on ${PORT}`));
