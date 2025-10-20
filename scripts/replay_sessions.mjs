#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { TextDecoder } from 'node:util';
import { BedrockAgentRuntimeClient, InvokeAgentCommand, CreateSessionCommand, ListInvocationsCommand, ListInvocationStepsCommand } from '@aws-sdk/client-bedrock-agent-runtime';

const REGION = process.env.AWS_REGION || 'us-west-2';
const AGENT_ID = process.env.AGENT_ID || 'JDLTXAKYJY';
const OUTPUT_DIR = path.join(process.cwd(), 'analytics', 'replay');
fs.mkdirSync(OUTPUT_DIR, { recursive: true });

const ALIASES = {
  aris: process.env.ALIAS_ID_PAUL || 'R1YRB7NGUP',
  mira: process.env.ALIAS_ID_GINA || 'UY0U7MRMDK',
  leo: process.env.ALIAS_ID_BIANCA || 'D84WDVHMZR',
};

const SESSIONS = [
  {
    flight: 'LH6814',
    sessionId: '03b364a6-904f-4eba-ac72-9bc8bae96966',
    aliasKey: 'mira',
    steps: [
      'Hi',
      'I want to go to Zurich. Next saturday.',
      'Next saturday',
      'Closest ariport to me.',
      'Nearest airport to my location',
      'ZAG',
      'Yes',
      'Show me the alternatives',
      'Zagreb, zurich, 01.11.2025, one passenger return 03.11.2025',
      'Yes',
      'Show me the alternatives same departure airport same destination',
      // Force an explicit tool call for formatting verification
      'ZAG to ZRH, 2025-12-10 return 2025-12-12, 1 adult. Show options.',
    ],
  },
  {
    flight: 'LH1378',
    sessionId: '2a422dff-3f11-44e9-aee7-bdca3aa8ec0b',
    aliasKey: 'aris',
    steps: [
      'Hi',
      'I want to go to Zurich. Next saturday.',
      'Correct. Use default',
      'Zagreb to Zurich, departure, 25.10.2025',
      '01.11.2025',
      'Show me the alternatives',
      // Force an explicit tool call for formatting verification
      'ZAG to ZRH, 2025-12-10 return 2025-12-12, 1 adult. Show options.',
    ],
  },
  {
    flight: 'LH9999',
    sessionId: '0aae5c0f-61e4-407a-83cf-bda1d58cd552',
    aliasKey: 'leo',
    steps: [
      'Hi',
      'I want to go to Zurich. Next saturday.',
      'I can be connecting flight',
      'Show me the alternatives',
      'Show me the alternatives. Inspire me',
      'Yes. Show me the alternatives',
      // Force an explicit tool call for formatting verification
      'ZAG to ZRH, 2025-12-10 return 2025-12-12, 1 adult. Show options.',
    ],
  },
  {
    flight: 'LH7777',
    sessionId: 'fa4a9a2f-6c3e-4a9e-9a9a-777777777777',
    aliasKey: 'aris',
    steps: [
      'Some warm place with beach in March next year',
      'Yes, show me a few options',
      'Option 1 looks good',
      // Force an explicit tool call for formatting verification
      'ZAG to ZRH, 2025-12-10 return 2025-12-12, 1 adult. Show options.',
    ],
  },
  {
    flight: 'LH8888',
    sessionId: 'fb5b9b3f-6c3e-4a9e-9a9a-888888888888',
    aliasKey: 'mira',
    steps: [
      'Cold place for skiing in January',
      'Prefer shortest travel time',
      'Hold the best value option',
      // Force an explicit tool call for formatting verification
      'ZAG to ZRH, 2025-12-10 return 2025-12-12, 1 adult. Show options.',
    ],
  },
  {
    flight: 'LH9998',
    sessionId: 'fc6c9c4f-6c3e-4a9e-9a9a-999899989998',
    aliasKey: 'leo',
    steps: [
      'Family city break in April weekend',
      'Any nonstop suggestions?',
      'Show me alternative dates',
      // Force an explicit tool call for formatting verification
      'ZAG to ZRH, 2025-12-10 return 2025-12-12, 1 adult. Show options.',
    ],
  },
];

