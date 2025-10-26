# Project Handoff

## Mission Snapshot
- Goal: Lufthansa Group conversational agent that always returns truthful flight inspiration/search results with prices in EUR. Google Flights/Calendar calls force `hl=en`, `gl=de`, and Explore calls force `hl=en-GB`, `gl=DE`.
- Execution stack: Render-hosted proxy + microservices (Node/Express) fronting an Amazon Bedrock Agent, plus an AWS Lambda action group for Amadeus/Google flights.
- Current status: Currency/locale enforcement and return-segment handling fixed in code; Google microservice must still be redeployed on Render to pick up the locked parameters. This document replaces the deleted handoff.

## AWS & Bedrock Context
- Region / Account: `us-west-2` in AWS account `083756035354`.
- Primary Bedrock agent ID: `JDLTXAKYJY`. Active customer-facing aliases map to agent versions Bianca → `D84WDVHMZR` (v99), Paul → `R1YRB7NGUP` (v97), Gina → `UY0U7MRMDK` (v98). A draft/testing alias (`AgentTestAlias`) currently points to `TSTALIASID`.
- Lambda IAM execution role: `service-role/daisy_in_action-0k2c0-role-FGL64GQRD5P`.
- Action groups tied to the agent:
  - `daisy_in_action` (ID `8QPJXRIZN5`) — primary flights/search handler surfaced to the proxy as `/tools/amadeus/search`.
  - `DestinationRecommender` (ID `DFAEASLNVH`) — inspiration + itinerary bundler driven by the catalog.
  - Supporting groups `TimePhraseParser` (ID `ASTBBKPZOE`) and `UserInputAction` (ID `QFT1TGMW5Y`) remain unchanged.

## Architecture Map
- Frontend clients (`frontend/{gina,bianca,paul,origin}`) hit the proxy via `/invoke`.
- `proxy.mjs` (Render) wraps Bedrock `InvokeAgent`, handles session attributes, forwards tool calls to:
  - `TOOLS_BASE_URL` (Render origin-daisy) for internal tools (Amadeus, inspiration, calendar, etc.).
  - `google-api.mjs` (Render google-api-daisy) for SearchAPI-backed Google endpoints.
  - Local helpers (`/tools/iata/lookup`, `antiPhaser`, `derDrucker`, `s3escalator`).
- Bedrock agent invokes the AWS Lambda action group (`aws/deploy_action/lambda_function.py`) for real Amadeus/Google flight search and itinerary formatting.
- Shared data files: `iata.json` (proxy + lambda nearest-airport resolver) and `data/lh_destinations_catalog.json` (lambda inspiration catalog).

```
Client -> proxy.mjs -> Bedrock Agent <-> AWS Lambda (action group)
                      |             \-> google-api.mjs (SearchAPI)
                      \-> origin-daisy tools (Render microservices)
```

## Key Components

### Render Proxy (`proxy.mjs`)
- Hosted on Render (port 8787) using Express + AWS SDK v3; `npm start` launches it locally.
- Handles up to 6 Bedrock `InvokeAgent` hops per turn and streams responses; readiness bootstraps Bedrock, IATA data, and env config.
- Builds session attributes from persona payloads, geo hints, inferred airports (`/tools/iata/lookup`), and stores sanitized strings before passing to Bedrock.
- `executeInput` routes tool calls (`/tools/*`, `/google/*`, `antiPhaser`, `derDrucker`, `s3escalator`); Google calls respect the enforced EUR/de defaults.
- Returns `{ text, toolResults }` so frontends can render destination cards alongside the chat transcript.
- Enforces fallback origin (`DEFAULT_ORIGIN_FALLBACK`, default FRA) and applies 1 MiB JSON body cap plus CORS allowlist via `ALLOWED_ORIGINS`.
- Logs warnings for persona/location issues; inspect Render logs for `[proxy]` lines.

### Google SearchAPI Microservice (`google-api.mjs`)
- Express service; endpoint groups: `/google/flights`, `/google/calendar`, `/google/explore`.
- `callSearchApi` enforces `currency="EUR"`, `hl="en"`, `gl="de"` for Flights/Calendar lookups, and `currency="EUR"`, `hl="en-GB"`, `gl="DE"` for Explore; inbound overrides are ignored. Attaches `SEARCHAPI_KEY`.
- To redeploy on Render: rebuild container, or for static service use Render dashboard “Manual Deploy” → “Clear build cache & deploy”.
- Health check: `GET /healthz` returns `{ ok: true }`.

