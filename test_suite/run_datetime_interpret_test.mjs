import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

process.env.AGENT_ID ||= "test-agent";
process.env.AGENT_ALIAS_ID ||= "alias";
process.env.AWS_ACCESS_KEY_ID ||= "test";
process.env.AWS_SECRET_ACCESS_KEY ||= "test";
process.env.ORIGIN ||= "https://example.com";
process.env.SKIP_PROXY_SERVER = "1";

const { interpretDatePhrase } = await import("../proxy.mjs");

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const cases = [
  { phrase: "next Saturday", referenceDate: "2025-10-17", timeZone: "Europe/Zagreb" },
  { phrase: "25th of October", referenceDate: "2025-10-10", timeZone: "Europe/Zagreb" },
  { phrase: "25.10.2025" },
  { phrase: "December", referenceDate: "2025-10-17" },
  { phrase: "tomorrow", referenceDate: "2025-10-17" }
];

const results = cases.map(input => ({
  input,
  output: interpretDatePhrase(input)
}));

const outputPath = path.join(__dirname, "results_latest", "out_test_event_datetime.json");
fs.writeFileSync(outputPath, JSON.stringify({ timestamp: new Date().toISOString(), results }, null, 2));
console.log(`Stored datetime interpret test result at ${outputPath}`);