const client = new BedrockAgentRuntimeClient({ region: REGION });
const decoder = new TextDecoder();

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function sanitizeAskUser(text) {
  if (!text) return '';
  let out = String(text);
  out = out.replace(/<user__askuser[^>]*question=\"([^\"]+)\"[^>]*>/gi, '$1');
  out = out.replace(/<\/user__askuser>/gi, '');
  out = out.replace(/<sources>[\s\S]*?<\/sources>/gi, '');
  out = out.replace(/\n{3,}/g, '\n\n');
  return out.trim();
}

// Formatting check for flight list responses
function assessFormatting(text) {
  const t = String(text || '');
  if (!t.trim()) return { ok: false, checks: { empty: true } };
  const normalized = t.replace(/\r\n?/g, '\n');
  // 1) or 1. or 1- style numbering at line start
  const hasNumbered = /^\s*\d+[\)\.-]\s+/m.test(normalized);
  // Lambda sections and alternative sections
  const hasSections = /(\n|^)(Direct Flights|Connecting Flights|Direct Alternatives|Connecting Alternatives)(\n|$)/.test(normalized);
  // Canonical THEN line (lambda) or generic THEN mention (fallback/LLM)
  const hasThenCanonical = /(^|\n)\s*-\s*THEN\s+[A-Z0-9]{1,3}\s*\d{1,5}\s+[A-Z]{3}\s+\d{2}:\d{2}\s*->\s*[A-Z]{3}\s+\d{2}:\d{2}/i.test(normalized);
  const hasThenGeneric = /\bTHEN\b/i.test(normalized);
  const hasThen = hasThenCanonical || hasThenGeneric;
  // Arrow either ASCII -> or unicode →
  const hasArrow = /(->|→)/.test(normalized);
  // Price: bold **123 EUR**, or currency symbol, or amount + ISO currency
  const hasPriceBold = /\*\*\s*[^*\n]*\d[\d.,]*\s*(EUR|USD|CHF|GBP)\s*\*\*/i.test(normalized);
  const hasPriceSym = /([€$£]\s?\d[\d.,]*)/.test(normalized);
  const hasPriceCode = /\b\d[\d.,]*\s*(EUR|USD|CHF|GBP|RSD|HRK)\b/i.test(normalized);
  const hasPrice = hasPriceBold || hasPriceSym || hasPriceCode;
  const checks = { hasNumbered, hasSections, hasThen, hasArrow, hasPrice };
  // Consider OK if we have either sections or numbering, plus either THEN or arrow, and a price
  const ok = (hasSections || hasNumbered) && (hasThen || hasArrow) && hasPrice;
  return { ok, checks };
}
async function invokeWithTrace(aliasId, sessionId, inputText, carryState) {
  const maxRetries = 2;
  let lastErr;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const cmd = new InvokeAgentCommand({ agentId: AGENT_ID, agentAliasId: aliasId, sessionId, inputText, enableTrace: true, sessionState: carryState });
      const response = await client.send(cmd);
      const trace = [];
      let assistantText = '';
      if (response.completion) {
        for await (const ev of response.completion) {
          if (ev.chunk?.bytes) {
            const txt = decoder.decode(ev.chunk.bytes, { stream: true });
            assistantText += txt;
          }
          if (ev.outputText?.items?.length) {
            assistantText += ev.outputText.items.map(i => i.text || '').join('');
          }
          if (ev.contentBlock?.text) {
            assistantText += ev.contentBlock.text;
          }
          if (ev.trace?.observation || ev.trace?.invocation) {
            trace.push(ev.trace);
            try { fs.writeFileSync(path.join(OUTPUT_DIR, 'event_'+Date.now()+'.json'), JSON.stringify(ev, null, 2)); } catch {}
          }
        }
      }
      assistantText = sanitizeAskUser(assistantText);
      return { assistantText, sessionState: response.sessionState, trace };
    } catch (e) {
      lastErr = e;
      const msg = String(e?.name || '') + ' ' + String(e?.message || '');
      if (/DependencyFailedException/i.test(e?.name || '') || /timeout/i.test(msg)) {
        await sleep(1500 + attempt * 1500);
        continue;
      }
      // Non-retryable error: return a graceful failure result to avoid aborting the run
      return { assistantText: '', sessionState: carryState, trace: [], error: String(e?.message || e) };
    }
  }
  // After retries, return a graceful failure
  return { assistantText: '', sessionState: carryState, trace: [], error: String(lastErr?.message || lastErr) };
}

