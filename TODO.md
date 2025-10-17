# Lufthansa Agent Reliability TODO

- [ ] Build `scripts/run_stress_tests.ps1` that executes the ladder in `docs/agent_stress_testing.md`, captures Lambda outputs, and flags failures automatically.
- [ ] Extend the proxy timeout or introduce streaming for long-haul Amadeus calls after measuring typical latency.
- [ ] Augment `_summarize_offers` to compute explicit per-segment durations for user-facing responses.
- [ ] Coordinate with the Bedrock team to plan persona/memory updates once Lambda stability is verified.
- [ ] (Continuous) After each deployment, run the automated stress suite and only promote when all scenarios pass and confidence ≥ 95%; revert immediately on regression.
