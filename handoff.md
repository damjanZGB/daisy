Handoff – Daisy Agent Platform (Updated 2025-10-24)
=================================================

This document gets the next Codex engineer productive fast. It covers live AWS context, code layout, toolchain updates, recent changes (Oct 24 redeploy), runbooks, and to-dos.

1. AWS / Bedrock Snapshot
-------------------------
- Region / Account: us-west-2 • 083756035354
- Bedrock Agent ID: JDLTXAKYJY
- Active aliases:
  - Bianca → D84WDVHMZR (agent version 99)
  - Paul   → R1YRB7NGUP (version 97)
  - Gina   → UY0U7MRMDK (version 98)
  - AgentTestAlias → TSTALIASID (current DRAFT)
- IAM Role used by Lambda: service-role/daisy_in_action-0k2c0-role-FGL64GQRD5P

2. Action Groups / Lambda
-------------------------
- DestinationRecommender (ID DFAEASLNVH)
  - Lambda: arn:aws:lambda:us-west-2:083756035354:function:daisy_in_action-0k2c0
  - payload: recommend destinations from bundled catalog + itineraries.
- daisy_in_action (ID 8QPJXRIZN5)
  - Handles flights, IATA lookup fallback, Google proxy calls.
  - Exposed OpenAPI route: /tools/amadeus/search (POST) running from origin proxy.
- TimePhraseParser (ID ASTBBKPZOE) and UserInputAction (ID QFT1TGMW5Y) remain unchanged.

