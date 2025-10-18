# Persona / Memory Update Prep – 2025-10-18

## Activities Performed
1. **Replay Harness Dry-Run**
   - Invoked `daisy-replay-lambda` for target date `2025-10-18` via the AWS CLI to validate nightly readiness.
   - Result: Lambda returned `dependencyFailedException` from the action Lambda.

2. **Action Lambda Log Review**
   - Pulled CloudWatch events for `/aws/lambda/daisy_in_action-0k2c0` around the failure window.
   - Observed multiple `"Falling back to diagnostic response"` entries, indicating the action Lambda did not recognise the replayed event shape and emitted the fallback payload instead of an itinerary.

3. **Backlog Update**
   - Logged `PU-001` in `docs/persona_update_backlog.md` capturing the dependency failure and asigning a follow-up with the action-Lambda owners.
   - Added reminder (`PU-002`) to keep encoding checks in scope for future instruction changes.

4. **Shared Artifacts**
   - Published `docs/persona_comparison.md` and `docs/persona_memory_update_plan.md` for team review ahead of the coordination meeting.

## Findings
- Replay harness currently exercises the production agent alias correctly but receives diagnostic fallbacks from the action Lambda. No transcripts were processed (variant stats empty).
- Historical transcript import succeeded (files present under `s3://origin-daisy-bucket/prod/<variant>/…`), so once the action Lambda responds normally we should see meaningful telemetry.

## Next Steps / Requests for Bedrock & Action-Lambda Owners
1. Inspect the latest invocation traces for `daisy_in_action-0k2c0` to determine why replayed inputs trigger the diagnostic path (likely missing expected request body fields).
2. Provide guidance on minimal payload requirements so replay harness can include any mandatory context (session attributes, parameters, etc.).
3. Once the action Lambda responds with real itineraries, rerun replay Lambda and verify CloudWatch metrics/SNS alerts fire as expected.
4. Schedule joint design session to discuss persona/memory deltas, using the comparison and update plan documents as pre-read.

## Open Questions
- Do we need to capture and forward `promptSessionAttributes` or other context from the original conversations for replays?
- Can we safely augment the replay payloads with a standard system prompt to avoid diagnostic fallbacks, or should the Lambda tolerate minimal inputs?

Document owner: Codex (Bedrock contacts) – 2025-10-18.
