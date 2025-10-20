Handoff - Daisy Agent and Action Groups (Updated 2025-10-20)

This document equips the next Codex session with all essentials to continue smoothly: which agent and aliases are live, action groups, Lambda, proxy, logging, verified behaviors, and follow-ups.

AWS / Bedrock Essentials
- Region: us-west-2
- Account: 083756035354
- Bedrock Agent ID: JDLTXAKYJY
- Aliases (live)
  - Bianca: D84WDVHMZR (routes to agent version 99)
  - Paul: R1YRB7NGUP (routes to agent version 97)
  - Gina: UY0U7MRMDK (routes to agent version 98)
  - AgentTestAlias: TSTALIASID (routes to DRAFT)

Action Groups
- DestinationRecommender (ID: DFAEASLNVH)
  - Purpose: Recommend destinations (catalog driven) and optionally enrich with itineraries.
  - Function: recommend_destinations (function-details)
  - Executor Lambda: arn:aws:lambda:us-west-2:083756035354:function:daisy_in_action-0k2c0
- daisy_in_action (ID: 8QPJXRIZN5)
  - Purpose: Flight shopping (Amadeus Flight Offers Search v2), IATA lookup fallback
  - OpenAPI route: /tools/amadeus/search (POST)
  - Executor Lambda: arn:aws:lambda:us-west-2:083756035354:function:daisy_in_action-0k2c0
- TimePhraseParser (ID: ASTBBKPZOE)
- UserInputAction (ID: QFT1TGMW5Y)

Lambda (Flight + Recommender)
- Name: daisy_in_action-0k2c0
- ARN: arn:aws:lambda:us-west-2:083756035354:function:daisy_in_action-0k2c0
- Runtime: Python 3.12 | Handler: lambda_function.lambda_handler | Timeout: 90s
- Environment
  - PROXY_BASE_URL=https://origin-daisy.onrender.com
  - DEFAULT_CURRENCY=EUR
  - LH_GROUP_ONLY=true
  - RECOMMENDER_VERBOSE=true
  - RECOMMENDER_MAX_OPTIONS=10
  - RECOMMENDER_MAX_TEXT_BYTES=4000
  - ACTION_GROUP_NAME=daisy_in_action
  - ACTION_GROUP_RECOMMENDER=DestinationRecommender
  - DEBUG_TOOL_IO=true
  - DEBUG_S3_BUCKET=origin-daisy-bucket
  - DEBUG_S3_PREFIX=debug-tool-io
- Bundled catalog: data/lh_destinations_catalog.json

Proxy (Node)
- File: proxy.mjs | Port: 8787
- Routes:
  - POST /invoke -> forwards to Bedrock Agent Runtime (signed)
  - POST /tools/amadeus/search -> Amadeus Flight Offers Search adapter
  - GET  /tools/iata/lookup -> local deterministic IATA resolver
- Amadeus timeout: 12000 ms (AbortController); Bedrock invoke streams events

Tool I/O Debug Capture (S3)
- Enabled (DEBUG_TOOL_IO=true)
- Bucket/prefix: s3://origin-daisy-bucket/debug-tool-io/YYYY/MM/DD/
- Contents: full request (proxy payload) and full response (simplified offers and raw Amadeus JSON)
- IAM: Bucket policy grants PutObject for Lambda role arn:aws:iam::083756035354:role/service-role/daisy_in_action-0k2c0-role-FGL64GQRD5P to debug-tool-io/*

Verified Behaviors (CloudWatch)
- Tools working: OpenAPI /tools/amadeus/search calls and recommender enrichment calls succeeded (200) and returned offers during sessions LH2943, LH6638, LH6119, LH4128.
- Function-details streaming: Bedrock may return only a final functionResponse (no streamed outputText). This previously led to “No text response.”
  - Implemented fix: proxy surfaces TEXT from functionResponse when streamed text is empty or is just a short heading; frontend logic no longer parses finalResponse (backend owns fallback).

Instruction Updates (ASCII variants)
- Files updated:
  - aws/Agent_instructions_ascii.md
  - aws/agent_aris_instructions_ascii.md
  - aws/agent_mira_instructions_ascii.md
  - aws/agent_leo_instructions_ascii.md
- Enforcements:
  - Tool-first policy; do not fabricate data; do not emit placeholders (Airport Name N, Airline Name N, EUR X.XX, X km, Notes: ...).
  - Reclassify comma-separated flight intents as full searches (resolve IATA -> TimePhraseParser -> Amadeus).
  - Flight numbers must include carrier + number with no space (LH612, OU324, OS654) in main items and THEN lines.
  - Presentation: ASCII arrow ->, uppercase THEN; sections “Direct Flights” and “Connecting Flights”.

Frontend (PDF and parsing)
- Files updated: frontend/paul/index.html, frontend/bianca/index.html, frontend/gina/index.html, frontend/origin/index.html
- Changes:
  - Sanitizer strips Markdown; parser captures legacy bullet lines (Departure/Arrival) and canonical THEN lines.
  - generatePdf constructs legs from bullets when needed; carrier+number without space; blanks instead of random gate/zone/seat/seq; multi-page generation fixed (one page per leg).
  - Trigger words include: confirm, confirmed, book, hold, pdf, download, itinerary, ticket, boarding pass.
  - Removed UI-level fallback for functionResponse; UI uses `text` from proxy only (backend-controlled).

Amadeus API Compatibility
- Requests sent through proxy use fields aligned with v2.8/2.9: originLocationCode, destinationLocationCode, departureDate, returnDate, adults, children, infants, travelClass, nonStop, currencyCode, includedAirlineCodes, excludedAirlineCodes, max.
- Proxy now forwards `includedAirlineCodes`/`excludedAirlineCodes` when provided and sets `Accept: application/vnd.amadeus+json`.
- Responses include simplified “offers” and “raw” canonical Amadeus payload (data[].itineraries[].segments[].carrierCode/number/departure/arrival).

Examples (S3 Debug Keys, 2025-10-20)
- debug-tool-io/2025/10/20/d6306f8ce0174299807632d88fb1111c_post.json
- debug-tool-io/2025/10/20/5438a519cdd2435ba9386bf5a2a8aae0_post.json
- debug-tool-io/2025/10/20/70eb15a2517a4cfeb6787f3814c67c00_post.json

CLI Snippets
- Tail logs: aws logs tail "/aws/lambda/daisy_in_action-0k2c0" --since 15m --follow --region us-west-2 --profile reStrike
- Filter logs by time: aws logs filter-log-events --log-group-name "/aws/lambda/daisy_in_action-0k2c0" --start-time <epoch_ms> --end-time <epoch_ms> --region us-west-2 --profile reStrike
- List alias/action groups:
  - aws bedrock-agent list-agent-aliases --agent-id JDLTXAKYJY --region us-west-2 --profile reStrike
  - aws bedrock-agent list-agent-action-groups --agent-id JDLTXAKYJY --agent-version <97|98|99> --region us-west-2 --profile reStrike
  - aws bedrock-agent get-agent-action-group --agent-id JDLTXAKYJY --agent-version <ver> --action-group-id <ID> --region us-west-2 --profile reStrike
- Fetch tool I/O debug: aws s3 ls s3://origin-daisy-bucket/debug-tool-io/2025/10/20/ --profile reStrike --region us-west-2

Open Items (handoff)
- DONE: Implement proxy fallback to surface TEXT from functionResponse when streamed text is empty/heading-only; removed frontend fallback.
- DONE: Extend replay tooling to count tool calls from CloudWatch and flag placeholder patterns.