### AWS Lambda Action Group (`aws/deploy_action/lambda_function.py`)
- Python 3.12 runtime (handler `lambda_function.lambda_handler`); last production deploy: 2025-10-24 ~19:47 UTC.
- Handles Amadeus flight searches, SearchAPI-powered inspiration fallback, itinerary formatting, and catalog-based recommendations.
- Environment highlights (see `aws/env_update.json`): `PROXY_BASE_URL=https://origin-daisy.onrender.com`, `GOOGLE_BASE_URL=https://google-api-daisy.onrender.com`, `DEFAULT_CURRENCY=EUR`, `LH_GROUP_ONLY=true`, `ACTION_GROUP_NAME=daisy_in_action`, `ACTION_GROUP_RECOMMENDER=DestinationRecommender`, `RECOMMENDER_MAX_OPTIONS=10`, `RECOMMENDER_MAX_TEXT_BYTES=4000`, `DEBUG_TOOL_IO=true`, `DEBUG_S3_BUCKET=origin-daisy-bucket`, `DEBUG_S3_PREFIX=debug-tool-io`.
- Currency enforcement: all inbound/outbound paths coerce EUR; `_fmt_price` strips `$` prefixes and `_ensure_currency` overwrites stale values.
- Splits itineraries into Direct vs Connecting sections and prints THEN lines for each segment (outbound + return) to keep PDF generation happy.
- Loads `data/lh_destinations_catalog.json` at cold start and maps inspiration requests (themes canonicalised, Lufthansa Group carriers enforced).
- Supports SearchAPI fallbacks via `_proxy_get` with `GOOGLE_BASE_URL` override; inspiration without concrete return legs will not fabricate inbound segments.
- Deployment: run `scripts/deploy_lambda.ps1 -FunctionName daisy_in_action-0k2c0 -Region us-west-2` (PowerShell) or zip manually + `aws lambda update-function-code`. Update environment via `aws lambda update-function-configuration --environment file://aws/env_update.json`.

### Origin-Daisy Tool Service (Render)
- Handles primary `/tools` endpoints (Amadeus HTTP wrapper, recommendations, calendars, PDF generation triggers).
- Currency defaults to EUR; keeps parity with lambda formatting rules.
- Not in this repo—treat as external dependency reachable via `TOOLS_BASE_URL`.

### Supporting Microservices (Node)
- `antiPhaser.mjs`: Natural language date interpreter; used when agent needs explicit travel dates.
- `derDrucker.mjs`: Generates Markdown summaries and PDF tickets (`/tools/derDrucker/*`).
- `s3escalator.mjs`: Uploads transcripts/logs to S3 when `TRANSCRIPT_UPLOADER_*` env vars set.

### Frontends
- Static HTML + Tailwind clients (`frontend/{gina,bianca,paul,origin}`) sharing persona defaults via `frontend/persona.js`.
- Each UI renders chat bubbles, exports PDFs via `derDrucker`, and hosts an image carousel built by `buildToolCards` to display `toolResults` (destination photos, prices, airlines).
- Persona instructions are bundled as ASCII references under `aws/agent_*_instructions_ascii.md`.
- When deploying static assets (S3/CloudFront or Render static site), invalidate caches so new cards/styles appear immediately.

### Toolchain & Data
- Node deps at repo root (`package.json`): `express`, `chrono-node`, `luxon`, `pdf-lib`, AWS SDK v3, etc. Run `npm install` before launching proxy or microservices.
- Lambda bundles all Python logic inline (no external packages beyond boto3 provided by runtime).
- Destination catalog resides in `data/lh_destinations_catalog.json`; `scripts/enrich-iata.js` refreshes `iata.json` and `backend/iata.json`.
- Hard-test transcripts live under `analytics/hard_tests/`; use them when reproducing failures such as the Bianca `DependencyFailedException`.

## Conversation & Data Flow
1. Client POSTs `/invoke` with `text`, persona, and optional geo.
2. Proxy enriches session attributes (persona, origin label, nearest IATA, lat/lon).
3. Bedrock agent responds with text or `returnControl` instructions.
4. Proxy executes tool calls sequentially; Google Flights/Calendar requests enforce EUR/en/de while Explore requests enforce EUR/en-GB/DE automatically.
5. Tool results propagate back to Bedrock via `returnControlInvocationResults`.
6. Lambda formats flight lists into two sections (Direct / Connecting), enumerates options, and generates THEN lines for each segment. Currency is always EUR.
7. Proxy streams final text to frontend; PDF pipeline consumes the standardized layout.

## Flight Listing Rules (enforced via lambda + origin tools)
- Never fabricate itineraries: lists only when offers exist; otherwise respond with alternatives/clarifications.
- Partition Direct vs Connecting sections, both capped to 10 combined (`RECOMMENDER_MAX_OPTIONS`).
- Prices formatted with `_fmt_price`, always in EUR (`€123` or `123 EUR` depending on context); `$` symbols are stripped.
- Return handling: if `returnDate` provided or inbound legs exist, all segments appear in chronological order with THEN lines.
- Google inspiration: if catalog misses, lambda uses SearchAPI via google microservice; identical formatting rules apply.

## Deployment Runbook
- **Local setup**: `npm install`, then `npm run proxy` (or `npm start`) to run proxy + static UI; ensure `.env` contains Bedrock creds and base URLs.
- **Render services**:
  - Proxy: redeploy `proxy.mjs` service after edits; verify env vars (`TOOLS_BASE_URL`, `GOOGLE_BASE_URL`, AWS credentials).
  - Google microservice: redeploy whenever `google-api.mjs` changes; confirm `SEARCHAPI_KEY`, `SEARCHAPI_BASE`.
  - Support services (`antiPhaser`, `derDrucker`, `s3escalator`) redeploy similarly when touched.
