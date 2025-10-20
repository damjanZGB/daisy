# Lufthansa Agent Reliability TODO

- [x] Build `scripts/run_stress_tests.ps1` that executes the ladder in `docs/agent_stress_testing.md`, captures Lambda outputs, and flags failures automatically.
- [ ] Extend the proxy timeout or introduce streaming for long-haul Amadeus calls after measuring typical latency (FRA - SYD currently ~8s; target new timeout 12s).
- [x] Augment `_summarize_offers` to compute explicit per-segment durations for user-facing responses.
- [ ] Coordinate with the Bedrock team to plan persona/memory updates once Lambda stability is verified.
- [ ] (Continuous) After each deployment, run the automated stress suite and only promote when all scenarios pass and confidence is at least 95%; revert immediately on regression.
- [ ] Backfill coordinates for airports missing from the OpenFlights dataset (~3.1k entries) so geolocation fallback covers every served city.

## NLP Feedback Loop Project
- [x] Define structured logging for chat transcripts (store in S3/location suitable for replay).
- [x] Build nightly/offline replay harness that feeds recorded turns back into the agent with varied phrasing to detect misunderstandings.
- [x] Implement reporting that highlights failed utterances with recommended instruction updates.
- [ ] Create process for human review and controlled deployment of updated instructions/exemplars to Bedrock.
- [x] Surface replay telemetry via CloudWatch metrics/SNS to highlight regression spikes.

## Bug Backlog
- [x] Retire /tools/datetime/interpret (handled by TimePhraseParser action group).
- [ ] Add automated conversation test ensuring inferred origin context reaches all personas.

## Project: Destination Recommender (Warm/Beach, Winter Sports, City Break, etc.)

Goal
- Turn fuzzy requests like “some warm place with beach in March next year” or “cold mountain for skiing in January” into concrete LH‑Group destination suggestions and real flight options from the traveler’s nearest origin — without changing agent instructions.

Deliverables
- A curated LH‑Group destination catalog with theme and seasonality data.
- Lambda function `recommend_destinations` that scores destinations by theme/month and returns top candidates.
- Lambda aggregator that searches Amadeus for top candidates and returns 3–5 “Good–Better–Best” itineraries with persuasive microcopy.
- Function schema added to `daisy_in_action` so Bedrock agents can call the recommender deterministically.
- Replay + stress tests covering “warm/beach” and “winter_sports” flows.

Implementation Plan (small steps)

1) Seed Data Catalog (repo)
- [x] Create `data/lh_destinations_catalog.json` with array of objects:
  - `code` (IATA), `city`, `country`
  - `tags`: e.g., ["beach","warm"], ["winter_sports","cold"], ["city_break"], ["surf"], ["family"], etc.
  - `avgHighCByMonth`: map "1".."12" → number
  - `waterTempCByMonth` (optional)
  - `elevationM` (optional), `snowReliability` (map) for winter sports scoring
  - `lhGroupCarriers`: ["LH","LX","OS","SN","EW","4Y","EN"] (most common brands to that destination)
  - `notes` (optional)
- [x] Populate 40-80 entries across themes:
  - Warm/beach (Spring): TFS, LPA, AGP, ALC, PMI, FNC, AYT, HRG, RMF
  - Winter sports (Jan–Feb): INN, SZG, ZRH, GVA, MUC, TRN, LYS, SOF, GNB
  - City break (mild spring): BCN, LIS, NCE, ATH, IST (filter via LH‑Group at runtime)
- [x] Add `scripts/validate_catalog.py` to sanity-check fields and month keys.

2) Lambda: Destination Recommender
- [x] In `aws/lambda_function.py` add handler branch `recommend_destinations` (function-details path):
  - Inputs: `originCode?`, `month?` or `monthRange?`, `themeTags[]` (e.g., ["beach","warm"]).
  - Normalize month (use TimePhraseParser Lambda when `month` is a phrase); derive `targetMonth` (1–12) and ISO year.
  - Load catalog (module‑level cache); filter by theme.
  - Score for March example (warm beach): tempScore (avgHighC≥20), waterScore (≥18), distancePenalty (haversine from origin), carrierBias (slight boost for LH‑Group presence).
  - Winter sports example: snowScore (snowReliability≥0.6 or elevation region), tempScore negative or ignored, distancePenalty, carrierBias.
  - Return top `N` candidates with `{ code, city, country, score, reason }`.

3) Lambda: Multi‑destination Flight Search
- [ ] Add helper `search_best_itineraries(origin, candidates[], monthRange, constraints)`
  - For each candidate, try 1–3 dates inside the month window (target day ±3 or nearest weekend).
  - Call existing `_proxy_post('/tools/amadeus/search', payload)` with LH‑Group filter and current currency.
  - Collect and rank by price and sensible total duration; keep 3–5 options.
- [ ] Format results per brand rules (numbered item + hyphen bullets + THEN lines + bold price). Add persuasive microcopy:
  - “Best Value” vs. “Shortest Travel Time” vs. “Flex”.
  - “Only a few seats left at this price” only if Amadeus indicates low availability (implement conservative heuristic; otherwise omit).
  - Close: “Shall I hold Option 1 for 15 minutes or adjust dates?”

