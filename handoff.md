## Project Handoff

### Mission Snapshot
- Deliver a Lufthansa Group conversational agent that always returns truthful, Lufthansa-aligned inspiration and flight search results.
- Hard guarantees:
  - All pricing in EUR.
  - Google Flights/Calendar requests force `hl=en`, `gl=de`.
  - Google Explore requests force `hl=en-GB`, `gl=DE`, `currency=EUR`, and `included_airlines=STAR_ALLIANCE`.
  - Explore responses are filtered to Lufthansa Group carriers (`LH`, `LX`, `OS`, `SN`, `EW`, `4Y`, `EN`) and surfaced as such.
- Stack: Render-hosted proxy + SearchAPI microservice in front of an Amazon Bedrock agent, with an AWS Lambda action group (Python) performing Amadeus and SearchAPI orchestration.

### AWS & Bedrock Context
- Region / Account: `us-west-2`, AWS account `083756035354`.
- Primary Bedrock agent ID: `JDLTXAKYJY`.
  - Aliases (agent versions): Bianca → `D84WDVHMZR` (v99), Paul → `R1YRB7NGUP` (v97), Gina → `UY0U7MRMDK` (v98), draft/test alias `TSTALIASID`.
- Lambda action groups:
  - `daisy_in_action` (`8QPJXRIZN5`) – flight search, bookings, explore bridging.
  - `DestinationRecommender` (`DFAEASLNVH`) – inspiration + itinerary aggregation.
  - Supporting groups (`TimePhraseParser`, `UserInputAction`) unchanged.
- IAM execution role: `service-role/daisy_in_action-0k2c0-role-FGL64GQRD5P`.
- Environment allowlists: `AGENT_ID_ALLOWLIST` / `AGENT_ALIAS_ALLOWLIST` in `aws/env_update.json`. Add new Bedrock IDs here before deploying to avoid runtime rejects.

### System Architecture Overview
```
Frontend persona (React)  ->  Render proxy (proxy.mjs)
                               |-> /google/* -> google-api.mjs (SearchAPI)
                               |-> /tools/*  -> origin-daisy microservices (Amadeus etc.)
                               \-> InvokeAgent (Bedrock agent)

Bedrock action group (daisy_in_action) -> aws/lambda_function.py
                                           |-> Amadeus flight search
                                           |-> Google Flights / Calendar
                                           \-> Google Explore (STAR ALLIANCE upstream, LH-only downstream)
```
- Shared data: `data/lh_destinations_catalog.json` (lambda inspiration source) and `iata.json` (nearest-airport lookup for proxy + lambda).
- Logging: Lambda → CloudWatch; optional tool IO copies to `s3://origin-daisy-bucket/debug-tool-io/YYYY/MM/DD/...`.

### Runtime Defaults & Filtering
- **Locale & currency**
  - Flights/Calendar: `currency=EUR`, `hl=en`, `gl=de`.
  - Explore: `currency=EUR`, `hl=en-GB`, `gl=DE`.
- **Alliance handling**
  - Every Explore request sets `included_airlines=STAR_ALLIANCE`.
  - Lambda filters Explore results to Lufthansa Group carriers and annotates each candidate/option with `alliance="STAR ALLIANCE"` and `presentedCarriers="Lufthansa Group"`.
- **Follow-on flight search**
  - Explore candidates and itinerary options include a `flightSearchRequest` dict (origin/destination, dates, `lhGroupOnly`, etc.) so the Bedrock agent can immediately launch `search_flights` when a user picks an option.
- **Messaging**
  - Inspiration header: “Inspiration — STAR ALLIANCE options (filtered to Lufthansa Group carriers)”.
  - Follow-up line invites the traveller to widen to full STAR ALLIANCE or stay Lufthansa Group-only.

### Key Components

#### Lambda (`aws/lambda_function.py`)
- `_fetch_explore_candidates` normalises search parameters, applies documented defaults, enforces the STAR ALLIANCE filter, removes `interests` when `travel_mode=flights_only`, and filters to Lufthansa Group carriers.
- `_call_proxy` auto-inserts `included_airlines=STAR_ALLIANCE` whenever the agent hits `/google/explore/search`.
- `recommend_destinations` merges Explore output with catalog fallback, builds scored candidates, attaches `flightSearchRequest` payloads to candidates/options, and crafts runway messaging.
- `google_search_flight_offers` & `_search_amadeus` handle actual flight retrieval; `_build_recommendation_message` formats timelines without fabricating segments.
- Unit tests: `tests/test_recommender.py` (particularly `TestExploreProxy`) confirm STAR ALLIANCE param injection, Lufthansa filtering, and the presence of `flightSearchRequest`.

