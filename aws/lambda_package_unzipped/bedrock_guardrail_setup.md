## Lambda Environment and Guardrail Setup

### 1. Configure Lambda environment variables

Use the provided `lambda_env_config.json` as the environment spec for your Lambda function that backs the Bedrock action group. Update the placeholder values before running the command.

```powershell
# Windows PowerShell example
$envFile = "c:\Users\Damjan\Downloads\dAisy - scripts and documents\lambda_env_config.json"
aws lambda update-function-configuration `
  --function-name YOUR_LAMBDA_NAME `
  --environment file://$envFile
```

Replace:
- `YOUR_LAMBDA_NAME` with the deployed Lambda function name.
- `PROXY_BASE_URL` inside the JSON with the publicly reachable proxy URL (for example, `https://api.daisy-proxy.yourdomain.com`).
- Adjust `DEFAULT_CURRENCY`, `LH_GROUP_ONLY`, or `AWS_REGION` if you operate in a different region or want another default currency.

### 2. Import the Bedrock guardrail

The repository already includes the guardrail definition at:

```
c:\Users\Damjan\Downloads\dAisy - scripts and documents\daisy_guardrails.json
```

Create or update the guardrail in Amazon Bedrock so that conversational boundaries match the Lambda logic.

```powershell
# Create a new guardrail
$guardrailFile = "c:\Users\Damjan\Downloads\dAisy - scripts and documents\daisy_guardrails.json"
aws bedrock create-guardrail `
  --name daisy-lhg-agent-guardrails `
  --guardrail-file file://$guardrailFile
```

If you already created the guardrail previously and just need to update it, use:

```powershell
aws bedrock update-guardrail-version `
  --name daisy-lhg-agent-guardrails `
  --guardrail-file file://$guardrailFile
```

### 3. Attach the guardrail to the Bedrock agent

After the guardrail exists, reference it in your Bedrock agent (via the console or `aws bedrock update-agent`) so every session inherits the persona, routing, and safety constraints described in the guardrail JSON.

```powershell
aws bedrock update-agent `
  --agent-id YOUR_AGENT_ID `
  --guardrail-identifier daisy-lhg-agent-guardrails
```

Replace `YOUR_AGENT_ID` with the identifier of your Bedrock agent.

### 4. Confirm runtime behaviour

1. Invoke the Lambda manually (via `aws lambda invoke`) using a sample event to ensure it resolves through the proxy and respects the 12‑month travel window.
2. Run an end-to-end Bedrock conversation and verify the guardrail enforces persona logging, Lufthansa-only routing, and safe-topic handling.