Lambda Runtime (deployed Oct‑24 19:47 UTC)
- Runtime: Python 3.12 (handler lambda_function.lambda_handler)
- Key env vars:
  - PROXY_BASE_URL=https://origin-daisy.onrender.com
  - GOOGLE_BASE_URL=https://google-api-daisy.onrender.com
  - DEFAULT_CURRENCY=EUR, LH_GROUP_ONLY=true
  - ACTION_GROUP_NAME=daisy_in_action
  - ACTION_GROUP_RECOMMENDER=DestinationRecommender
  - RECOMMENDER_MAX_OPTIONS=10, RECOMMENDER_MAX_TEXT_BYTES=4000
  - DEBUG_TOOL_IO=true (uploads to s3://origin-daisy-bucket/debug-tool-io/YYYY/MM/DD/)
- Bundled data: data/lh_destinations_catalog.json (LH-only destinations)

Latest Lambda changes (Oct‑24):
- Auto route /google/explore/search to /google/flights/search when concrete outbound dates are provided (ISO or range). Interest terms are canonicalised (beach→beaches, ski→skiing) and HL default is en-US.
- _convert_explore_to_flights builds SearchAPI flights query (supports round trip when range includes return).
- _target_bases honours GOOGLE_BASE_URL priority.

3. Node Proxy (proxy.mjs)
-------------------------
- Runs on Render, port 8787 (Express + AWS SDK v3).
- Routes:
  - POST /invoke → Bedrock agent runtime (streaming).
  - POST /tools/amadeus/search → Amadeus adapter.
  - GET /tools/iata/lookup (deterministic local lookup).
  - Forwards /google/* to GOOGLE_BASE_URL (searchapi-powered microservice).
- CORS allowlist via ORIGIN env; body cap 1 MiB.
- Readiness probe caches config/iata/bedrock checks.
- New (Oct‑24): handleChat now returns 	oolResults so frontend can surface images/cards.

4. Google API microservice (google-api.mjs)
------------------------------------------
- Exposes /google/flights/*, /google/calendar/*, /google/explore/* via SearchAPI.io (key stored in env SEARCHAPI_KEY).
- Explore path defaults: adds engine, gl (from origin), hl=en, etc. Fallbacks ensure at least flights_only, adults=1, currency=EUR when unspecified.
- Flights/Calendar endpoints unchanged (pass-through to SearchAPI).

5. Frontend UIs (React + Tailwind)
-----------------------------------
- Supported variants: frontend/gina, frontend/bianca, frontend/paul, frontend/origin.
- Shared features:
  - persona.js attaches questionnaire persona state.
  - Chat log with PDF export (derDrucker feed).
  - New image carousel: toolResults cards (destinations) displayed using uildToolCards helper; cards show photo, region, price (formatted), airline.
  - Bubble component now accepts meta object with cards/pdf metadata.
- When deploying static sites (S3/CloudFront or Render static), ensure cache invalidation so new cards appear.

6. Toolchain / Data
--------------------
- Python deps inside Lambda layer: boto3 (managed by AWS runtime). All business logic inline.
- Proxy / microservices rely on Node 18 + npm modules (chrono-node, luxon, pdf-lib). Ensure 
pm install at repo root before running services locally.
- Local scripts:
  - scripts/deploy_lambda.ps1 builds dist/lambda.zip and runs ws lambda update-function-code.
  - Config env update stored in ws/env_update.json (GOOGLE_BASE_URL now included).

7. Testing / Diagnostics
-------------------------
- Unit tests (run with Python 3.13 on Windows):
  - python -m unittest tests.test_recommender.TestExploreProxy
- Known failing legacy tests: rest of 	ests/test_recommender rely on helper functions removed from lambda. They are ignored for now; do not delete.
- Hard-test transcripts: analytics/hard_tests/*.json (latest summary_1761302422834.json). Bianca fails due to upstream tool issue (DependencyFailedException) pre-dating current work.
- CloudWatch logs: /aws/lambda/daisy_in_action-0k2c0.

8. Deploy / Infra Ops
----------------------
- Lambda redeploy command (PowerShell, default profile):
  scripts/deploy_lambda.ps1 -FunctionName daisy_in_action-0k2c0 -Region us-west-2
- After redeploy, run smoke invoke:
  ws lambda invoke --function-name daisy_in_action-0k2c0 --payload fileb://aws/invoke_explore.json aws/invoke_explore_out.json
- Update env: ws lambda update-function-configuration --environment file://aws/env_update.json (contains GOOGLE_BASE_URL).
- Proxy / google-api microservices hosted separately (Render). To update, push new code and redeploy service.

9. Recent Oct‑24 Work Summary
------------------------------
- Added Google Flights auto-switch in lambda + interest canonicaliser.
- Proxy now attaches 	oolResults to final JSON; downstream UIs show destination images.
- Frontends (all personas) render SearchAPI imagery, keep PDF flow intact, preserve persona log capture.
- Redeployed Lambda 2025-10-24 ~19:47 UTC, validated explore+dates returns flights data.
- Explore / flights tool calls enforce Lufthansa Group airlines and clamp responses to 10 results.

10. Outstanding & Next Steps
-----------------------------
- Bianca hard-test still fails due to DependencyFailedException from earlier run; rerun smoke tests after next agent session.
- Consider re-enabling full `tests.test_recommender` by porting helper functions to a utility module.
- Monitor SearchAPI quotas; add alerting if 401/429 increase.
- Implement explore fallback per `docs/searchapi_google_explore_fix.md` (map “default” origins to real IATA + valid `gl`).
- Future improvement: surface toolResults in transcript uploads for audit trail.

11. Quick Start Checklist for Next Agent
----------------------------------------
1. Clone repo, 
pm install for microservices, ensure Python 3.12+ available.
2. Export AWS credentials with rights to Lambda & CloudWatch.
3. Run python -m unittest tests.test_recommender.TestExploreProxy.
4. For UI updates, open rontend/<persona>/index.html using React dev server or serve static build.
5. When testing explore scenarios, supply ISO dates so lambda selects Google Flights endpoint.
6. Always redeploy using scripts/deploy_lambda.ps1 and confirm with test invoke + CloudWatch tail.

Contact & Notes
---------------
- Repo location: C:\Users\Damjan\source\repos\daisy
- Persona instructions ASCII copies under ws/agent_*_instructions_ascii.md.
- Debug S3 bucket: origin-daisy-bucket (debug-tool-io prefix). Requires least-priv role to inspect.
- Slack/Email integrations not configured (manual log download via frontend).

Good luck and keep the Lufthansa experience delightful! 😊
