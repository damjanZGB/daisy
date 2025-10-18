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
- [ ] Implement reporting that highlights failed utterances with recommended instruction updates.
- [ ] Create process for human review and controlled deployment of updated instructions/exemplars to Bedrock.
- [ ] Surface replay telemetry via CloudWatch metrics/SNS to highlight regression spikes.

## Bug Backlog
- [ ] Monitor /tools/datetime/interpret coverage for additional phrase patterns.
- [ ] Add automated conversation test ensuring inferred origin context reaches all personas.