#### Proxy (`proxy.mjs`)
- Node 18 Express service on Render; wraps `InvokeAgent`.
- Supplies persona + context (nearest airport, travel preferences) to Bedrock.
- Forwards `/google/*` to `google-api.mjs`, `/tools/*` to origin-daisy services, with local `/tools/iata/lookup`.
- Required env: `AWS_REGION`, `AGENT_ID`, `AGENT_ALIAS_ID`, `TOOLS_BASE_URL`, `GOOGLE_BASE_URL`, AWS credentials.

#### SearchAPI microservice (`google-api.mjs`)
- Enforces per-engine defaults (Flights/Calendar vs Explore).
- Adds `SEARCHAPI_KEY`; forwards the call to `https://www.searchapi.io/api/v1/search`.
- Deploy on Render service `google-api-daisy`; health check `GET /healthz`.

#### Supporting Assets
- `origin-daisy` Render services: Amadeus wrapper, PDF generator (`derDrucker`), `antiPhaser`, `s3escalator`.
- `data/lh_destinations_catalog.json` packaged with lambda for fallback inspiration.
- Scripts: `scripts/deploy_lambda.ps1`, `scripts/replay_sessions.mjs`, `scripts/debug_invoke_agent.mjs`, `scripts/run_hard_conversation.mjs`.

### Testing & Verification
- **Unit**: `py -3 -m unittest tests.test_recommender.TestExploreProxy`.
- **Lambda smoke**:
  - `aws lambda invoke --function-name daisy_in_action-0k2c0 --payload fileb://aws/invoke_func.json aws/out_func_test.json`
  - `aws lambda invoke --function-name daisy_in_action-0k2c0 --payload fileb://aws/invoke_reco.json aws/out_reco_test.json`
- **Manual agent test**: `node scripts/debug_invoke_agent.mjs --text "Show me winter getaways in February"` (requires AWS creds + Bedrock IDs).
- After redeploying Render services (proxy or google-api), wait ~5 minutes for cold starts before testing end-to-end.

### Deployment Runbooks
1. **Lambda (`daisy_in_action-0k2c0`)**
   - Optional: `python -m compileall aws/lambda_function.py`.
   - `scripts/deploy_lambda.ps1 -FunctionName daisy_in_action-0k2c0 -Region us-west-2`.
   - Update `aws/env_update.json` if new Bedrock agent/alias IDs are added; apply via `aws lambda update-function-configuration --environment file://aws/env_update.json`.
   - Tail CloudWatch logs `/aws/lambda/daisy_in_action-0k2c0`.
2. **SearchAPI microservice (`google-api-daisy`)**
   - Node 18 + `npm install`.
  - Redeploy via Render (“Manual Deploy → Clear build cache & deploy”); verify with `/healthz`.
3. **Proxy service**
   - Ensure env: `AGENT_ID`, `AGENT_ALIAS_ID`, `AWS_REGION`, `TOOLS_BASE_URL`, `GOOGLE_BASE_URL`, AWS creds.
   - Redeploy via Render and confirm readiness (first call may take ~60s).

### Observability & Troubleshooting
- Lambda logs identify key milestones: `Google Explore candidates prepared`, `Destination recommendations prepared`, `Function flight request prepared`.
- `DEBUG_TOOL_IO` uploads detailed tool inputs/outputs to S3 for auditing.
- Render logs capture proxy and microservice behaviour; sample transcripts stored under `frontend/*/logs/`.
- `docs/searchapi_google_explore_fix.md` documents SearchAPI quirks and required defaults (now explicitly noting STAR ALLIANCE usage).

### Known Issues / Follow-ups
- Ensure Render deployments stay current; lambda changes alone are insufficient.
- Add integration coverage verifying that selecting an Explore option triggers `search_flights` using `flightSearchRequest`.
- Monitor SearchAPI quota (HTTP 429) and add backoff if hit rates rise.
- Expand `AGENT_ID_ALLOWLIST` / `AGENT_ALIAS_ALLOWLIST` when onboarding new Bedrock agent versions.
- Legacy tests in `tests/test_recommender` beyond `TestExploreProxy` remain pending refactor.

### Quick Reference
- Repo root: `C:\Users\Damjan\source\repos\daisy`
- Lufthansa Group carriers (downstream): `LH`, `LX`, `OS`, `SN`, `EW`, `4Y`, `EN`
- STAR ALLIANCE enforced upstream; Lufthansa-only presentation downstream
- Primary identifiers: agent `JDLTXAKYJY`; aliases Bianca `D84WDVHMZR`, Paul `R1YRB7NGUP`, Gina `UY0U7MRMDK`, test `TSTALIASID`; action groups `8QPJXRIZN5` and `DFAEASLNVH`
- Key scripts: `scripts/deploy_lambda.ps1`, `scripts/replay_sessions.mjs`, `scripts/debug_invoke_agent.mjs`
- Health endpoints: `https://google-api-daisy.onrender.com/healthz`; proxy health depends on service logs

Keep this handoff updated after notable changes so the next Codex session can resume quickly with full context.
