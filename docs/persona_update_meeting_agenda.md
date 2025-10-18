# Persona/MEM Update Coordination – Meeting Prep

## Participants
- Product/Persona owner (Damjan)
- Bedrock contacts (Codex team)
- Action-Lambda owner
- Observers (analytics / replay ops)

## Proposed Agenda
1. **Replay Harness Status (5 min)**
   - Review dependency failure (diagnostic responses) and remediation options.
   - Confirm action-lambda payload requirements for replays.
2. **Persona Behaviour Review (10 min)**
   - Walk through comparison table (docs/persona_comparison.md).
   - Capture tone/memory changes desired per persona.
3. **Update Workflow (10 min)**
   - Discuss operating rhythm from docs/persona_memory_update_plan.md.
   - Decide staging alias usage and test coverage.
4. **Backlog Grooming (10 min)**
   - Review open items (docs/persona_update_backlog.md).
   - Assign owners / due-dates.
5. **Next Steps & Communications (5 min)**
   - Agree on timeline for next persona release.
   - Define reporting cadence (replay metrics, SNS alerts).

## Pre-Read
- docs/persona_comparison.md
- docs/persona_memory_update_plan.md
- docs/persona_update_notes_2025-10-18.md

## Open Questions for Meeting
- Do we need additional context/payload fields for replays to avoid diagnostic fallbacks?
- Preferred channel for replay failure alerts (SNS topic, OpsGenie, etc.).
- Any limits on prompt/memory size we should enforce when drafting updates.
