// server.mjs — serves static site and proxies /invoke to Bedrock Agent
// Node 20+
// npm i express cors dotenv @aws-sdk/client-bedrock-agent-runtime
import 'dotenv/config';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import express from 'express';
import cors from 'cors';
import { BedrockAgentRuntimeClient, InvokeAgentCommand } from '@aws-sdk/client-bedrock-agent-runtime';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
app.use(express.json());

const ORIGIN = process.env.ORIGIN || '*'; // set to your domain in production
app.use(cors({ origin: ORIGIN === '*' ? true : ORIGIN }));

// serve static
const pub = path.join(__dirname, 'public');
app.use(express.static(pub));

// Bedrock client
const REGION = process.env.AWS_REGION || 'us-west-2';
const AGENT_ID = process.env.AGENT_ID;
const AGENT_ALIAS_ID = process.env.AGENT_ALIAS_ID;
if (!AGENT_ID || !AGENT_ALIAS_ID) {
  console.warn('AGENT_ID and AGENT_ALIAS_ID are required. Set them in .env');
}
const client = new BedrockAgentRuntimeClient({ region: REGION });

app.post('/invoke', async (req, res) => {
  try {
    const { inputText, sessionId } = req.body || {};
    if (!inputText) return res.status(400).json({ error: 'inputText required' });

    const cmd = new InvokeAgentCommand({
      agentId: AGENT_ID,
      agentAliasId: AGENT_ALIAS_ID,
      sessionId: sessionId || crypto.randomUUID(),
      inputText,
      enableTrace: false,
    });
    const resp = await client.send(cmd);

    // collect streamed completion text
    let text = '';
    if (resp.completion) {
      const decoder = new TextDecoder();
      for await (const event of resp.completion) {
        if (event.chunk) text += decoder.decode(event.chunk.bytes, { stream: true });
      }
    }
    res.json({ text: text.trim() });
  } catch (e) {
    console.error('invoke error', e);
    res.status(502).json({ error: String(e) });
  }
});

// SPA fallback
app.get('*', (_, res) => res.sendFile(path.join(pub, 'index.html')));

const PORT = process.env.PORT || 8787;
app.listen(PORT, () => {
  console.log('Running on http://localhost:' + PORT);
  console.log('Agent:', AGENT_ID, 'Alias:', AGENT_ALIAS_ID, 'Region:', REGION);
});
