// services/derDrucker.mjs — Markdown formatter + PDF tickets (minimal)
import express from "express";
import PDFDocument from "pdfkit";

const { PORT = 8791, ALLOWED_ORIGINS="*" } = process.env;

const app = express();
app.use(express.json({ limit: "3mb" }));

app.use((req,res,next)=>{
  const allow = ALLOWED_ORIGINS==="*" ? "*" : (req.headers.origin || ALLOWED_ORIGINS);
  res.setHeader("Access-Control-Allow-Origin", allow);
  res.setHeader("Access-Control-Allow-Methods","GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers","content-type, authorization");
  if (req.method==="OPTIONS") return res.sendStatus(200);
  next();
});

function formatMarkdown({ scope, options=[] }) {
  const dir = options.filter(o=>o.isDirect);
  const ndi = options.filter(o=>!o.isDirect);
  function lines(opts){
    return opts.map((o,i)=>{
      const segs = (o.segments||[]).map(s=>`  - **${s.label||'Starting flight'}**: ${s.from} -> ${s.to} • ${s.carrier}${s.flight?(' '+s.flight):''} • ${s.departISO} → ${s.arriveISO}`);
      const price = `  - **Price**: ${o.price} ${o.currency||''}`.trim();
      return `- ${i+1}. Option\n${segs.join("\n")}\n  - Duration: ${o.duration||'—'}\n${price}`;
    }).join("\n");
  }
  const md = [
    scope ? `_Scope: ${scope}_` : "",
    dir.length? "## Direct flights\n"+lines(dir) : "",
    ndi.length? "## Non-direct flights\n"+lines(ndi) : ""
  ].filter(Boolean).join("\n\n");
  const ticketSegments = (options||[]).flatMap(o=>o.segments||[]).map(s=>({
    carrier:s.carrier, flight:s.flight, from:s.from, to:s.to, departISO:s.departISO, arriveISO:s.arriveISO
  }));
  return { markdown: md, ticketSegments };
}

app.post("/tools/derDrucker/wannaCandy",(req,res)=>{
  const body = req.body || {};
  const out = formatMarkdown(body);
  res.json(out);
});

function pdfFromSegments(segments=[]) {
  const doc = new PDFDocument({ size:"A4", margin: 50 });
  const chunks = [];
  doc.on("data", c=>chunks.push(c));
  doc.on("end", ()=>{});
  segments.forEach((s, idx)=>{
    if (idx>0) doc.addPage();
    doc.fontSize(18).text("Flight Ticket", { align:"center" });
    doc.moveDown().fontSize(12);
    doc.text(`Carrier: ${s.carrier}`);
    doc.text(`Flight: ${s.flight}`);
    doc.text(`From: ${s.from}`);
    doc.text(`To: ${s.to}`);
    doc.text(`Departure: ${s.departISO}`);
    doc.text(`Arrival: ${s.arriveISO}`);
  });
  doc.end();
  return Buffer.concat(chunks).toString("base64");
}

app.post("/tools/derDrucker/generateTickets",(req,res)=>{
  const { segments=[] } = req.body || {};
  const pdfBase64 = pdfFromSegments(segments);
  res.json({ pdfBase64, pages: Math.max(segments.length,1) });
});

app.get("/healthz",(req,res)=>res.json({ ok:true }));
app.listen(Number(PORT), ()=>console.log(`[derDrucker] up on ${PORT}`));
