# Persona Update Backlog

| ID | Persona(s) | Issue | Source | Proposed Next Step |
|----|-------------|-------|--------|--------------------|
| PU-001 | All | Replay harness reported dependencyFailedException when invoking alias (action Lambda error). | Replay Lambda test (2025-10-18) | ✅ Direct Lambda invoke path deployed 2025-10-19. Expand transcript coverage (add more successful logs) and monitor nightly EventBridge run; schedule follow-up if new payload variants surface. |
| PU-002 | Mira | Ensure mirrored instruction updates for accent characters (prevent encoding drift in UI). | Code review (Oct 18) | Include encoding QA checklist in next instruction rollout. |
