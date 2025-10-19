import { BedrockAgentRuntimeClient, InvokeAgentCommand } from "@aws-sdk/client-bedrock-agent-runtime";

const REGION = process.env.AWS_REGION || "us-west-2";
const AGENT_ID = process.env.AGENT_ID || "JDLTXAKYJY";
const AGENT_ALIAS_ID = process.env.AGENT_ALIAS_ID || "R1YRB7NGUP";
const SESSION_ID = `debug-${Math.random().toString(16).slice(2, 10)}`;

const client = new BedrockAgentRuntimeClient({ region: REGION });

async function invokeAgent(inputText, sessionState) {
  const command = new InvokeAgentCommand({
    agentId: AGENT_ID,
    agentAliasId: AGENT_ALIAS_ID,
    sessionId: SESSION_ID,
    inputText,
    enableTrace: false,
    sessionState,
  });
  const response = await client.send(command);
  if (!response.completion) {
    return "";
  }
  let text = "";
  const decoder = new TextDecoder();
  for await (const event of response.completion) {
    if (event.chunk?.bytes) {
      text += decoder.decode(event.chunk.bytes, { stream: true });
    }
    if (event.outputText?.items?.length) {
      text += event.outputText.items.map(item => item.text || "").join("");
    }
    if (event.contentBlock?.text) {
      text += event.contentBlock.text;
    }
  }
  return text.trim();
}

async function main() {
  const defaultOrigin = process.argv[2] || "ZAG";
  const defaultOriginLabel = process.argv[3] || "Zapresic, Croatia";
  const baseState = {
    sessionAttributes: {
      default_origin: defaultOrigin,
    },
    promptSessionAttributes: {
      default_origin: defaultOrigin,
      default_origin_label: defaultOriginLabel,
    },
  };

  console.log("Session:", SESSION_ID);

  let reply = await invokeAgent("Hi", baseState);
  console.log("1:", reply);

  reply = await invokeAgent("Trip to Zurich on next Saturday.");
  console.log("2:", reply);

  reply = await invokeAgent("Default is ok.");
  console.log("3:", reply);

  reply = await invokeAgent("I don't know");
  console.log("4:", reply);

  reply = await invokeAgent("Zapresic, Croatia");
  console.log("5:", reply);
}

main().catch(error => {
  console.error("Invocation failed:", error);
  process.exitCode = 1;
});
