#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { TextDecoder } from 'node:util';
import { BedrockAgentRuntimeClient, CreateSessionCommand, InvokeAgentCommand } from '@aws-sdk/client-bedrock-agent-runtime';

const REGION = process.env.AWS_REGION || 'us-west-2';
const AGENT_ID = process.env.AGENT_ID || 'JDLTXAKYJY';
const ALIAS_ID = process.env.ALIAS_ID_BIANCA || process.env.ALIAS_ID || 'D84WDVHMZR';

async function main() {
  const file = process.argv[2];
  if (!file) {
    console.error('Usage: node scripts/replay_one_transcript.mjs <transcript.json>');
    process.exit(2);
  }
  const raw = fs.readFileSync(file, 'utf8');
  const payload = JSON.parse(raw);
  const messages = Array.isArray(payload.messages) ? payload.messages : [];
  const userMsgs = messages.filter(m => (m?.role === 'user') && (m?.text));
  if (userMsgs.length === 0) {
    console.error('No user messages in transcript.');
    process.exit(1);
  }
  const client = new BedrockAgentRuntimeClient({ region: REGION });
  const create = await client.send(new CreateSessionCommand({}));
  const sessionId = create.sessionId;
  const decoder = new TextDecoder();
  let state = { sessionAttributes: { default_origin: 'ZAG' }, promptSessionAttributes: { default_origin: 'ZAG', default_origin_label: 'Zapresic, Croatia' } };
  const turns = [];
  for (let i = 0; i < userMsgs.length; i++) {
    const text = String(userMsgs[i].text);
    let assistantText = '';
    try {
      const cmd = new InvokeAgentCommand({ agentId: AGENT_ID, agentAliasId: ALIAS_ID, sessionId, inputText: text, enableTrace: false, sessionState: state });
      const response = await client.send(cmd);
      if (response.completion) {
        for await (const ev of response.completion) {
          if (ev.chunk?.bytes) assistantText += decoder.decode(ev.chunk.bytes, { stream: true });
          if (ev.outputText?.items?.length) assistantText += ev.outputText.items.map(it => it.text || '').join('');
          if (ev.contentBlock?.text) assistantText += ev.contentBlock.text;
        }
      }
      state = response.sessionState || state;
    } catch (e) {
      assistantText = `ERROR: ${e?.name || ''} ${e?.message || e}`;
    }
    turns.push({ turn: i + 1, user: text, assistant: assistantText });
    console.log(`TURN ${i + 1}\nUSER: ${text}\nASSISTANT:\n${assistantText}\n---`);
  }
  const outDir = path.join(process.cwd(), 'analytics', 'replay');
  fs.mkdirSync(outDir, { recursive: true });
  const outFile = path.join(outDir, `replayed_${path.basename(file).replace(/\.json$/i,'')}_${Date.now()}.json`);
  fs.writeFileSync(outFile, JSON.stringify({ file, agentId: AGENT_ID, aliasId: ALIAS_ID, sessionId, turns }, null, 2));
  console.log(`Saved: ${outFile}`);
}

main().catch(err => { console.error(err); process.exit(1); });