- **AWS Lambda**:
  - Build zip: `python -m compileall aws/deploy_action` (optional), then zip entire folder contents (excluding tests, docs).
  - Deploy via `aws lambda update-function-code`.
  - Update environment variables with `aws lambda update-function-configuration` when toggling features (debug S3, timeouts).
  - Smoke invoke after deploy: `aws lambda invoke --function-name daisy_in_action-0k2c0 --payload fileb://aws/invoke_explore.json aws/invoke_explore_out.json`.
- **Config Guarantees**: Flights/Calendar calls always send `currency=EUR`, `hl=en`, `gl=de`; Explore calls always send `currency=EUR`, `hl=en-GB`, `gl=DE`. Any inbound request overriding them must be ignored (already enforced; document requirement in code reviews).

## Testing & Verification
- `npm test` (if configured) plus manual `node scripts/debug_invoke_agent.mjs --text "Find flights FRA to MLA in July"` to exercise the proxy locally.
- For lambda: `aws lambda invoke --function-name <name> --payload fileb://tests/payloads/sample.json out.json` then inspect `out.json`.
- Replay harness: `node scripts/replay_sessions.mjs` validates formatting and logs placeholder flags.
- Google microservice check: `curl https://google-api-daisy.onrender.com/google/flights/search?departure_id=FRA&arrival_id=MLA&outbound_date=2026-07-13` (response currency should be EUR, observe `search_parameters.currency`).
- IATA lookup sanity: `curl http://localhost:8787/tools/iata/lookup?lat=48.3538&lon=11.7861&limit=5`.
- Python unit coverage: `python -m unittest tests.test_recommender.TestExploreProxy` (other legacy `tests/test_recommender` cases are known to fail until helpers are restored—do not delete them).

## Observability & Debugging
- Render logs: Download via dashboard or saved `.log` files (`C:\Users\Damjan\Downloads\LH*.log`). Inspect request IDs to trace tool calls.
- Proxy traces `default_origin`, persona parsing, and tool execution errors. Look for `[proxy]` warnings.
- Lambda logs in CloudWatch (`/aws/lambda/daisy_in_action-*`). `DependencyFailedException` indicates Bedrock tool invocation timed out or microservice failed; retry or inspect downstream logs.
- Scripts:
  - `python scripts/extract_proxy_logs.py --minutes 30` to aggregate tool usage.
  - `node scripts/run_hard_conversation.mjs` for stress scenarios.
  - `python scripts/timephraseparser_smoke_tests.py` to validate date parsing behavior.
- S3 debug: Enable `DEBUG_TOOL_IO=true` and `DEBUG_S3_BUCKET` to capture tool payloads for post-mortem via S3.
  Debug outputs land under `s3://origin-daisy-bucket/debug-tool-io/YYYY/MM/DD/`.

## Known Issues & Follow-Ups
- Pending action: Redeploy the Render `google-api` service so production respects the locked Flights/Calendar (EUR/en/de) and Explore (EUR/en-GB/DE) params shipped in code.
- Consider adding integration tests covering:
  1. Inspiration flow with catalog miss → Google SearchAPI fallback.
  2. No-return scenarios to ensure no phantom return section appears.
- Monitor Bedrock rate limits; retry logic currently lives in lambda for Amadeus API errors but not for SearchAPI.
- Validate locale enforcement after redeploy—some historic responses still show `$` signs due to stale deployments.
- Bianca hard-test transcript still fails with `DependencyFailedException`; rerun smoke tests after next agent session and capture logs if the error persists.
- Legacy `tests/test_recommender` cases remain skipped/broken; port missing helpers if you need broader regression coverage.
- Keep an eye on SearchAPI quota usage (401/429 spikes); add alerting or fallbacks per `docs/searchapi_google_explore_fix.md` if rates increase.

## Quick Reference
- Proxy env essentials: `AWS_REGION`, `AGENT_ID`, `AGENT_ALIAS_ID`, `TOOLS_BASE_URL`, `GOOGLE_BASE_URL`, `IATA_DB_PATH`.
- Google microservice env: `PORT`, `SEARCHAPI_KEY`, `SEARCHAPI_BASE`, `ALLOWED_ORIGINS`.
- Lambda env toggles: `DEBUG_TOOL_IO`, `DEBUG_S3_BUCKET`, `RECOMMENDER_VERBOSE`, `GOOGLE_SEARCH_TIMEOUT`.
- Primary scripts: `scripts/deploy_lambda.ps1`, `scripts/replay_sessions.mjs`, `scripts/run_hard_conversation.mjs`, `scripts/enrich-iata.js`.
- Bedrock reference IDs: Agent `JDLTXAKYJY`; aliases Bianca `D84WDVHMZR`, Paul `R1YRB7NGUP`, Gina `UY0U7MRMDK`; action groups `8QPJXRIZN5` (daisy_in_action) and `DFAEASLNVH` (DestinationRecommender).

Keep this document up to date after every significant architecture or workflow change to ensure future sessions can onboard quickly.
