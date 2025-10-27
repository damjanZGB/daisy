## Project Handoff

### Mission Snapshot
- Deliver a Lufthansa Group conversational travel assistant that only surfaces truthful, Lufthansa-aligned inspiration and flight results.
- Non-negotiables:
  - All prices expressed in EUR.
  - `/google/flights/*` and `/google/calendar/*` enforce `hl=en`, `gl=de`, `currency=EUR`.
  - `/google/explore/search` enforces `hl=en-GB`, `gl=DE`, `currency=EUR`, `included_airlines=STAR_ALLIANCE`.
  - Downstream messaging highlights Lufthansa Group carriers (`LH`, `LX`, `OS`, `SN`, `EW`, `4Y`, `EN`) even when STAR ALLIANCE is requested upstream.
- Architecture: browser persona UI → Render proxy (`proxy.mjs`) → SearchAPI microservice (`google-api.mjs`) / origin tool services / Bedrock Agent (`JDLTXAKYJY`) → AWS Lambda action groups (`aws/lambda_function.py`).

### AWS & Bedrock Context
- Region / Account: `us-west-2`, AWS account `083756035354`.
- Bedrock agent: `JDLTXAKYJY`.
  - Aliases (current versions): Bianca `D84WDVHMZR` (v99), Paul `R1YRB7NGUP` (v97), Gina `UY0U7MRMDK` (v98), draft/test `TSTALIASID`.
- Lambda action groups:
  - `daisy_in_action` (`8QPJXRIZN5`) – explore → flights orchestration, booking, Amadeus integration.
  - `DestinationRecommender` (`DFAEASLNVH`) – catalogue-driven inspiration aggregator.
  - Supporting groups (`TimePhraseParser`, `UserInputAction`) remain unchanged.
- IAM execution role: `service-role/daisy_in_action-0k2c0-role-FGL64GQRD5P`.
- Allowlist configuration for proxy → Bedrock: `aws/env_update.json` (`AGENT_ID_ALLOWLIST`, `AGENT_ALIAS_ALLOWLIST`). Update before deploying new aliases.

### System Architecture Overview
```
Frontend persona UI  ->  Render proxy (proxy.mjs)
                           |-> /google/* -> google-api.mjs (SearchAPI)
                           |-> /tools/*  -> origin-daisy microservices
                           \-> InvokeAgent (Bedrock JDLTXAKYJY)

Bedrock action group (daisy_in_action) -> aws/lambda_function.py
                                             |-> Google Explore/Flights/Calendar
                                             |-> Amadeus as fallback
                                             \-> Inspiration shaping and validation
```
- Shared data: `data/lh_destinations_catalog.json` (fallback inspiration), `iata.json` (nearest airport lookup for UI + proxy + lambda).
- Logging:
  - Lambda → CloudWatch (`/aws/lambda/daisy_in_action-0k2c0`).
  - Tool IO (when `DEBUG_TOOL_IO` enabled) → `s3://origin-daisy-bucket/debug-tool-io/YYYY/MM/DD/...`.
  - Frontends stream transcripts to `/log.php` and optional S3 via `transcriptUrl`.

### Component Summaries

#### Lambda (`aws/lambda_function.py`)
- `_normalize_flight_request_fields` maps alias parameters (`departure_id`, `arrival_id`, etc.) before defaults are applied.
- `_apply_contextual_defaults` fills missing origins using `sessionAttributes/promptSessionAttributes.default_origin`, coordinates (nearest airport lookup), or FRA fallback.
- `_fetch_explore_candidates` enforces STAR ALLIANCE parameters, shapes candidates, and attaches `flightSearchRequest` for immediate `/google/flights/search`.
- `recommend_destinations` merges SearchAPI results with catalogue fallback and produces Lufthansa-branded messaging.
- `google_search_flight_offers` and `_nearest_date_alternatives` handle actual flight retrieval; Amadeus remains as fallback.
- Key logs to watch: `OpenAPI flight request prepared`, `OpenAPI explore search success`, `Context origin substituted`, and any `_log` entries mentioning fabrication or empty offers.
- Tests: `tests/test_recommender.py::TestExploreProxy` (limited coverage). No automated verification yet for calendar search or itinerary PDF generation.

#### Proxy (`proxy.mjs`)
- Node 18 Express on Render; wraps Bedrock `InvokeAgent`.
- After 2025-10-27 update, mirrors default origin, label, and coordinates into both `sessionAttributes` and `promptSessionAttributes` so agent instructions can rely on `default_origin`.
- Implements `/tools/iata/lookup` directly using `iata.json`; forwards other `/tools/*` calls to origin microservices.
- Maintains `returnControlInvocationResults` between hops so tool traces remain attached.
- Environment: `AWS_REGION`, `AGENT_ID`, `AGENT_ALIAS_ID`, `TOOLS_BASE_URL`, `GOOGLE_BASE_URL`, AWS credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` when needed).

#### SearchAPI Microservice (`google-api.mjs`)
- Single Express service; enforces per-engine defaults, injects `SEARCHAPI_KEY`, and forwards to `https://www.searchapi.io/api/v1/search`.
- Deploy as Render service `google-api-daisy`; health endpoint `GET /healthz`.

#### Frontend Personas (`frontend/{bianca,gina,paul,origin}/index.html`)
- React (UMD) apps with Tailwind styling and persona banner.
- New behaviour (2025-10-27): chat textarea and send button remain disabled until geolocation/IP fallback resolves and a `defaultOrigin` is set. UI displays “Departure airport set to XXX” once unlocked; FRA used as final fallback.
- Payloads include location label, coordinates, resolved airport object, `defaultOrigin`, `inferredOrigin`, plus persona data via `frontend/persona.js`.
- Transcripts saved to `/log.php` and can be downloaded locally; PDF itinerary generation still wired through `derDrucker`.

