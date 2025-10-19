# Persona Memory Upgrade – Immediate Actions (2025-10-19)

## 1. Validate Instruction Updates
- Deploy refreshed instructions (Leo/Aris/Mira/Paul) to staging aliases so the revamped flight-list format and Lufthansa-only language go live.
- Run `daisy-replay-lambda` against staging for `2025-10-18` transcripts; confirm every response keeps `lhGroupOnly=true` and follows the Markdown template (`THEN`, `NEXT DAY`, bold price line).
- Capture two spot-check chats per persona to ensure tone + formatting survive real agent output; log any deviations in `docs/persona_update_backlog.md` (PU-003 owner).  

## 2. Mine Transcripts for Persona & Memory Signals
- Use the 13 replayed transcripts plus new staging outputs to tag:
  - Tone mismatches (e.g., LOT references, missing Lufthansa emphasis).
  - Memory cues that could be stored per persona (archetype, preferred tone, reminder to stay on-network).
- Record findings in the backlog (PU-004) with concrete instruction tweaks or memory candidates (e.g., persona-specific follow-up prompts).  

## 3. Prep the Persona Design Session
- Share pre-read pack: `docs/persona_comparison.md`, `docs/persona_memory_update_plan.md`, latest replay summary (this doc).
- Schedule the meeting using `docs/persona_update_meeting_agenda.md`; include staging owners + Bedrock contacts. Target date: 2025-10-21.
- Assign owners for: instruction drafting, memory schema review, staging validation, and rollout sign-off.  

## 4. Prototype & Test Instruction Deltas
- Draft focused tweaks per persona (e.g., Lufthansa-only reminder, empathetic promises, origin confirmation language).
- Apply to staging aliases sequentially; after each tweak:
  - Run the replay harness (13 transcripts) and the failure report:  
    `python scripts/report_replay_failures.py --bucket origin-daisy-bucket --prefix prod/replay-results --date YYYY-MM-DD`
  - Compare formatted outputs vs. template; archive example conversations for reference.
- When a persona passes replay + manual QA, mark the change ready for the approval gate in `docs/persona_memory_update_plan.md`.  

## 5. Reporting & Rollout Readiness
- Keep the replay failure digest and backlog up to date so leadership sees progress.
- Once all personas pass staging, follow the release rhythm: approvals → production alias updates → 48-hour monitoring.
- Capture lessons learned into the knowledge base to speed up future instruction iterations.  

Document owner: Codex (Persona Memory Workstream) – 2025-10-19.
