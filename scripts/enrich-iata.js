// scripts/enrich-iata.js
// Merge OpenFlights airport coordinates into the local IATA database and regenerate JSON/MD artefacts.
import fs from "node:fs";
import path from "node:path";

const ROOT = process.cwd();
const DATA_DIR = path.join(ROOT, "data");
const DEFAULT_IATA_JSON = path.join(ROOT, "iata.json");
const BACKEND_IATA_JSON = path.join(ROOT, "backend", "iata.json");
const DEFAULT_MD = path.join(DATA_DIR, "iata.md");
const KB_MD = path.join(DATA_DIR, "iata-kb.md");
const OPENFLIGHTS_PATH = path.join(DATA_DIR, "openflights_airports.dat");

const TARGET_PRECISION = 6;
const fmtCoord = (value) => {
  if (!Number.isFinite(value)) return undefined;
  const n = Number(value);
  if (!Number.isFinite(n)) return undefined;
  return Number(n.toFixed(TARGET_PRECISION));
};

const parseCsvLine = (line) => {
  const out = [];
  let cur = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (ch === "\"") {
      if (inQuotes && line[i + 1] === "\"") {
        cur += "\"";
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (ch === "," && !inQuotes) {
      out.push(cur);
      cur = "";
    } else {
      cur += ch;
    }
  }
  out.push(cur);
  return out;
};

const loadOpenFlights = (filePath) => {
  if (!fs.existsSync(filePath)) {
    throw new Error(`OpenFlights dataset missing at ${filePath}`);
  }
  const map = new Map();
  const raw = fs.readFileSync(filePath, "utf8");
  const lines = raw.split(/\r?\n/);
  for (const line of lines) {
    if (!line || line.startsWith("#")) continue;
    const cells = parseCsvLine(line);
    if (cells.length < 8) continue;
    const iata = (cells[4] || "").trim();
    if (!iata || iata === "\\N") continue;
    const lat = parseFloat(cells[6]);
    const lon = parseFloat(cells[7]);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    const type = (cells[12] || "").trim().toLowerCase() || "airport";
    // Prefer the first "airport" we see; skip heliports/seaplane bases if we already stored an airport.
    const existing = map.get(iata);
    if (existing?.type === "airport" && type !== "airport") {
      continue;
    }
    map.set(iata, {
      latitude: fmtCoord(lat),
      longitude: fmtCoord(lon),
      name: cells[1] || "",
      city: cells[2] || "",
      country: cells[3] || "",
      type,
    });
  }
  return map;
};

const escapeCell = (value) => {
  if (value === null || value === undefined) return "";
  return String(value).replace(/\|/g, "\\|");
};

const writeMarkdown = (records, filePath) => {
  const header = [
    "# IATA Airports/City Codes (Table)",
    "",
    "| IATA | Name | City | Country | Type | Latitude | Longitude |",
    "|:----:|:-----|:-----|:--------|:-----|---------:|----------:|",
  ];
  const lines = [...header];
  for (const [code, rec] of records) {
    const row = [
      code,
      escapeCell(rec.name || ""),
      escapeCell(rec.city || ""),
      escapeCell(rec.country || ""),
      rec.type || "",
      rec.latitude ?? "",
      rec.longitude ?? "",
    ];
    lines.push(`| ${row.join(" | ")} |`);
  }
  fs.writeFileSync(filePath, lines.join("\n"));
};

const main = () => {
  const openFlights = loadOpenFlights(OPENFLIGHTS_PATH);
  const iataJson = JSON.parse(fs.readFileSync(DEFAULT_IATA_JSON, "utf8"));

  let enriched = 0;
  let missing = 0;
  for (const [code, rec] of Object.entries(iataJson)) {
    const dataset = openFlights.get(code);
    if (!dataset || !Number.isFinite(dataset.latitude) || !Number.isFinite(dataset.longitude)) {
      missing += rec.type === "airport" ? 1 : 0;
      continue;
    }
    rec.latitude = dataset.latitude;
    rec.longitude = dataset.longitude;
    enriched += 1;
  }

  const sortedEntries = Object.entries(iataJson).sort((a, b) => a[0].localeCompare(b[0]));
  fs.writeFileSync(DEFAULT_IATA_JSON, JSON.stringify(Object.fromEntries(sortedEntries), null, 2));
  fs.writeFileSync(BACKEND_IATA_JSON, JSON.stringify(Object.fromEntries(sortedEntries), null, 2));
  const markdownTargets = [DEFAULT_MD, KB_MD].filter(Boolean);
  for (const target of markdownTargets) {
    writeMarkdown(sortedEntries, target);
  }

  console.log(`Enriched ${enriched} records with coordinates. Missing airports: ${missing}.`);
};

main();
