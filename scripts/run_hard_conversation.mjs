#!/usr/bin/env node

/**
 * Executes a hard conversation scenario against each agent alias.
 * Exercises tricky date phrases and location questions (nearest airports).
 */

import fs from "node:fs";
import path from "node:path";
import { TextDecoder } from "node:util";
import { BedrockAgentRuntimeClient, InvokeAgentCommand } from "@aws-sdk/client-bedrock-agent-runtime";

const REGION = process.env.AWS_REGION || "us-west-2";
const AGENT_ID = process.env.AGENT_ID || "JDLTXAKYJY";
const REQUEST_TIMEOUT_MS = Number(process.env.REQUEST_TIMEOUT_MS || 240_000);

const ALIASES = [
  { key: "paul", aliasId: process.env.ALIAS_ID_PAUL || "R1YRB7NGUP", persona: "Paul" },
  { key: "bianca", aliasId: process.env.ALIAS_ID_BIANCA || "D84WDVHMZR", persona: "Bianca" },
  { key: "gina", aliasId: process.env.ALIAS_ID_GINA || "UY0U7MRMDK", persona: "Gina" },
];
const FILTER = (process.env.ONLY_ALIAS || "")
  .split(",")
  .map(s => s.trim().toLowerCase())
  .filter(Boolean);
const TARGET_ALIASES = FILTER.length
  ? ALIASES.filter(entry => FILTER.includes(entry.key))
  : ALIASES;

const BASE_STATE = {
  sessionAttributes: {
    default_origin: "ZAG",
  },
  promptSessionAttributes: {
    default_origin: "ZAG",
    default_origin_label: "Zapresic, Croatia",
  },
};

const SCENARIO = [
  "Hi! I'm planning a short-notice escape for two adults from Zapresic (default origin ZAG) to Zurich next Saturday evening, returning the following Monday around noon, Lufthansa Group flights only—use the nearest LH airport within 100 km if that helps.",
  "One more time: keep that default origin without asking for an IATA code, call the time tool for those dates, and move straight to itineraries.",
  "If Zurich is sold out, pivot to another Central European city that fits those dates and show the ISO-formatted dates you used.",
  "Please flag if any overnight return involves a layover longer than four hours so I can plan accordingly.",
];

const OUTPUT_DIR = path.join(process.cwd(), "analytics", "hard_tests");
fs.mkdirSync(OUTPUT_DIR, { recursive: true });

const client = new BedrockAgentRuntimeClient({ region: REGION });
const decoder = new TextDecoder();

async function collectTextAndState(response) {
  const parts = [];
  const askUser = [];
  if (response.completion) {
    for await (const event of response.completion) {
      if (event.chunk?.bytes) {
        parts.push(decoder.decode(event.chunk.bytes, { stream: true }));
      }
      if (event.outputText?.items?.length) {
        parts.push(event.outputText.items.map(item => item.text || "").join(""));
      }
      if (event.contentBlock?.text) {
        parts.push(event.contentBlock.text);
      }
    }
  }
  let combined = parts.join("").trim();
  const askUserTag = /<user[\w.\-]*askuser\b[^>]*question="([^"]+)"[^>]*\/?>/gi;
  combined = combined.replace(askUserTag, (_, question) => {
    const decoded = question
      .replace(/&quot;/g, '"')
      .replace(/&apos;/g, "'")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">");
    askUser.push(decoded);
    return decoded;
  });
  combined = combined.trim();
  if (!combined && askUser.length > 0) {
    combined = askUser[askUser.length - 1];
  }
  return {
    text: combined,
    askUser,
    sessionState: response.sessionState,
  };
}

async function sendWithTimeout(command) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await client.send(command, { abortSignal: controller.signal });
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error(`InvokeAgent timed out after ${REQUEST_TIMEOUT_MS / 1000}s`);
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

async function runScenarioForAlias(alias) {
  const sessionId = `${alias.key}-hard-${Date.now().toString(16)}`;
  const transcript = [];
  let carryState = BASE_STATE;
  try {
    for (let idx = 0; idx < SCENARIO.length; idx += 1) {
      const userText = SCENARIO[idx];
      const commandInput = {
        agentId: AGENT_ID,
        agentAliasId: alias.aliasId,
        sessionId,
        inputText: userText,
        enableTrace: false,
      };
      if (carryState) commandInput.sessionState = carryState;
      const command = new InvokeAgentCommand(commandInput);
      const response = await sendWithTimeout(command);
      const { text, askUser, sessionState } = await collectTextAndState(response);
      transcript.push({
        turn: idx + 1,
        role: "user",
        text: userText,
      });
      transcript.push({
        turn: idx + 1,
        role: "assistant",
        text,
        askUser,
      });
      if (sessionState && Object.keys(sessionState).length > 0) {
        carryState = sessionState;
      }
    }
    return { ok: true, sessionId, transcript };
  } catch (error) {
    transcript.push({
      turn: transcript.length / 2 + 1,
      role: "error",
      text: String(error),
    });
    return { ok: false, sessionId, transcript, error: String(error) };
  }
}

async function main() {
  const summary = [];
  for (const alias of TARGET_ALIASES) {
    const result = await runScenarioForAlias(alias);
    const filePath = path.join(OUTPUT_DIR, `${alias.key}_${result.sessionId}.json`);
    fs.writeFileSync(filePath, JSON.stringify(result, null, 2));
    summary.push({
      alias: alias.key,
      sessionId: result.sessionId,
      ok: result.ok,
      file: filePath,
      error: result.error || null,
    });
    console.log(`${alias.persona}: ${result.ok ? "PASS" : "FAIL"} (${result.sessionId}) -> ${filePath}`);
    if (result.error) {
      console.error(`  Error: ${result.error}`);
    }
  }
  const summaryPath = path.join(OUTPUT_DIR, `summary_${Date.now()}.json`);
  fs.writeFileSync(summaryPath, JSON.stringify(summary, null, 2));
  console.log(`Summary stored at ${summaryPath}`);
}

main().catch(error => {
  console.error("Fatal error running hard conversation scenarios:", error);
  process.exitCode = 1;
});
