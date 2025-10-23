// services/antiPhaser.mjs — Date phrase interpreter (GET and POST supported)
import express from "express";
import dayjs from "dayjs";

const { PORT = 8789, ALLOWED_ORIGINS = "*" } = process.env;

const app = express();
app.use(express.json({ limit: "1mb" }));

app.use((req,res,next)=>{
  const allow = ALLOWED_ORIGINS==="*" ? "*" : (req.headers.origin || ALLOWED_ORIGINS);
  res.setHeader("Access-Control-Allow-Origin", allow);
  res.setHeader("Access-Control-Allow-Methods","GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers","content-type, authorization");
  if (req.method==="OPTIONS") return res.sendStatus(200);
  next();
});

function toISO(d) { const x = dayjs(d); return x.isValid() ? x.format("YYYY-MM-DD") : null; }

function parsePhrase(phrase, referenceDate) {
  const ref = referenceDate ? dayjs(referenceDate) : dayjs();
  const s = String(phrase||"").toLowerCase();
  let depart=null, ret=null;
  if (/next\s+friday/.test(s)) depart = ref.day()<=5 ? ref.day(5+7) : ref.add(1,'week').day(5);
  if (/next\s+monday/.test(s)) ret = ref.day()<=1 ? ref.day(1+7) : ref.add(1,'week').day(1);
  return { departDate: depart && depart.format("YYYY-MM-DD"), returnDate: ret && ret.format("YYYY-MM-DD") };
}

app.post("/tools/antiPhaser", (req,res)=>{
  const { phrase, timezone, referenceDate } = req.body || {};
  const out = parsePhrase(phrase, referenceDate);
  res.json({ ...out, notes: "antiPhaser demo parser" });
});

app.get("/tools/antiPhaser", (req,res)=>{
  const { phrase, timezone, referenceDate } = req.query || {};
  const out = parsePhrase(phrase, referenceDate);
  res.json({ ...out, notes: "antiPhaser demo parser" });
});

app.get("/healthz",(req,res)=>res.json({ ok:true }));
app.listen(Number(PORT), ()=>console.log(`[antiPhaser] up on ${PORT}`));
