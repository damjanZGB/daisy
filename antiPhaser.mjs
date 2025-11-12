// antiPhaser.mjs — Natural language date phrase parser service
import "dotenv/config";
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

const MONTH_DEFINITIONS = [
  { slug: "january", names: ["january", "jan"] },
  { slug: "february", names: ["february", "feb"] },
  { slug: "march", names: ["march", "mar"] },
  { slug: "april", names: ["april", "apr"] },
  { slug: "may", names: ["may"] },
  { slug: "june", names: ["june", "jun"] },
  { slug: "july", names: ["july", "jul"] },
  { slug: "august", names: ["august", "aug"] },
  { slug: "september", names: ["september", "sep"] },
  { slug: "october", names: ["october", "oct"] },
  { slug: "november", names: ["november", "nov"] },
  { slug: "december", names: ["december", "dec", "xmas", "christmas", "weihnachten"] },
];

const MONTH_SLUG_TO_INDEX = new Map(
  MONTH_DEFINITIONS.map((item, idx) => [item.slug, idx + 1])
);

function nextFixedDate(ref, zone, month, day) {
  let candidate = DateTime.fromObject({ year: ref.year, month, day }, { zone });
  if (!candidate.isValid) {
    return null;
  }
  const refStartMillis = ref.startOf("day").toMillis();
  if (candidate.toMillis() < refStartMillis) {
    candidate = candidate.plus({ years: 1 });
  }
  return candidate;
}

function computeEasterSunday(year, zone) {
  const a = year % 19;
  const b = Math.floor(year / 100);
  const c = year % 100;
  const d = Math.floor(b / 4);
  const e = b % 4;
  const f = Math.floor((b + 8) / 25);
  const g = Math.floor((b - f + 1) / 3);
  const h = (19 * a + b - d - g + 15) % 30;
  const i = Math.floor(c / 4);
  const k = c % 4;
  const l = (32 + 2 * e + 2 * i - h - k) % 7;
  const m = Math.floor((a + 11 * h + 22 * l) / 451);
  const month = Math.floor((h + l - 7 * m + 114) / 31);
  const day = ((h + l - 7 * m + 114) % 31) + 1;
  return DateTime.fromObject({ year, month, day }, { zone });
}

function nextEasterSunday(ref, zone) {
  let easter = computeEasterSunday(ref.year, zone);
  if (!easter.isValid) {
    return null;
  }
  if (easter.toMillis() < ref.startOf("day").toMillis()) {
    easter = computeEasterSunday(ref.year + 1, zone);
  }
  return easter;
}

