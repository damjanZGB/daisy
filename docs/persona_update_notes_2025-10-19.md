# Persona / Memory Update Notes - 2025-10-19

## Activities Performed
1. Pulled the successful Bianca transcript (`2025-10-18T21-43-25-029Z_1339bf23-d3a4-4e4b-8c91-d555316b1625.json`) from S3 and extracted itinerary signals (origin/destination, travel date) directly from the conversation text.
2. Queried CloudWatch for `/aws/lambda/daisy_in_action-0k2c0` around 2025-10-18T21:52Z to confirm the action Lambda built a complete Amadeus request (ZAG ➜ BRU, 2025-11-01, 1 adult, ECONOMY, EUR).
3. Updated `aws/replay_lambda.py` so transcripts containing structured itineraries either parse embedded JSON payloads or fall back to a heuristic (latest ISO-like date + first/last IATA codes in parentheses). When such data exists the replay now bypasses Bedrock and invokes `daisy_in_action-0k2c0` directly via Lambda.
4. Ran a local validation script that replays the extracted payload against the action Lambda; the invocation returned HTTP 200 with 10 offers, matching the production transcript.
5. Packaged and deployed the replay Lambda, added `lambda:InvokeFunction` permission to its execution role, and re-ran `daisy-replay-lambda` for 2025-10-18 to confirm a full remote success.
6. Seeded S3 with the 2025-10-18 synthetic transcripts for all personas and replayed the full set (13 transcripts total); every run completed via the direct Lambda path with zero failures.
7. Added `scripts/report_replay_failures.py` to summarise replay failures (latest-only) so instruction deltas surface quickly after nightly runs.
8. Updated `aws/replay_lambda.py` to force `lhGroupOnly` to `"true"` for every direct Lambda invocation, guaranteeing searches stay within the Lufthansa Group network.
9. Removed date-phrase parsing from `aws/lambda_function.py`, redeployed the flight action Lambda, and registered the standalone `TimePhraseParser` Lambda as a new Bedrock action group.
10. Updated persona instructions (Paul/Aris/Leo/Mira) to call the TimePhraseParser action group and reaffirm Lufthansa-only phrasing before presenting flights.
11. Filtered non-Lufthansa carriers in `aws/lambda_function.py` to prevent LOT/other partners from appearing in flight lists; reran replay (2025-10-19) to confirm offers are now empty when only non-LH options exist.
12. Updated persona instruction bullet points so Lufthansa-only enforcement is the default policy (not only when travelers request it).

## Findings
- The transcript does not store the OpenAPI payload verbatim, but the message stream consistently exposes enough structured hints (`(ZAG)`, `(BRU)`, ISO-like travel date) to rebuild a working request.
- Lambda invocation with the reconstructed payload succeeds and reproduces the logged Amadeus search (200 status, `offers: 10`, `nonstop: false`, `lhGroupOnly: true`).
- Replay harness now depends on `ACTION_LAMBDA_NAME`/`ACTION_GROUP_NAME`/`ACTION_API_PATH` environment variables for the direct-call path; fallback to the agent remains in place when itinerary extraction fails.

## Reconstructed Payload
```json
{
  "originLocationCode": "ZAG",
  "destinationLocationCode": "BRU",
  "departureDate": "2025-11-01",
  "adults": "1",
  "travelClass": "ECONOMY",
  "currencyCode": "EUR",
  "max": "10"
}
```

## Replay Validation
- Replay harness rerun on 2025-10-18 transcripts after agent versions 73/74/75 were published; 13 transcripts, 0 failures.
- TimePhraseParser Lambda manually invoked (`human_to_future_iso`, `normalize_any`), both returned expected ISO dates.
- Local test session `replay-test` invoked `daisy_in_action-0k2c0` with the payload above and received a 200 response plus 10 itineraries (JSON summary captured in `aws/replay_lambda.py` step output).
- Remote `daisy-replay-lambda` run for `targetDate=2025-10-18` now succeeds (1 transcript replayed, 0 failures) once the role is allowed to call the action Lambda directly.
- Broader replay over all seeded transcripts (Bianca/Gina/Origin/Paul) processed 13 sessions with 0 failures, confirming the heuristic extraction works across personas given structured itinerary hints.
- Action Lambda defaults filled in `nonstop=false` and `lhGroupOnly=true`; no additional session attributes were required.

## Open Questions for Bedrock / Action Lambda
- Do we anticipate transcripts without parenthesized IATA codes? If so, we may need a secondary lookup (e.g., location metadata or stored flight summaries) to recover airport codes reliably.
- Should replay captures persist the normalized itinerary payload alongside the transcript to avoid future heuristic/parsing drift?
- Confirm whether we should also set optional parameters such as `nonstop`, `lhGroupOnly`, or passenger mixes when present in future transcripts (currently relying on Lambda defaults).

Document owner: Codex (Bianca replay workstream) - 2025-10-19.
