// derDrucker.mjs -- Flight itinerary formatting & ticket generation service.
//
// The service exposes:
//   POST /tools/derDrucker/wannaCandy      -> format offers into Markdown sections
//   POST /tools/derDrucker/generateTickets -> produce PDF tickets (base64) per leg
//
// Requests must include `type`, `path`, `sender`, etc. Nothing else? (handled by s3 escalator).\n
import http from "node:http";
import url from "node:url";
import crypto from "node:crypto";
import { PDFDocument, StandardFonts, rgb } from "pdf-lib";
import { DateTime } from "luxon";

const PORT = Number(process.env.PORT || 8790);
const DEFAULT_TIMEZONE = (process.env.DEFAULT_TIMEZONE || "UTC").trim() || "UTC";
const rawOrigin = process.env.ORIGIN || "";
const ALLOW_ORIGINS = String(rawOrigin)
  .split(/[\s,]+/)
  .map(s => s.trim())
  .filter(Boolean);
const ALLOW_ORIGIN_SET = new Set(ALLOW_ORIGINS);
const ALLOW_ORIGIN_ENTRIES = ALLOW_ORIGINS.map(value => {
  if (value === "*") return { value: "*", host: "*" };
  let host = value;
  try { host = new URL(value).host; }
  catch (_) { host = value.replace(/^https?:\/\//i, "").replace(/\/$/, ""); }
  return { value, host };
});
function normalizeOriginHost(origin) {
  if (!origin) return "";
  try { return new URL(origin).host; }
  catch (_) { return origin.replace(/^https?:\/\//i, "").replace(/\/$/, ""); }
}

const MAX_BODY_SIZE = 2 * 1024 * 1024; // 2 MiB

const logger = {
  info: (...args) => console.log(new Date().toISOString(), "[INFO]", ...args),
  warn: (...args) => console.warn(new Date().toISOString(), "[WARN]", ...args),
  error: (...args) => console.error(new Date().toISOString(), "[ERROR]", ...args),
};

const readiness = {
  ready: true,
  lastUpdated: Date.now(),
  checks: { deps: true },
  errors: [],
};

function setCorsHeaders(res) {
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  res.setHeader("Vary", "Origin");
}
function enforceCors(req, res) {
  const origin = req.headers.origin || "";
  if (!origin) {
    setCorsHeaders(res);
    return true;
  }
  if (ALLOW_ORIGIN_SET.has("*")) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    setCorsHeaders(res);
    return true;
  }
  const originHost = normalizeOriginHost(origin);
  const match = ALLOW_ORIGIN_ENTRIES.find(entry => {
    if (entry.value === "*") return true;
    if (entry.value === origin) return true;
    if (entry.host && entry.host === originHost) return true;
    return false;
  });
  if (match) {
    res.setHeader("Access-Control-Allow-Origin", origin);
    setCorsHeaders(res);
    return true;
  }
  logger.warn("Blocked request from disallowed origin", { origin, path: req.url || "" });
  res.statusCode = 403;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify({ error: "origin_not_allowed" }));
  return false;
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    let size = 0;
    let done = false;
    req.on("data", chunk => {
      if (done) return;
      size += chunk.length;
      if (size > MAX_BODY_SIZE) {
        done = true;
        reject(new Error("Payload too large"));
        req.destroy();
        return;
      }
      data += chunk;
    });
    req.on("end", () => {
      if (done) return;
      done = true;
      if (!data) { resolve({}); return; }
      try { resolve(JSON.parse(data)); }
      catch (error) {
        const parseError = new Error("Invalid JSON body");
        parseError.cause = error;
        reject(parseError);
      }
    });
    req.on("error", error => {
      if (done) return;
      done = true;
      reject(error);
    });
  });
}

// ---- Domain helpers ------------------------------------------------------

function limitToMax(items = [], max = 10) {
  const cap = Math.max(1, Math.min(10, Math.floor(Number.isFinite(max) ? max : 10)));
  return items.slice(0, cap);
}

function isDirect(offer = {}) {
  const segCount = Array.isArray(offer.legs) ? offer.legs.length : 0;
  return segCount <= 1;
}

function splitDirectness(offers = []) {
  const direct = [], connecting = [];
  for (const offer of Array.isArray(offers) ? offers : []) {
    (isDirect(offer) ? direct : connecting).push(offer);
  }
  return { direct, connecting };
}

function connectingCarriers(offer = {}) {
  const legs = Array.isArray(offer.legs) ? offer.legs : [];
  if (legs.length <= 2) return [];
  const mids = legs.slice(1, -1);
  const primary = legs[0]?.marketingCarrier || legs[0]?.operatingCarrier || null;
  const set = new Set();
  for (const leg of mids) {
    if (leg.operatingCarrier && leg.operatingCarrier !== primary) set.add(leg.operatingCarrier);
    if (leg.marketingCarrier && leg.marketingCarrier !== primary) set.add(leg.marketingCarrier);
  }
  return [...set];
}

