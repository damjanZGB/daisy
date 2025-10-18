# Persona & Memory Update Plan

## Goals
- Keep Bianca/Gina/Paul instruction sets aligned while preserving their Tonal differences.
- Coordinate updates with the Bedrock team so new prompt versions are versioned, tested, and rolled out consistently.
- Ensure replay telemetry, transcript corpus, and regression checks feed into every update cycle.

## Operating Rhythm
1. **Suggest Changes**
   - Capture issues from replay metrics, customer feedback, or business stakeholders.
   - File them in a dedicated persona-updates backlog (tag with persona + theme).
2. **Design Session**
   - Draft instruction deltas (tone, introductions, fallback wording, tool usage clarifications).
   - Review with Bedrock team for compliance, memory constraints, and guardrail consistency.
3. **Prototype & Test**
   - Create staging aliases for each persona and apply draft instructions.
   - Run automated replay (daisy-replay-lambda) on the staging alias + manual spot checks.
   - Monitor replay metrics: a change is acceptable only if ReplayFailed == 0 for the day and key transcripts show expected deltas.
4. **Approval Gate**
   - Document tone/behavior shifts, updated instructions, and expected outcomes in a change log.
   - Obtain sign-off from product owner + Bedrock team contact.
5. **Rollout**
   - Update production aliases sequentially (Paul → Bianca → Gina) while monitoring CloudWatch metrics, SNS alerts, and Amadeus proxy logs.
   - Pause if failure metrics spike.
6. **Post-Deployment Review**
   - Observe nightly replay metrics for 48 hours.
   - Record notable conversations in a knowledge base; feed insights back into backlog.

## Version Control
- Maintain ws/agent_*_instructions.md as source of truth.
- Tag releases in Git: persona-update-YYYYMMDD.
- Ensure Render proxies refresh their static instructions immediately after deploy.

## Memory Considerations
- Keep memory footprint minimal (avoid large inline examples).
- Align metadata (variant names, header subtitles, initial greetings) through config files rather than code diffs.

## Open Questions for Bedrock Team
- Desired memory span / conversation length per persona.
- Support for contextual examples or few-shot prompts within allowed limits.
- Guidance on collaborative updates if multiple accounts share the same agent ID.