4) Function Schema & Wiring
- [ ] Extend `daisy_in_action` action group with function schema for `recommend_destinations`:
  - Params: `originCode?` (string), `month?` (string), `monthRange?` (string), `themeTags` (array), `minAvgHighC?` (number), `maxCandidates?` (int, default 8).
  - Return: `candidates[]`.
- Note: initial schema file added at `aws/schemas/daisy_function_schema.json`.
- [ ] CLI update (DRAFT):
  - `aws bedrock-agent update-agent-action-group --agent-id <id> --agent-version DRAFT --action-group-id <id> --action-group-name daisy_in_action --function-schema file://aws/schemas/daisy_function_schema.json --region us-west-2`
  - `aws bedrock-agent prepare-agent --agent-id <id> --region us-west-2`
  - `aws bedrock-agent get-agent --agent-id <id>` → pick new version
  - `aws bedrock-agent update-agent-alias --agent-id <id> --agent-alias-id <alias> --routing-configuration '[{"agentVersion":"<new>"}]' --region us-west-2`

5) Proxy Preflight (optional, minimal)
- [ ] In `proxy.mjs` preflight: if input includes theme words + month phrases, inject system context lines summarizing interpreted destination/date to reduce clarifying questions.
- [ ] Keep it lightweight; the recommender remains in Lambda for determinism.

6) Observability / Debugging
- [ ] Keep `Tool I/O debug stored` S3 capture disabled by default; enable via env when needed (DEBUG_TOOL_IO=true, DEBUG_S3_BUCKET, DEBUG_S3_PREFIX) to store full request/response JSON for IATA/Amadeus.
- [ ] Add CloudWatch logs around recommender scoring: chosen tags, month, top candidates with scores.

7) Tests
- [ ] Unit tests for scoring: warm/beach March and winter_sports January — verify top 3 include expected airports.
- [ ] Unit tests for month parsing: “March next year” → correct year‑month; “January” in November → next January.
- [ ] Integration replay: add scenarios to `scripts/replay_sessions.mjs`:
  - “Some warm place with beach in March next year”
  - “Cold place for skiing in January”
  - “Family city break in April weekend”
- [ ] Confirm conversations return 3–5 options without asking for IATA or ISO dates and that the closing prompt nudges commitment.

8) Rollout
- [ ] Deploy Lambda changes (action group code) and verify via replay.
- [ ] Update action group function schema and publish new agent versions; move aliases.
- [ ] Run stress and replay suites; compare conversion‑oriented metrics (did the user pick one option more often?).

Quick Start for a new Codex session (empty context)
1. Open `docs/TODO.md` (this section) and scan “Project: Destination Recommender”.
2. Create `data/lh_destinations_catalog.json` with 10–15 seed entries (beach + winter_sports). Run `scripts/validate_catalog.py` if present.
3. In `aws/lambda_function.py`, add:
   - `recommend_destinations` function‑details handler
   - `search_best_itineraries` helper
   - Wire results into the existing OpenAPI/function responses (use existing note/message + caching of default_origin).
4. Update function schema (DRAFT), prepare agent, publish, switch alias.
5. Run replay for the three scenarios above. If good, expand catalog and tune scoring weights.


## STATUS UPDATE — Destination Recommender (handoff)

- Catalog
  - [x] Expanded to 93 entries; added activities: diving, tuna_fishing, fishing, safari, wildlife, wildlife_photography, cycling, hunting.
  - [x] Cycling tags added to Tenerife (TFS), Mallorca (PMI), Lanzarote (ACE).
  - [x] Safari gateways added: WDH, JNB, NBO, VFA, MQP (with wildlife photography tags).

- Lambda recommender
  - [x] `_canonicalize_theme_tags` maps skiing/scuba/tuna/bike/safari synonyms to catalog themes.
  - [x] `_score_destination` activity boosts + heavier city_break distance penalty.
  - [x] `search_best_itineraries` weekend‑biased ±14d sampling; availability-first ranking; composite scoring; time budget and call caps; gentle nonstop boost only.
  - [x] Nonstop fallback: if zero nonstop results, retry with connections allowed (function-details + OpenAPI).
  - [x] Formatter outputs clean ASCII message; top 3 options; THEN lines; fallback list when no flights.

- Frontend (Gina)
  - [x] Sanitizes `<user__askuser>` and `<sources>`; cleans blank lines.
  - [x] Parses itinerary options; after “confirm/book/hold” generates a demo itinerary PDF (jsPDF) and posts a final message with a download link.
  - [ ] Port same to Bianca and Paul.
  - [ ] Optional: server-side PDF endpoint and signed URL.

- Replay harness
  - [x] Sanitizes `<user__askuser>`; retries transient `DependencyFailedException`.
  - [ ] Targeted replays + summary for 5 scenarios: skiing (Dec), diving (Mar), tuna (Jul), safari (Sep), cycling (Apr).

- Known issue
  - [ ] Intermittent `DependencyFailedException: error processing Lambda response` during replay.
    - [ ] Add response-size logging and env toggles to reduce verbosity (drop THEN lines / max 2 options) when threshold exceeded; consider S3 link for full details.
    - [ ] Tune aggregator envs if needed: `AGGR_TIME_BUDGET_S`, `AGGR_MAX_CALLS`.




