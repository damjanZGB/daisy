# Project Handoff — Destination Recommender, Frontend PDF, and Replays

This document captures the current state, what’s live, the open issues, and a precise plan for continuing the work in a fresh Codex session.

## Summary Context
- Repo: `c:\Users\Damjan\source\repos\daisy`
- Region: `us-west-2`
- Bedrock Agent: `JDLTXAKYJY` (prepared)
- Action groups:
  - `daisy_in_action` (existing flight search)
  - `TimePhraseParser` (existing)
  - `DestinationRecommender` (new, id `DFAEASLNVH`, ENABLED)
- Lambda (executor for recommender + flight search): `daisy_in_action-0k2c0` (python 3.12)
  - Env: `PROXY_BASE_URL=https://origin-daisy.onrender.com`, `DEFAULT_CURRENCY=EUR`, `LH_GROUP_ONLY=true`
  - Recommender aggregator has time guards (overridable via env): `AGGR_TIME_BUDGET_S`, `AGGR_MAX_CALLS`

## What’s Implemented

### Recommender and Aggregator
- Catalog expanded to 93 destinations (in `data/lh_destinations_catalog.json`) with richer activity tags:
  - New: `diving`, `tuna_fishing`, `fishing`, `safari`, `wildlife`, `wildlife_photography`, `cycling`, `hunting`
  - Existing: `winter_sports`, `beach`, `city_break`
- Synonym canonicalization (`_canonicalize_theme_tags`):
  - ski/skiing/snowboard/alps → `winter_sports`; scuba/snorkeling → `diving`; tuna/big_game → `tuna_fishing`
  - bike/bicycle/mtb → `cycling`; safari/wildlife → `safari`; photography → `wildlife_photography`
- Scoring (`_score_destination`): theme match; temp/water for beach; snowReliability/elevation for winter_sports; activity boosts; carrier presence; distance penalty (heavier for `city_break`).
- Aggregator (`search_best_itineraries`):
  - Weekend‑biased ±14‑day sampling around mid‑month
  - Availability‑first sorting; composite ranking (price + stops + duration − availability)
  - Connections allowed by default; gentle nonstop boost only
  - Time budget + per‑call timeout + call caps; two‑phase sampling to avoid 90s Lambda timeout
- Formatter in `recommend_destinations`:
  - ASCII‑only; numbered “Good–Better–Best” headers + bold price, carriers, THEN lines (up to 3 legs)
  - Returns top 3 summarized options only; trims logs in recommender responses
  - Fallback list of candidates (clean bullets) if no flights yet

### Nonstop Fallback
- Flight search (OpenAPI + function‑details): if `nonstop=true` returns 0 offers, automatically retries with connections allowed.

### Frontend (Gina variant)
- `frontend/gina/index.html`:
  - Sanitizes `<user__askuser>` and `<sources>` markers before render.
  - Parses itinerary options from assistant message and, on a “confirm/book/hold” cue, generates a demo itinerary PDF (jsPDF) and posts a final assistant message with a download link.
  - Keeps bubble formatting clean (no duplicate numbering or stray lines).

### Replay Harness
- `scripts/replay_sessions.mjs`:
  - Strips `<user__askuser>` and `<sources>` from streamed text (`sanitizeAskUser`).
  - Retries on `DependencyFailedException`/timeout with small backoff.

## Current Issue (Open)
- Intermittent replay failure: `DependencyFailedException: The server encountered an error processing the Lambda response`.
  - Likely due to occasional large/complex payloads or non‑ASCII artifacts in function‑details TEXT responses.
  - We already:
    - Trimmed recommender debug logs; limited options to top 3 summarized; sanitized separators to ASCII only.
    - Added aggregator time guards and call caps.
  - Next mitigations (to do):
    - Add explicit response size logging before `_wrap_function` in recommender responses.
    - Optionally reduce displayed options to 2 (env toggle) if size ≥ threshold.
    - Add env `RECOMMENDER_VERBOSE=false` to skip THEN lines in replay mode.
    - If still failing, return a very compact summary + S3 link instead of verbose text.

## How To Run

### Deploy Lambda (zip already staged when you rebuild)
```
aws lambda update-function-code --function-name daisy_in_action-0k2c0 \
  --zip-file fileb://build/destination_recommender_lambda.zip --region us-west-2
```

### Replays (targeted and smoke)
```
$env:AWS_REGION='us-west-2'
$env:AGENT_ID='JDLTXAKYJY'
node scripts/replay_sessions.mjs
```
- Outputs: `analytics/replay/summary_*.json` and per‑scenario JSONs.

### CloudWatch Logs (Lambda)
```
aws logs describe-log-streams \
  --log-group-name /aws/lambda/daisy_in_action-0k2c0 \
  --order-by LastEventTime --descending --max-items 1 --region us-west-2

aws logs get-log-events \
  --log-group-name /aws/lambda/daisy_in_action-0k2c0 \
  --log-stream-name <latest-stream> --limit 200 --region us-west-2
```

## Next Actions (Ordered)
1) Run the 5‑scenario targeted replays and summarize results:
   - Skiing in December (winter_sports)
   - Scuba diving in March (diving)
   - Tuna fishing in July (tuna_fishing)
   - Safari in September (safari/wildlife/wildlife_photography)
   - Cycling camp in April (cycling)
   Summary per scenario: best price range, typical durations, availability hit rate, and representative option headers.

2) Stabilize recommender payloads in replay:
   - Add response‑size logging and an env toggle to switch to a compact message (no THEN lines, top 2 options) when size ≥ threshold.
   - Consider returning an S3 URL for full itinerary details for replay/stress modes.

3) Frontend itinerary download across personas:
   - Port Gina’s jsPDF logic and `<user__askuser>` sanitization to `frontend/bianca/index.html` and `frontend/paul/index.html`.
   - Optional: implement a server‑side PDF endpoint (Puppeteer) for multi‑page itineraries and share a signed URL.

4) “Wow” inspiration presets:
   - Add a simple “inspire me” preset module that triggers the recommender with curated multi‑activity templates (e.g., ski daytrip, surf daytrip, cycling camp, safari+falls loop, Azores tuna+whales), with suggested month windows.

## Files Touched (key)
- `aws/lambda_function.py` — recommender logic, aggregator, nonstop fallback, ASCII formatting.
- `data/lh_destinations_catalog.json` — 93 entries with expanded activities.
- `scripts/replay_sessions.mjs` — sanitization + retry.
- `frontend/gina/index.html` — formatting cleanup, PDF link on “confirmed/hold/book”.

## Checkpoints for the Next Codex
- Keep recommender outputs small and ASCII only; return max 2–3 options.
- If Bedrock still reports “error processing Lambda response,” temporarily switch recommender to a minimal candidate list with a short prompt to confirm dates for flight search; then iterate on payload size.
- Once stable, port frontend PDF and sanitizers to Bianca/Paul, and optionally add server‑rendered PDFs.

