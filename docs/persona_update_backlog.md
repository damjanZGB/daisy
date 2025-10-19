# Persona Update Backlog

| ID | Persona(s) | Issue | Source | Proposed Next Step |
|----|-------------|-------|--------|--------------------|
| PU-001 | All | Replay harness reported dependencyFailedException when invoking alias (action Lambda error). | Replay Lambda test (2025-10-18) | ✅ Direct Lambda invoke path deployed 2025-10-19. Expand transcript coverage (add more successful logs) and monitor nightly EventBridge run; schedule follow-up if new payload variants surface. |
| PU-005 | All | Confirm TimePhraseParser action group stays healthy (periodic CLI invocation). | Ops (Oct 19) | Use `scripts/timephraseparser_smoke_tests.py` for daily Lambda checks; add scheduler + alerting hook. |
| PU-002 | Mira | Ensure mirrored instruction updates for accent characters (prevent encoding drift in UI). | Code review (Oct 18) | Include encoding QA checklist in next instruction rollout. |
| PU-003 | All | New flight-list formatting and Lufthansa-only guidance must be validated on staging personas. | Instruction update (2025-10-19) | Deploy refreshed instructions to staging aliases, run replay harness + spot checks, confirm bold price lines and `THEN`/`NEXT DAY` formatting render correctly. |
| PU-004 | All | Need structured review of 2025-10-18 transcripts to harvest persona tone/memory adjustments. | Replay batch (2025-10-19) | Assign reviewers to annotate transcripts for tonal gaps, then feed findings into the persona memory upgrade workshop. |
