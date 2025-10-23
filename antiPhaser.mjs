// services/antiPhaser.mjs — Date phrase interpreter (placeholder implementation)
import express from "express";
import dayjs from "dayjs";

const { PORT=8789, ALLOWED_ORIGINS="*" } = process.env;
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

function toISO(d){ const x=dayjs(d); return x.isValid() ? x.format("YYYY-MM-DD") : null; }
function parse(phrase, ref){
  const r = ref ? dayjs(ref) : dayjs();
  const s = String(phrase||"").toLowerCase();
  // Minimal demo logic; replace with your robust Lambda logic.
  let depart = null, ret = null;
  if (s.includes("next friday")) depart = r.day()<=5 ? r.day(12) : r.add(1,"week").day(5);
  if (s.includes("next monday")) ret = r.day()<=1 ? r.day(8) : r.add(1,"week").day(1);
  return { departDate: depart && depart.format("YYYY-MM-DD"), returnDate: ret && ret.format("YYYY-MM-DD") };
}

app.post("/tools/antiPhaser",(req,res)=>{
  const { phrase, referenceDate } = req.body || {};
  const out = parse(phrase, referenceDate);
  res.json({ ...out, notes:"demo" });
});

app.get("/tools/antiPhaser",(req,res)=>{
  const { phrase, referenceDate } = req.query || {};
  const out = parse(phrase, referenceDate);
  res.json({ ...out, notes:"demo" });
});

app.get("/healthz",(req,res)=>res.json({ ok:true }));
app.listen(Number(PORT), ()=>console.log(`[antiPhaser] up on ${PORT}`));