function fmtDur(fromISO, toISO) {
  try {
    const diff = DateTime.fromISO(toISO).diff(DateTime.fromISO(fromISO), 'minutes').minutes | 0;
    const h = Math.floor(diff / 60);
    const m = Math.abs(diff % 60);
    return `${h}h${m.toString().padStart(2, '0')}`;
  } catch (_) {
    return "-";
  }
}

function formatOfferMarkdown(offer = {}, timezone = DEFAULT_TIMEZONE) {
  const legs = Array.isArray(offer.legs) ? offer.legs : [];
  if (!legs.length) {
    return `- **Price:** ${offer.currency || ''} ${(offer.total ?? 0).toFixed?.(2) || Number(offer.total || 0).toFixed(2)}`;
  }
  const out = legs[0];
  const rest = legs.slice(1);
  const returning = rest.length ? rest[rest.length - 1] : undefined;
  const connectors = rest.slice(0, Math.max(0, rest.length - 1));

  const fmtLeg = (label, leg) => `- **${label}:** ${leg.marketingCarrier || ''} ${leg.flight || ''} ${leg.from}→${leg.to} — ` +
    `${DateTime.fromISO(leg.depart).setZone(timezone).toFormat('ccc yyyy-LL-dd HH:mm')}–${DateTime.fromISO(leg.arrive).setZone(timezone).toFormat('HH:mm')} ` +
    `(${fmtDur(leg.depart, leg.arrive)})`;

  const lines = [];
  lines.push(fmtLeg('Starting flight', out));
  connectors.forEach((leg, idx) => {
    lines.push(`  - **Connecting flight ${idx + 1}:** ${leg.marketingCarrier || ''} ${leg.flight || ''} ${leg.from}→${leg.to} — ` +
      `${DateTime.fromISO(leg.depart).setZone(timezone).toFormat('ccc yyyy-LL-dd HH:mm')}–${DateTime.fromISO(leg.arrive).setZone(timezone).toFormat('HH:mm')} ` +
      `(${fmtDur(leg.depart, leg.arrive)})`);
  });
  if (returning && connectors.length) {
    lines.push(fmtLeg('Returning flight', returning));
  }
  const price = Number(offer.total || 0);
  lines.push(`- **Price:** ${offer.currency || ''} ${price.toFixed(2)}`);
  return lines.join('\n');
}

function wannaCandy(offers = [], max = 10, timezone = DEFAULT_TIMEZONE) {
  const { direct, connecting } = splitDirectness(offers);
  const directTop = limitToMax(direct, Math.ceil(max / 2));
  const connTop = limitToMax(connecting, Math.max(0, max - directTop.length));

  const sections = [];
  if (directTop.length) {
    sections.push('**Direct flights**');
    directTop.forEach((offer, idx) => sections.push(`${idx + 1}. ${formatOfferMarkdown(offer, timezone)}`));
  }
  if (connTop.length) {
    sections.push('**Connecting flights**');
    connTop.forEach((offer, idx) => sections.push(`${idx + 1}. ${formatOfferMarkdown(offer, timezone)}`));
  }
  return {
    sections,
    markdown: sections.join('\n'),
    directCount: directTop.length,
    connectingCount: connTop.length,
    connectingCarriers: connectingTopCarriers(connTop),
  };
}

function connectingTopCarriers(offers = []) {
  const all = new Set();
  for (const offer of offers) {
    connectingCarriers(offer).forEach(c => all.add(c));
  }
  return [...all];
}

async function generateSegmentTicket(leg, pnr, timezone = DEFAULT_TIMEZONE) {
  const pdf = await PDFDocument.create();
  const page = pdf.addPage([595, 842]);
  const font = await pdf.embedFont(StandardFonts.Helvetica);
  const draw = (text, x, y, size = 12) => page.drawText(String(text || ''), { x, y, size, font, color: rgb(0, 0, 0) });

  draw('e-Ticket / Itinerary Receipt', 50, 800, 16);
  draw(`PNR: ${pnr}`, 50, 780);
  draw(`Flight: ${leg?.marketingCarrier || ''} ${leg?.flight || ''}`.trim(), 50, 760);
  draw(`From: ${leg?.from || '-'}  →  To: ${leg?.to || '-'}`, 50, 740);
  draw(`Departure: ${DateTime.fromISO(leg?.depart || '').setZone(timezone).toFormat('yyyy-LL-dd HH:mm')}`, 50, 720);
  draw(`Arrival:   ${DateTime.fromISO(leg?.arrive || '').setZone(timezone).toFormat('yyyy-LL-dd HH:mm')}`, 50, 700);
  draw(`Operating carrier: ${leg?.operatingCarrier || leg?.marketingCarrier || '-'}`, 50, 680);

  const bytes = await pdf.save();
  return bytes;
}