#### Supporting Services
- `antiPhaser` (Render) parses natural language date ranges; proxy retries POST → GET and falls back cleanly.
- `derDrucker` produces itinerary PDFs.
- `s3escalator` handles optional escalation logging.
- Amadeus wrapper remains available for fallback search flows (avoid unless required).

### Deployment Runbooks
1. **Lambda (`daisy_in_action-0k2c0`)**
   - Optional compile: `python -m compileall aws/lambda_function.py`.
   - Deploy: `scripts/deploy_lambda.ps1 -FunctionName daisy_in_action-0k2c0 -Region us-west-2`.
   - Update environment allowlists via `aws lambda update-function-configuration --environment file://aws/env_update.json` when alias IDs change.
   - Monitor logs: `aws logs tail /aws/lambda/daisy_in_action-0k2c0 --follow`.
2. **SearchAPI microservice (`google-api-daisy`)**
   - Install deps (`npm install`), deploy via Render dashboard (clear build cache recommended).
   - Verify with `GET https://google-api-daisy.onrender.com/healthz`.
3. **Proxy service**
   - Ensure env vars (above) are set; deploy via Render dashboard.
   - First invocation may take ~60s due to cold start.
4. **Frontend bundles**
   - Each persona served statically (Render static site). Rebuild/redeploy when updating UI logic (default origin gating introduced 2025-10-27).

### Testing & Verification
- Unit: `py -3 -m unittest tests.test_recommender.TestExploreProxy`.
- Lambda smoke:
  - `aws lambda invoke --function-name daisy_in_action-0k2c0 --payload fileb://aws/invoke_func.json aws/out_func_test.json`
  - `aws lambda invoke --function-name daisy_in_action-0k2c0 --payload fileb://aws/invoke_reco.json aws/out_reco_test.json`
- Manual end-to-end:
  - `node scripts/debug_invoke_agent.mjs --text "Show me skiing in February"` (requires AWS credentials & allowlisted agent alias).
  - Browser persona UIs (Paul/Gina/Bianca). Confirm send button stays disabled until origin resolves and payload includes `defaultOrigin`.

### Observability & Troubleshooting
- CloudWatch logs for lambda show contextual substitutions, explore parameters, and flight search outcomes.
- Render logs cover proxy, SearchAPI, and microservices (`antiPhaser`, `derDrucker`, etc.).
- `frontend/*/logs/` captures past conversations (legacy sample data under `frontend/bianca/logs`).
- `docs/searchapi_google_explore_fix.md` documents SearchAPI quirks (notably “time period too far” handling).
- When investigating fabrication, pull the transcript (`Downloads\LHxxxx.log`) and correlate with CloudWatch `GetAgentSession` traces to confirm whether mandatory tool calls executed.

### Known Issues / Follow-ups
- **Bianca fabricating after selection**: Transcript `C:\Users\Damjan\Downloads\LH5239.log` (2025-10-27 08:21Z) shows placeholders despite instructions mandating `/google/flights/search`. Need to inspect Bedrock trace to verify whether the call was skipped or failed silently; root cause unresolved.
- **Default origin acknowledgement still inconsistent**: Some logs (e.g., `LH2039.log`) show Bianca re-asking for departure despite `default_origin` being injected into prompt/session attributes. Verify alias instructions consume `promptSessionAttributes` correctly or further reinforce acknowledgement in instructions.
- **Instruction/version drift**: Ensure latest `*_updated_instructions` (mandatory antiPhaser → explore → calendar → flights, tool fallback rules, no placeholders) are uploaded to Bedrock aliases after edits.
- **Testing coverage gap**: Only explore proxy path has unit tests. Add integration checks for calendar search, flights search, and PDF output before shipping major changes.
- **SearchAPI resilience**: Monitor for 4xx/5xx (notably “time period too far in the future”). Lambda currently retries with `one_week_trip_in_the_next_six_months`, but more robust backoff/alerts would help.
- **Render deployment discipline**: Repo changes have no effect until each Render service is redeployed; always redeploy SearchAPI, proxy, and relevant frontends after merging patches.

### Recent Updates (2025-10-27)
- Frontend personas updated to lock chat input until a default origin is inferred; UI now surfaces the chosen origin or FRA fallback.
- Proxy mirrors location context into both session and prompt attributes to support instruction compliance.
- `antiPhaser` microservice redeployed with six-month horizon; proxy retries POST and falls back to GET (with logging) on failure.
- Lambda explore shaping retains STAR ALLIANCE upstream but tags Lufthansa-only presentation downstream; `flightSearchRequest` includes origin from context.
- Instruction bundles (`gina_updated_instructions`, `bianca_updated_instructions`, `paul_updated_instructions`) refreshed with mandatory tool order (antiPhaser → explore → flights → calendar), STAR ALLIANCE messaging, anti-placeholder guidance, and Lufthansa-only emphasis.

### Quick Reference
- Repo root: `C:\Users\Damjan\source\repos\daisy`
- Key carriers (downstream presentation): `LH`, `LX`, `OS`, `SN`, `EW`, `4Y`, `EN`
- Primary identifiers: agent `JDLTXAKYJY`; aliases Bianca `D84WDVHMZR`, Paul `R1YRB7NGUP`, Gina `UY0U7MRMDK`, test `TSTALIASID`; action groups `8QPJXRIZN5`, `DFAEASLNVH`
- Key scripts: `scripts/deploy_lambda.ps1`, `scripts/replay_sessions.mjs`, `scripts/debug_invoke_agent.mjs`
- Health endpoints: `https://google-api-daisy.onrender.com/healthz` (SearchAPI). Proxy health via Render service logs.

Keep this document updated after notable changes so the next Codex session can resume with full context.