async function run() {
  const results = [];
  for (const s of SESSIONS) {
    try {
      const aliasId = ALIASES[s.aliasKey];
      const create = await client.send(new CreateSessionCommand({}));
      const bedrockSessionId = create.sessionId;
      const out = { flight: s.flight, session: s.sessionId, bedrockSessionId, aliasKey: s.aliasKey, turns: [] };
      let state = {
        sessionAttributes: { default_origin: 'ZAG' },
        promptSessionAttributes: { default_origin: 'ZAG', default_origin_label: 'Zapresic, Croatia' },
      };
      for (let i = 0; i < s.steps.length; i++) {
        const user = s.steps[i];
        try {
          const { assistantText, sessionState, trace, error } = await invokeWithTrace(aliasId, bedrockSessionId, user, state);
          const fmt = assessFormatting(assistantText);
          out.turns.push({ turn: i + 1, user, assistant: assistantText, trace, error, format_ok: fmt.ok, format_checks: fmt.checks });
          state = sessionState || state;
        } catch (stepErr) {
          out.turns.push({ turn: i + 1, user, assistant: '', trace: [], error: String(stepErr) });
        }
      }
      const file = path.join(OUTPUT_DIR, `${s.flight}_${s.aliasKey}_${Date.now()}.json`);
      fs.writeFileSync(file, JSON.stringify(out, null, 2));
      try {
        // Small delay to allow trace/steps to persist server-side
        await sleep(600);
        const inv = await client.send(new ListInvocationsCommand({
          agentId: AGENT_ID,
          agentAliasId: aliasId,
          sessionIdentifier: bedrockSessionId,
        }));
        fs.writeFileSync(file.replace('.json','_inv.json'), JSON.stringify(inv, null, 2));
        if (Array.isArray(inv.invocationSummaries) && inv.invocationSummaries.length) {
          // Fetch steps for each invocationId to improve tool-call detection
          const allSteps = [];
          for (const s of inv.invocationSummaries) {
            try {
              const steps = await client.send(new ListInvocationStepsCommand({
                agentId: AGENT_ID,
                agentAliasId: aliasId,
                sessionIdentifier: bedrockSessionId,
                invocationId: s.invocationId,
              }));
              allSteps.push({ invocationId: s.invocationId, steps });
            } catch (stepErr) {
              allSteps.push({ invocationId: s.invocationId, error: String(stepErr) });
            }
          }
          fs.writeFileSync(file.replace('.json','_steps.json'), JSON.stringify(allSteps, null, 2));
        }
      } catch (e) {
        try { fs.writeFileSync(file.replace('.json','_inv_err.txt'), String(e)); } catch {}
      }
      results.push({ flight: s.flight, aliasKey: s.aliasKey, file });
      console.log(`Replayed ${s.flight}/${s.aliasKey} -> ${file}`);
    } catch (sessionErr) {
      const errFile = path.join(OUTPUT_DIR, `${s.flight}_${s.aliasKey}_${Date.now()}_error.txt`);
      try { fs.writeFileSync(errFile, String(sessionErr)); } catch {}
      results.push({ flight: s.flight, aliasKey: s.aliasKey, file: errFile, error: String(sessionErr) });
      console.warn(`Replay error ${s.flight}/${s.aliasKey}:`, sessionErr?.name || sessionErr);
    }
  }
  const summary = path.join(OUTPUT_DIR, `summary_${Date.now()}.json`);
  fs.writeFileSync(summary, JSON.stringify(results, null, 2));
  console.log(`Summary: ${summary}`);
}

run().catch(err => {
  console.error('Replay failed:', err);
  process.exitCode = 1;
});


