// antiPhaser.mjs — Natural language date phrase parser service
import express from "express";
import * as chrono from "chrono-node";
import { DateTime } from "luxon";

const {
  PORT = "8789",
  ORIGIN = "*",
  DEFAULT_TIMEZONE = "UTC",
  NODE_ENV = "",
} = process.env;

const rawOrigins = ORIGIN.split(",").map((o) => o.trim()).filter(Boolean);
const allowAllOrigins = rawOrigins.length === 0 || rawOrigins.includes("*");
const allowedOriginSet = new Set(rawOrigins.length === 0 ? ["*"] : rawOrigins);

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "1mb" }));

function setCorsHeaders(req, res) {
  const requestOrigin = req.headers.origin;
  let allowOrigin = allowAllOrigins ? "*" : undefined;
  if (!allowOrigin) {
    if (requestOrigin && allowedOriginSet.has(requestOrigin)) {
      allowOrigin = requestOrigin;
    } else if (!requestOrigin) {
      // No Origin header: fall back to first allowed value
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

function coerceTimezone(value) {
  const tz = (value || "").toString().trim();
  return tz || DEFAULT_TIMEZONE || "UTC";
}

function resolveReferenceDate(referenceDate, zone) {
  if (!referenceDate) {
    return DateTime.now().setZone(zone);
  }
  const candidate = DateTime.fromISO(referenceDate, { zone });
  return candidate.isValid ? candidate : DateTime.now().setZone(zone);
}

function extractConfidence(component) {
  if (!component || typeof component.isCertain !== "function") return 0.4;
  const certaintyKeys = ["year", "month", "day"];
  const score = certaintyKeys.reduce(
    (acc, key) => acc + (component.isCertain(key) ? 1 : 0),
    0
  );
  return 0.4 + 0.2 * score;
}

function interpretDatePhrase({ phrase, referenceDate, timeZone }) {
  const trimmed = (phrase || "").toString().trim();
  if (!trimmed) {
    return {
      success: false,
      statusCode: 400,
      reason: "phrase_required",
      message: "Provide a natural-language date or time phrase to interpret.",
    };
  }

  const zone = coerceTimezone(timeZone);
  const ref = resolveReferenceDate(referenceDate, zone);

  try {
    const parsed = chrono.parse(trimmed, ref.toJSDate(), {
      forwardDate: true,
    });
    if (!parsed || parsed.length === 0) {
      return {
        success: false,
        statusCode: 422,
        reason: "unrecognised_phrase",
        message: "The phrase could not be interpreted. Ask the user for clearer dates.",
      };
    }

    const best = parsed[0];
    const start = best.start;
    if (!start) {
      return {
        success: false,
        statusCode: 422,
        reason: "no_start_component",
        message: "The phrase did not resolve to a concrete start date.",
      };
    }

    const startDate = DateTime.fromJSDate(start.date(), { zone });
    const result = {
      success: true,
      phrase: trimmed,
      isoDate: startDate.toISO({ suppressMilliseconds: true }),
      isoDateUTC: startDate.toUTC().toISO({ suppressMilliseconds: true }),
      isoDateOnly: startDate.toISODate(),
      isoTime: startDate.toISOTime({ suppressMilliseconds: true }),
      timeZone: zone,
      referenceDate: ref.toISO(),
      confidence: Number(extractConfidence(start).toFixed(2)),
      explanation: best.text
        ? `Interpreted "${best.text}" relative to ${ref.toISODate()}`
        : "Interpreted using chrono-node default parser",
    };

    if (best.end) {
      const endDate = DateTime.fromJSDate(best.end.date(), { zone });
      result.endIsoDate = endDate.toISO({ suppressMilliseconds: true });
      result.endIsoDateUTC = endDate.toUTC().toISO({ suppressMilliseconds: true });
      result.endIsoDateOnly = endDate.toISODate();
      result.endIsoTime = endDate.toISOTime({ suppressMilliseconds: true });
    }

    // Surface chrono components for debugging if running locally
    if (NODE_ENV !== "production") {
      result.components = {
        knownValues: start.knownValues,
        impliedValues: start.impliedValues,
      };
    }

    return result;
  } catch (error) {
    return {
      success: false,
      statusCode: 500,
      reason: "parse_error",
      message: "An unexpected error occurred while parsing the phrase.",
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

function respondWithInterpretation(req, res, payloadSource) {
  const result = interpretDatePhrase(payloadSource);
  if (!result.success) {
    const status = result.statusCode || 422;
    const body = {
      success: false,
      reason: result.reason,
      message: result.message,
    };
    if (result.error && NODE_ENV !== "production") {
      body.error = result.error;
    }
    res.status(status).json(body);
    return;
  }
  res.json(result);
}

app.post("/tools/antiPhaser", (req, res) => {
  const { phrase, referenceDate, timezone, timeZone } = req.body || {};
  respondWithInterpretation(req, res, {
    phrase,
    referenceDate,
    timeZone: timeZone || timezone,
  });
});

app.get("/tools/antiPhaser", (req, res) => {
  const { phrase, referenceDate, timezone, timeZone } = req.query || {};
  respondWithInterpretation(req, res, {
    phrase,
    referenceDate,
    timeZone: timeZone || timezone,
  });
});

app.get("/health", (req, res) => {
  res.json({ ok: true, time: new Date().toISOString() });
});

app.get("/ready", (req, res) => {
  res.json({
    ok: true,
    time: new Date().toISOString(),
    dependencies: {
      chronoNode: true,
      luxon: true,
    },
  });
});

app.get("/", (req, res) => {
  res.type("text/plain").send("antiPhaser online\n");
});

app.use((req, res) => {
  res.status(404).json({ error: "not_found" });
});

const port = Number.parseInt(PORT, 10) || 8789;
app.listen(port, () => {
  console.log(`[antiPhaser] listening on port ${port}`, {
    allowAllOrigins,
    origins: allowAllOrigins ? ["*"] : [...allowedOriginSet],
  });
});
