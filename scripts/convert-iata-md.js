// scripts/convert-iata-md.js
import fs from "node:fs";

const [,, mdPath, outPath] = process.argv;
if (!mdPath || !outPath) {
  console.error("Usage: node scripts/convert-iata-md.js ./iata.md ./iata.json");
  process.exit(1);
}
const md = fs.readFileSync(mdPath, "utf8");
const lines = md.split(/\r?\n/);
const rows = [];
let header = null;
for (const line of lines) {
  const m = /^\s*\|(.+)\|\s*$/.exec(line);
  if (!m) continue;
  const cells = m[1].split("|").map(c => c.trim());
  if (!header) { header = cells.map(h => h.toLowerCase()); continue; }
  if (cells.length !== header.length) continue;
  const rec = Object.fromEntries(cells.map((v, i) => [header[i], v]));
  rows.push(rec);
}
const out = {};
for (const r of rows) {
  const code = (r.iata || r.code || r["iata code"] || "").toUpperCase();
  if (!code) continue;
  out[code] = {
    name: r.name || r.airport || "",
    city: r.city || "",
    country: r.country || "",
    type: (r.type || (r.airport ? "airport" : "city") || "").toLowerCase()
  };
}
fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
console.log(`Wrote ${Object.keys(out).length} records to ${outPath}`);
