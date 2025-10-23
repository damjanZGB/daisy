// derDrucker.mjs — Markdown formatter and PDF ticket generator service
import express from "express";
import { Buffer } from "node:buffer";
import { PDFDocument, StandardFonts } from "pdf-lib";

const {
  PORT = "8790",
  ORIGIN = "*",
  NODE_ENV = "",
} = process.env;

const rawOrigins = ORIGIN.split(/[\s,]+/).map((o) => o.trim()).filter(Boolean);
const allowAllOrigins = rawOrigins.length === 0 || rawOrigins.includes("*");
const allowedOriginSet = new Set(
  rawOrigins.length === 0 ? ["*"] : rawOrigins
);

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "5mb" }));

function setCorsHeaders(req, res) {
  const requestOrigin = req.headers.origin;
  let allowOrigin = allowAllOrigins ? "*" : undefined;
  if (!allowOrigin) {
    if (requestOrigin && allowedOriginSet.has(requestOrigin)) {
      allowOrigin = requestOrigin;
    } else if (!requestOrigin) {
      allowOrigin = rawOrigins[0] || "*";
    }
  }
  if (!allowOrigin) {
    return false;
  }
  res.setHeader("Access-Control-Allow-Origin", allowOrigin);
  res.setHeader("Vary", "Origin");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "content-type, authorization");
  res.setHeader("Access-Control-Max-Age", "7200");
  return true;
}

app.use((req, res, next) => {
  if (!setCorsHeaders(req, res)) {
    res.status(403).json({ error: "origin_not_allowed" });
    return;
  }
  if (req.method === "OPTIONS") {
    res.status(204).end();
    return;
  }
  next();
});

function toSafeText(value, fallback = "?") {
  const text = (value ?? "").toString().trim();
  return text || fallback;
}

function renderSegmentsMarkdown(segments = []) {
  if (!Array.isArray(segments) || segments.length === 0) {
    return ["    - THEN ?? ? -> ? | ? -> ?"];
  }
  return segments.map((seg) => {
    const carrierFlight = `${toSafeText(seg.carrier, "")}${toSafeText(
      seg.flight,
      ""
    )}`.trim();
    const path = `${toSafeText(seg.from)} -> ${toSafeText(seg.to)}`;
    const times = `${toSafeText(seg.departISO)} -> ${toSafeText(
      seg.arriveISO
    )}`;
    const label = toSafeText(seg.label, "Segment");
    return `    - THEN ${carrierFlight || "??"} ${path} | ${times} | ${label}`;
  });
}

function formatMarkdown(body = {}) {
  const scope = toSafeText(body.scope, "");
  const options = Array.isArray(body.options) ? body.options : [];
  const direct = options.filter((o) => o.isDirect);
  const connecting = options.filter((o) => !o.isDirect);

  const buildSection = (title, items) => {
    if (!items.length) return "";
    const lines = items.map((opt, idx) => {
      const header = `- ${idx + 1}. Option`;
      const priceValue = toSafeText(opt.price);
      const currencyRaw = (opt.currency ?? "").toString().trim();
      const priceLine = `    - Price: ${priceValue}${
        currencyRaw ? ` ${currencyRaw}` : ""
      }`;
      const meta = [
        opt.label ? `    - Label: ${toSafeText(opt.label)}` : null,
        `    - Duration: ${toSafeText(opt.duration)}`,
        priceLine,
      ].filter(Boolean);
      const segmentLines = renderSegmentsMarkdown(opt.segments);
      return [header, ...meta, ...segmentLines].join("\n");
    });
    return [`## ${title}`, ...lines].join("\n");
  };

  const sections = [
    scope ? `_Scope: ${scope}_` : "",
    buildSection("Direct Flights", direct),
    buildSection("Connecting Flights", connecting),
  ].filter(Boolean);

  const ticketSegments = options.flatMap((opt) =>
    Array.isArray(opt.segments) ? opt.segments : []
  );

  return {
    markdown: sections.join("\n\n"),
    ticketSegments: ticketSegments.map((seg) => ({
      carrier: toSafeText(seg.carrier, ""),
      flight: toSafeText(seg.flight, ""),
      from: toSafeText(seg.from),
      to: toSafeText(seg.to),
      departISO: toSafeText(seg.departISO),
      arriveISO: toSafeText(seg.arriveISO),
      label: toSafeText(seg.label, ""),
    })),
  };
}

app.post("/tools/derDrucker/wannaCandy", (req, res) => {
  try {
    const body = req.body || {};
    const result = formatMarkdown(body);
    res.json(result);
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Unable to format payload.";
    console.error("[derDrucker] format error", message);
    res.status(400).json({ error: "format_failed", message });
  }
});

async function buildTicketPdf(segments = []) {
  const pdfDoc = await PDFDocument.create();
  const font = await pdfDoc.embedFont(StandardFonts.Helvetica);
  const items = Array.isArray(segments) && segments.length > 0 ? segments : [
    {
      carrier: "",
      flight: "",
      from: "?",
      to: "?",
      departISO: "?",
      arriveISO: "?",
    },
  ];

  items.forEach((seg, index) => {
    const page = pdfDoc.addPage();
    const { width, height } = page.getSize();
    const margin = 50;
    let cursor = height - margin;

    const drawLine = (text, size = 12, lineGap = 18) => {
      page.drawText(text, {
        x: margin,
        y: cursor,
        size,
        font,
      });
      cursor -= lineGap;
    };

    drawLine("Flight Ticket", 20, 28);
    drawLine(`Segment #${index + 1}`);
    drawLine(`Carrier: ${toSafeText(seg.carrier)}`);
    drawLine(`Flight: ${toSafeText(seg.flight)}`);
    drawLine(`From: ${toSafeText(seg.from)}`);
    drawLine(`To: ${toSafeText(seg.to)}`);
    drawLine(`Departure: ${toSafeText(seg.departISO)}`);
    drawLine(`Arrival: ${toSafeText(seg.arriveISO)}`);
  });

  const pdfBytes = await pdfDoc.save();
  const pdfBase64 = Buffer.from(pdfBytes).toString("base64");
  return { pdfBase64, pages: pdfDoc.getPageCount() };
}

app.post("/tools/derDrucker/generateTickets", async (req, res) => {
  try {
    const { segments = [] } = req.body || {};
    const { pdfBase64, pages } = await buildTicketPdf(segments);
    res.json({ pdfBase64, pages });
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "Unable to generate PDF.";
    console.error("[derDrucker] pdf error", message);
    res.status(500).json({ error: "pdf_generation_failed", message });
  }
});

app.get("/health", (req, res) => {
  res.json({ ok: true, time: new Date().toISOString() });
});

app.get("/ready", (req, res) => {
  res.json({
    ok: true,
    time: new Date().toISOString(),
    dependencies: {
      pdfLib: true,
    },
  });
});

app.get("/", (req, res) => {
  res
    .type("text/plain")
    .send("derDrucker online\n");
});

app.use((req, res) => {
  res.status(404).json({ error: "not_found" });
});

const port = Number.parseInt(PORT, 10) || 8790;
app.listen(port, () => {
  console.log(`[derDrucker] listening on port ${port}`, {
    allowAllOrigins,
    origins: allowAllOrigins ? ["*"] : [...allowedOriginSet],
  });
});