async function generateTicketsForOffer(offer = {}, pnr = 'PNR', options = {}) {
  const timezone = options.timezone || DEFAULT_TIMEZONE;
  const legs = Array.isArray(offer.legs) ? offer.legs : [];
  if (!legs.length) {
    return [];
  }
  const tickets = [];
  let idx = 1;
  for (const leg of legs) {
    const bytes = await generateSegmentTicket(leg, pnr, timezone);
    tickets.push({
      fileName: `${options.fileNamePrefix || 'ticket'}_${idx}.pdf`,
      mimeType: 'application/pdf',
      base64: Buffer.from(bytes).toString('base64'),
    });
    idx += 1;
  }
  return tickets;
}

// ---- Route handlers ------------------------------------------------------

async function handleWannaCandy(req, res, body) {
  const offers = Array.isArray(body?.offers) ? body.offers : [];
  if (!offers.length) {
    res.statusCode = 400;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "offers_required" }));
    return;
  }
  const max = Number(body?.max ?? 10);
  const timezone = String(body?.timezone || DEFAULT_TIMEZONE).trim() || DEFAULT_TIMEZONE;
  const result = wannaCandy(offers, max, timezone);
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify({ ok: true, ...result, timezone }));
}

async function handleGenerateTickets(req, res, body) {
  const offer = body?.offer;
  const pnr = String(body?.pnr || body?.locator || 'PNR').trim();
  if (!offer || !Array.isArray(offer.legs) || offer.legs.length === 0) {
    res.statusCode = 400;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "offer_with_legs_required" }));
    return;
  }
  const timezone = String(body?.timezone || DEFAULT_TIMEZONE).trim() || DEFAULT_TIMEZONE;
  try {
    const tickets = await generateTicketsForOffer(offer, pnr, {
      timezone,
      fileNamePrefix: body?.fileNamePrefix || 'ticket',
    });
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ ok: true, tickets }));
  } catch (error) {
    logger.error("generateTickets error", { message: error?.message || String(error) });
    res.statusCode = 500;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "internal_error" }));
  }
}

// ---- Server --------------------------------------------------------------

const server = http.createServer(async (req, res) => {
  const requestId = crypto.randomUUID();
  const startedAt = Date.now();
  logger.info(`[${requestId}] ${req.method} ${req.url}`);

  if (!enforceCors(req, res)) { return; }

  if (req.method === "OPTIONS") {
    res.statusCode = 204;
    setCorsHeaders(res);
    res.end();
    return;
  }

  let parsed;
  try {
    parsed = new url.URL(req.url, "http://localhost");
  } catch (_) {
    res.statusCode = 400;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "invalid_url" }));
    return;
  }

  try {
    if (req.method === "POST" && parsed.pathname === "/tools/derDrucker/wannaCandy") {
      let body;
      try { body = await readBody(req); }
      catch (error) {
        const status = error.message === "Payload too large" ? 413 : 400;
        res.statusCode = status;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: error.message }));
        return;
      }
      await handleWannaCandy(req, res, body);
      return;
    }

    if (req.method === "POST" && parsed.pathname === "/tools/derDrucker/generateTickets") {
      let body;
      try { body = await readBody(req); }
      catch (error) {
        const status = error.message === "Payload too large" ? 413 : 400;
        res.statusCode = status;
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ error: error.message }));
        return;
      }
      await handleGenerateTickets(req, res, body);
      return;
    }

    if ((req.method === "GET" || req.method === "HEAD") && parsed.pathname === "/health") {
      if (req.method === "HEAD") { res.statusCode = 200; res.end(); return; }
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ ok: true, time: new Date().toISOString() }));
      return;
    }

    if ((req.method === "GET" || req.method === "HEAD") && parsed.pathname === "/ready") {
      const ageMs = Date.now() - (readiness.lastUpdated || 0);
      const body = {
        ok: !!readiness.ready,
        time: new Date().toISOString(),
        lastUpdated: new Date(readiness.lastUpdated || Date.now()).toISOString(),
        checks: readiness.checks,
        errors: readiness.errors,
        ageMs,
      };
      res.statusCode = readiness.ready ? 200 : 503;
      if (req.method === "HEAD") { res.end(); return; }
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(body));
      return;
    }

    res.statusCode = 404;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "not_found" }));
  } catch (error) {
    logger.error(`[${requestId}] Request failed`, { message: error?.message || String(error), stack: error?.stack });
    if (!res.headersSent) {
      res.statusCode = 500;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ error: "internal_error" }));
    }
  } finally {
    logger.info(`[${requestId}] Completed with status ${res.statusCode} in ${Date.now() - startedAt}ms`);
  }
});

server.listen(PORT, () => {
  logger.info(`derDrucker listening on :${PORT}`, { timezone: DEFAULT_TIMEZONE, origins: [...ALLOW_ORIGIN_SET] });
});
