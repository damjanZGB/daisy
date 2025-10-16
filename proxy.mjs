// proxy.mjs — proxy only (if you host static site elsewhere)
// Node 20+
// npm i express cors dotenv @aws-sdk/client-bedrock-agent-runtime
import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import { BedrockAgentRuntimeClient, InvokeAgentCommand } from '@aws-sdk/client-bedrock-agent-runtime';

const app = express();
app.use(express.json());
app.use(cors({ origin: process.env.ORIGIN || '*' }));

const REGION = process.env.AWS_REGION || 'us-west-2';
const AGENT_ID = process.env.AGENT_ID;
const AGENT_ALIAS_ID = process.env.AGENT_ALIAS_ID;

const client = new BedrockAgentRuntimeClient({ region: REGION });

app.post('/invoke', async (req, res) => {
  const { inputText, sessionId } = req.body || {};
  if (!inputText) return res.status(400).json({ error: 'inputText required' });
  try {
    const cmd = new InvokeAgentCommand({
      agentId: AGENT_ID,
      agentAliasId: AGENT_ALIAS_ID,
      sessionId: sessionId || crypto.randomUUID(),
      inputText,
      enableTrace: false,
    });
    const resp = await client.send(cmd);
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

const PORT = process.env.PORT || 8787;
app.listen(PORT, () => console.log('Proxy on http://localhost:' + PORT));
