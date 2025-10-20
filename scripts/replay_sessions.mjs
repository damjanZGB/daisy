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
          if (ev.trace?.observation) {
            trace.push(ev.trace);
            fs.writeFileSync(path.join(OUTPUT_DIR, 'event_'+Date.now()+'.json'), JSON.stringify(ev, null, 2));
          }
          if (ev.trace?.invocation) {
            trace.push(ev.trace);
            fs.writeFileSync(path.join(OUTPUT_DIR, 'event_'+Date.now()+'.json'), JSON.stringify(ev, null, 2));
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
      throw e;
    }
  }
  throw lastErr;
}

async function run() {
  const results = [];
  for (const s of SESSIONS) {
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
      const { assistantText, sessionState, trace } = await invokeWithTrace(aliasId, bedrockSessionId, user, state);
      out.turns.push({ turn: i + 1, user, assistant: assistantText, trace });
      state = sessionState || state;
    }
    const file = path.join(OUTPUT_DIR, `${s.flight}_${s.aliasKey}_${Date.now()}.json`);
    fs.writeFileSync(file, JSON.stringify(out, null, 2));
    try {
      const inv = await client.send(new ListInvocationsCommand({ sessionIdentifier: bedrockSessionId }));
      fs.writeFileSync(file.replace('.json','_inv.json'), JSON.stringify(inv, null, 2));
      if (inv.invocationSummaries?.length) {
        const steps = await client.send(new ListInvocationStepsCommand({ sessionIdentifier: bedrockSessionId, invocationId: inv.invocationSummaries[0].invocationId }));
        fs.writeFileSync(file.replace('.json','_steps.json'), JSON.stringify(steps, null, 2));
      }
    } catch (e) {
      fs.writeFileSync(file.replace('.json','_inv_err.txt'), String(e));
    }
    results.push({ flight: s.flight, aliasKey: s.aliasKey, file });
    console.log(`Replayed ${s.flight}/${s.aliasKey} -> ${file}`);
  }
  const summary = path.join(OUTPUT_DIR, `summary_${Date.now()}.json`);
  fs.writeFileSync(summary, JSON.stringify(results, null, 2));
  console.log(`Summary: ${summary}`);
}

run().catch(err => {
  console.error('Replay failed:', err);
  process.exitCode = 1;
});


