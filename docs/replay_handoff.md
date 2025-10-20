# Replay Harness Continuation Plan

## Context Recap
- The replay harness fails because the agent falls back to the diagnostic response (dependencyFailedException).
- A confirmed successful transcript lives at s3://origin-daisy-bucket/prod/bianca/2025/10/18/bianca/2025-10-18T21-43-25-029Z_1339bf23-d3a4-4e4b-8c91-d555316b1625.json.
- The action Lambda (daisy_in_action-0k2c0) works when it receives a full OpenAPI payload; the replay harness simply isn’t rebuilding that payload.

## Goal
Use the real transcript/logs to recover the Amadeus search parameters and update the replay harness to call the action Lambda directly without relying on Bedrock to infer them.

## Step-by-Step Instructions
1. **Inspect the transcript**
   - Download: ws s3 cp s3://origin-daisy-bucket/prod/bianca/2025/10/18/bianca/2025-10-18T21-43-25-029Z_1339bf23-d3a4-4e4b-8c91-d555316b1625.json local.json
   - Parse the JSON for origin, destination, departures, returns, passenger count, cabin, currency, and note that the conversation reached a successful flight listing (the log you shared).

2. **Pull the action-Lambda payload**
   - Search CloudWatch logs /aws/lambda/daisy_in_action-0k2c0 around 2025-10-18T21:52Z using logs filter-log-events.
   - Look for log lines such as OpenAPI request body parsed, Amadeus search request prepared to see the JSON properties the Lambda built.

3. **Update the replay harness**
   - In ws/replay_lambda.py, detect transcripts with structured itinerary info (you can match JSON blocks inside user messages).
   - When detected, bypass invoke_agent and directly call the action Lambda with an event shaped like 	est_suite/test_event1.json (include originLocationCode, destinationLocationCode, departureDate, eturnDate, dults, 	ravelClass, currencyCode).

4. **Local validation**
   - ws lambda invoke --function-name daisy_in_action-0k2c0 --payload fileb://payload.json response.json
   - Confirm the response contains actual flight options, not the diagnostic fallback.

5. **Redeploy & retest**
   - Zip and update the replay Lambda after the code change.
   - Invoke daisy-replay-lambda with 	argetDate=2025-10-18 and confirm the JSON summary shows transcripts replayed successfully.
   - Check CloudWatch for Amadeus search succeeded entries.

6. **Document outcomes**
   - Update docs/persona_update_notes_YYYY-MM-DD.md with the reconstructed payload, replay results, and any open questions for the Bedrock team.

## Additional Notes
- Relaxing the action Lambda validation is **not** recommended; supplying the true OpenAPI payload is the correct fix.
- Synthetic transcripts alone won’t work—this plan relies on the confirmed real transcript above.
- If additional payload variants are needed (e.g., round-trip vs one-way), repeat the process for other successful sessions once captured.

Provide this document to the next Codex session so they can continue the work.

## Updates (2025-10-20)

- Proxy fallback implemented for functionResponse-only or heading-only replies
  - File: `proxy.mjs:decodeAgentEventStream`
  - If Bedrock streams no `outputText`, or only a very short heading (e.g., ends with ":"), the proxy uses the final `functionResponse.responseBody.TEXT.body` as the `text` returned to clients.
  - Logs include `usedFunctionResponseFallback` and `textLen` for observability.

- UI simplification (backend-owned)
  - Files: `frontend/paul/index.html`, `frontend/bianca/index.html`, `frontend/gina/index.html`, `frontend/origin/index.html`
  - Removed UI-side finalResponse parsing; UIs now rely solely on backend-provided `text`.

- Replay analytics extensions
  - Placeholder detection added in `scripts/replay_sessions.mjs` per turn; flags `placeholders` plus `placeholder_flags` map.
    - Banned patterns flagged: "Airport Name N", "Airline Name N", "EUR X.XX", "X km", "Notes: ...", solitary "...", "TBD".
  - CloudWatch tool-call aggregation added in `scripts/extract_proxy_logs.py`:
    - Output now includes `aggregate` totals and per-session `counts` and `toolCalls` for IATA and Amadeus events.

- Amadeus request alignment (spec)
  - Proxy forwards `includedAirlineCodes`/`excludedAirlineCodes` when present and sets `Accept: application/vnd.amadeus+json`.

### Quick validation commands

1) Run replays to generate per-session JSON and a summary list:

   `node scripts/replay_sessions.mjs`

2) Aggregate proxy/Lambda logs for recent window (adjust minutes as needed):

   `python scripts/extract_proxy_logs.py --minutes 60`

   - Output: `analytics/replay/proxy_logs_summary.json` with `aggregate` and per-session `counts`.

3) Inspect placeholder flags and formatting in replay outputs:

   - Each `analytics/replay/<flight>_<alias>_*.json` includes `turns[].format_ok` and `turns[].placeholders`.
