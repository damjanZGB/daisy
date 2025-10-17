import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

process.env.AGENT_ID ||= "test-agent";
process.env.AGENT_ALIAS_ID ||= "alias";
process.env.AWS_ACCESS_KEY_ID ||= "test";
process.env.AWS_SECRET_ACCESS_KEY ||= "test";
process.env.ORIGIN ||= "https://example.com";
process.env.SKIP_PROXY_SERVER = "1";

const { iataLookup } = await import("../proxy.mjs");

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const coordinates = {
  latitude: 45.86790775647403,
  longitude: 15.790267703403517,
};

const matches = iataLookup({ lat: coordinates.latitude, lon: coordinates.longitude, limit: 5 });
const expected = "ZAG";
const topMatch = matches[0]?.code || null;
const pass = topMatch === expected;

const result = {
  timestamp: new Date().toISOString(),
  coordinates,
  expected,
  actual: topMatch,
  pass,
  matches,
};

const outputPath = path.join(__dirname, "results_latest", "out_test_event_iata_location.json");
fs.writeFileSync(outputPath, JSON.stringify(result, null, 2));
console.log(`Stored nearest-airport test result at ${outputPath}`);
if (!pass) {
  console.error(`Expected nearest airport ${expected} but received ${topMatch}`);
  process.exitCode = 1;
}