const COMMON_PHRASE_RULES = [
  {
    slug: "new_years_eve",
    label: "New Year's Eve",
    confidence: 0.95,
    patterns: [
      /\bnew[\s-]*year'?s?\s*eve\b/i,
      /\bnye\b/i,
      /\bsilvester\b/i,
    ],
    resolve: (ref, zone) => {
      const start = nextFixedDate(ref, zone, 12, 31);
      return start ? { start } : null;
    },
  },
  {
    slug: "new_years_day",
    label: "New Year's Day",
    confidence: 0.95,
    patterns: [
      /\bnew[\s-]*year'?s?\s*day\b/i,
      /\bnew[\s-]*year(?!'?\s*eve)\b/i,
    ],
    resolve: (ref, zone) => {
      const start = nextFixedDate(ref, zone, 1, 1);
      return start ? { start } : null;
    },
  },
  {
    slug: "christmas_eve",
    label: "Christmas Eve",
    confidence: 0.9,
    patterns: [
      /\bchristmas\s+eve\b/i,
      /\bxmas\s+eve\b/i,
      /\bheiligabend\b/i,
    ],
    resolve: (ref, zone) => {
      const start = nextFixedDate(ref, zone, 12, 24);
      return start ? { start } : null;
    },
  },
  {
    slug: "christmas_day",
    label: "Christmas Day",
    confidence: 0.9,
    patterns: [
      /\bchristmas\b/i,
      /\bxmas\b/i,
      /\bweihnachten\b/i,
    ],
    resolve: (ref, zone) => {
      const start = nextFixedDate(ref, zone, 12, 25);
      return start ? { start } : null;
    },
  },
  {
    slug: "boxing_day",
    label: "Boxing Day",
    confidence: 0.9,
    patterns: [/\bboxing\s+day\b/i],
    resolve: (ref, zone) => {
      const start = nextFixedDate(ref, zone, 12, 26);
      return start ? { start } : null;
    },
  },
  {
    slug: "valentines_day",
    label: "Valentine's Day",
    confidence: 0.9,
    patterns: [/\bvalentine'?s?\s+day\b/i],
    resolve: (ref, zone) => {
      const start = nextFixedDate(ref, zone, 2, 14);
      return start ? { start } : null;
    },
  },
  {
    slug: "halloween",
    label: "Halloween",
    confidence: 0.9,
    patterns: [/\bhalloween\b/i],
    resolve: (ref, zone) => {
      const start = nextFixedDate(ref, zone, 10, 31);
      return start ? { start } : null;
    },
  },
  {
    slug: "easter_weekend",
    label: "Easter Weekend",
    confidence: 0.9,
    patterns: [
      /\beaster\s+weekend\b/i,
      /\boster(n)?wochenende\b/i,
    ],
    resolve: (ref, zone) => {
      const easter = nextEasterSunday(ref, zone);
      if (!easter) return null;
      return {
        start: easter.minus({ days: 2 }),
        end: easter.plus({ days: 1 }),
      };
    },
  },
  {
    slug: "easter_sunday",
    label: "Easter Sunday",
    confidence: 0.9,
    patterns: [/\beaster\b/i, /\bostern\b/i],
    resolve: (ref, zone) => {
      const start = nextEasterSunday(ref, zone);
      return start ? { start } : null;
    },
  },
  {
    slug: "good_friday",
    label: "Good Friday",
    confidence: 0.9,
    patterns: [/\bgood\s+friday\b/i, /\bkarfreitag\b/i],
    resolve: (ref, zone) => {
      const easter = nextEasterSunday(ref, zone);
      if (!easter) return null;
      return { start: easter.minus({ days: 2 }) };
    },
  },
  {
    slug: "easter_monday",
    label: "Easter Monday",
    confidence: 0.9,
    patterns: [/\beaster\s+monday\b/i, /\bostermontag\b/i],
    resolve: (ref, zone) => {
      const easter = nextEasterSunday(ref, zone);
      if (!easter) return null;
      return { start: easter.plus({ days: 1 }) };
    },
  },
  {
    slug: "pentecost",
    label: "Pentecost",
    confidence: 0.85,
    patterns: [/\bpentecost\b/i, /\bwhitsun\b/i, /\bpfingsten\b/i],
    resolve: (ref, zone) => {
      const easter = nextEasterSunday(ref, zone);
      if (!easter) return null;
      return { start: easter.plus({ days: 49 }) };
    },
  },
];

function normalizeCommonPhrase(phrase, ref, zone) {
  const trimmed = (phrase || "").trim();
  if (!trimmed) {
    return {
      normalizedPhrase: "",
    };
  }
  for (const rule of COMMON_PHRASE_RULES) {
    if (!rule.patterns.some((regex) => regex.test(trimmed))) {
      continue;
    }
    const resolved = rule.resolve(ref, zone);
    if (!resolved || !resolved.start || !resolved.start.isValid) {
      continue;
    }
    const confidenceOverride =
      resolved.confidence ?? rule.confidence ?? 0.9;
    const isoDate = resolved.start.toISODate();
    return {
      normalizedPhrase: resolved.start.toISODate(),
      startOverride: resolved.start,
      endOverride: resolved.end,
      appliedRule: rule.slug,
      explanation: `Preset phrase "${rule.label}" mapped to ${isoDate}.`,
      confidenceOverride,
    };
  }
  return {
    normalizedPhrase: trimmed,
  };
}

const SEARCHAPI_DURATION_DEFAULT = 7;
const SEARCHAPI_DURATION_TWO_WEEK = 14;
const SEARCHAPI_DURATION_WEEKEND = 3;
const SEARCHAPI_DEFAULT_ROLLING_MONTHS = 6;
const SEARCHAPI_MAX_MONTHS_AHEAD = 6;

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
  const normalization = normalizeCommonPhrase(trimmed, ref, zone);
  const chronoSource = normalization.normalizedPhrase || trimmed;

  try {
    const parsed = chrono.parse(chronoSource, ref.toJSDate(), {
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

    let startDate = DateTime.fromJSDate(start.date(), { zone });
    if (normalization.startOverride) {
      startDate = normalization.startOverride;
    }
    const result = {
      success: true,
      phrase: trimmed,
      isoDate: startDate.toISO({ suppressMilliseconds: true }),
      isoDateUTC: startDate.toUTC().toISO({ suppressMilliseconds: true }),
      isoDateOnly: startDate.toISODate(),
      isoTime: startDate.toISOTime({ suppressMilliseconds: true }),
      timeZone: zone,
      referenceDate: ref.toISO(),
      confidence: Number(
        (
          normalization.confidenceOverride ??
          extractConfidence(start)
        ).toFixed(2)
      ),
      explanation: normalization.explanation
        ? normalization.explanation
        : best.text
          ? `Interpreted "${best.text}" relative to ${ref.toISODate()}`
          : "Interpreted using chrono-node default parser",
    };
    if (normalization.appliedRule) {
      result.preset = normalization.appliedRule;
    }

    if (normalization.endOverride) {
      const endDate = normalization.endOverride;
      result.endIsoDate = endDate.toISO({ suppressMilliseconds: true });
      result.endIsoDateUTC = endDate.toUTC().toISO({ suppressMilliseconds: true });
      result.endIsoDateOnly = endDate.toISODate();
      result.endIsoTime = endDate.toISOTime({ suppressMilliseconds: true });
    } else if (best.end) {
      const endDate = DateTime.fromJSDate(best.end.date(), { zone });
      result.endIsoDate = endDate.toISO({ suppressMilliseconds: true });
      result.endIsoDateUTC = endDate.toUTC().toISO({ suppressMilliseconds: true });
      result.endIsoDateOnly = endDate.toISODate();
      result.endIsoTime = endDate.toISOTime({ suppressMilliseconds: true });
    }

    const searchApiMeta = deriveSearchApiMetadata(trimmed, ref, zone);
    if (searchApiMeta) {
      if (searchApiMeta.isoRange) {
        const [startIso, endIso] = searchApiMeta.isoRange.split("..");
        const startCandidate = DateTime.fromISO(startIso || "", { zone });
        const endCandidate = DateTime.fromISO(endIso || "", { zone });
        const clamp = clampRangeToSearchApiHorizon(startCandidate, endCandidate, ref);
        if (clamp) {
          searchApiMeta.isoRange = `${clamp.start.toISODate()}..${clamp.end.toISODate()}`;
        }
      }
      result.searchApi = searchApiMeta;
    }

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
function clampRangeToSearchApiHorizon(startDate, endDate, ref) {
  if (!startDate || !endDate) return null;
  const horizonEnd = ref.plus({ months: SEARCHAPI_MAX_MONTHS_AHEAD }).endOf("month");
  if (endDate <= horizonEnd) {
    return null;
  }
  if (startDate > horizonEnd) {
    const horizonStart = horizonEnd.startOf("month");
    return { start: horizonStart, end: horizonEnd };
  }
  return { start: startDate, end: horizonEnd };
}

function buildRollingRange(ref) {
  const start = ref.startOf("day");
  const end = ref.plus({ months: SEARCHAPI_DEFAULT_ROLLING_MONTHS }).endOf("month");
  const clamp = clampRangeToSearchApiHorizon(start, end, ref);
  if (clamp) {
    return `${clamp.start.toISODate()}..${clamp.end.toISODate()}`;
  }
  return `${start.toISODate()}..${end.toISODate()}`;
}

function detectYearNearIndex(text, index, defaultYear) {
  const after = text.slice(index);
  const yearMatch = after.match(/(\d{4})/);
  if (yearMatch) {
    const year = parseInt(yearMatch[1], 10);
    if (!Number.isNaN(year)) {
      return year;
    }
  }
  return defaultYear;
}

function detectMonthInPhrase(phrase, ref) {
  const lower = phrase.toLowerCase();
  for (const item of MONTH_DEFINITIONS) {
    for (const name of item.names) {
      const idx = lower.indexOf(name);
      if (idx >= 0) {
        let year = detectYearNearIndex(lower, idx + name.length, ref.year);
        const monthIndex = MONTH_SLUG_TO_INDEX.get(item.slug);
        if (year < ref.year || (year === ref.year && monthIndex < ref.month)) {
          year += 1;
        }
        const start = DateTime.fromObject({ year, month: monthIndex, day: 1 }, { zone: ref.zoneName });
        const end = start.endOf("month");
        return {
          slug: item.slug,
          month: monthIndex,
          year,
          isoRange: `${start.toISODate()}..${end.toISODate()}`,
        };
      }
    }
  }
  return null;
}

function deriveTripType(lowerPhrase) {
  if (/\bone[-\s]?way\b/.test(lowerPhrase) || /\bno return\b/.test(lowerPhrase)) {
    return "one_way";
  }
  if (/\bround[-\s]?trip\b/.test(lowerPhrase) || /\breturn\b/.test(lowerPhrase)) {
    return "round_trip";
  }
  return "round_trip";
}

function deriveDurationKind(lowerPhrase) {
  if (/(two|2)[\s-]?week/.test(lowerPhrase)) {
    return "two_week";
  }
  if (/weekend/.test(lowerPhrase)) {
    return "weekend";
  }
  if (/(one|1)[\s-]?week/.test(lowerPhrase) || /\ba week\b/.test(lowerPhrase)) {
    return "one_week";
  }
  return "one_week";
}

function deriveSearchApiMetadata(phrase, ref, zone) {
  const trimmed = phrase.trim();
  if (!trimmed) return null;
  const lower = trimmed.toLowerCase();
  const tripType = deriveTripType(lower);
  const durationKind = deriveDurationKind(lower);
  const monthData = detectMonthInPhrase(trimmed, ref);

  let timePeriodToken;
  let isoRange;
  let durationDays = SEARCHAPI_DURATION_DEFAULT;

  if (tripType === "one_way") {
    if (monthData) {
      timePeriodToken = `trip_in_${monthData.slug}`;
      isoRange = monthData.isoRange;
    } else {
      timePeriodToken = "trip_in_the_next_six_months";
      isoRange = buildRollingRange(ref);
    }
    durationDays = SEARCHAPI_DURATION_DEFAULT;
  } else {
    if (durationKind === "two_week") {
      durationDays = SEARCHAPI_DURATION_TWO_WEEK;
      if (monthData) {
        timePeriodToken = `two_week_trip_in_${monthData.slug}`;
        isoRange = monthData.isoRange;
      } else {
        timePeriodToken = "two_week_trip_in_the_next_six_months";
        isoRange = buildRollingRange(ref);
      }
    } else if (durationKind === "weekend") {
      durationDays = SEARCHAPI_DURATION_WEEKEND;
      if (monthData) {
        timePeriodToken = `weekend_in_${monthData.slug}`;
        isoRange = monthData.isoRange;
      } else {
        timePeriodToken = "weekend_trip_in_the_next_six_months";
        isoRange = buildRollingRange(ref);
      }
    } else {
      durationDays = SEARCHAPI_DURATION_DEFAULT;
      if (monthData) {
        timePeriodToken = `one_week_trip_in_${monthData.slug}`;
        isoRange = monthData.isoRange;
      } else {
        timePeriodToken = "one_week_trip_in_the_next_six_months";
        isoRange = buildRollingRange(ref);
      }
    }
  }

  if (!isoRange) {
    const startDate = DateTime.fromISO(trimmed, { zone });
    if (startDate.isValid) {
      const startBoundary = startDate.startOf("day");
      const endBoundary = startBoundary.plus({ days: durationDays }).endOf("day");
      const clamp = clampRangeToSearchApiHorizon(startBoundary, endBoundary, ref);
      if (clamp) {
        isoRange = `${clamp.start.toISODate()}..${clamp.end.toISODate()}`;
      } else {
        isoRange = `${startBoundary.toISODate()}..${endBoundary.toISODate()}`;
      }
    }
  }

  return {
    timePeriodToken,
    isoRange,
    tripType,
    durationDays,
  };
}
