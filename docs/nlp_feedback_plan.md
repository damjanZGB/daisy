# NLP Feedback Loop & Transcript Logging Plan

## Current state
- Chat transcripts are only persisted in the local `analytics/` directory via the web UI calling `/log.php`; nothing is stored in AWS-managed storage.
- CloudWatch holds Lambda debug output, but there is no structured transcript capture or replay harness.
- Instruction updates for the three personas (Aris, Leo, Mira) are hand-maintained; there is no automated way to evaluate their behaviour on historical conversations.

## Goals
1. Capture every traveller conversation (all personas) in a structured format that is easy to replay, analyse, and export.
2. Align storage and processing with AWS best practices (S3, KMS, IAM scoping, lifecycle policies).
3. Build a replay/validation workflow that can run nightly and surface regressions or instruction gaps automatically.
4. Keep personas isolated while applying identical tooling improvements.

## Target architecture (AWS aligned)
| Component | Responsibility | AWS best-practice notes |
|-----------|----------------|-------------------------|
| **Frontend** | Continue buffering conversation locally, but POST transcripts to the proxy when a session finishes (or on rolling checkpoints). Include persona ID, session/flight numbers, timestamps. | Minimise browser storage; keep payload JSON. |
| **Proxy (Node)** | Receive transcript payloads, enrich with request metadata (IP, user agent), and write to S3 using the AWS SDK v3 (`@aws-sdk/client-s3`). Reuse the existing SigV4 credentials already loaded for Bedrock. | - Write to `s3://<env>-daisy-transcripts/<date>/<persona>/<session>.json`\n- Enable server-side encryption (SSE-S3 or SSE-KMS).\n- Use conditional IAM policy limiting proxy role to `PutObject` in that bucket.\n- Add basic metric logging (CloudWatch Embedded Metrics) for success/failure counts. |
| **Transcript schema** | JSON envelope capturing persona, timestamps, geo inference, message list, and session metadata (Lambda sessionId, flight number). Include versioning. | Keep consistent with all personas; add schema version header. |
| **Replay harness** | AWS Step Functions (or EventBridge + Lambda) kicks off nightly job. Lambda downloads previous day’s transcripts from S3, replays them against the staging Bedrock agent, and logs pass/fail metrics. | - Store artefacts/results in another S3 prefix (`replay-results/`).\n- Publish CloudWatch metrics + alarms for regression rates. |
| **Reporting** | Generate summary report (JSON + optional HTML) listing failures, novel user phrases, and suggested instruction deltas. | Persist reports in S3 and optionally push to an SNS topic for notifications. |
| **Human review pipeline** | Maintain Git-versioned instruction files (one per persona) and require updates to be applied in all three files simultaneously. | Use CodeCommit/CodeBuild or existing Git workflow; ensure PR template includes “all personas updated”. |

## Implementation milestones
1. **Schema & bucket definition** (this sprint)\n   - Finalise transcript JSON schema.\n   - Define S3 bucket name, lifecycle (e.g., transition to Glacier after 90 days), and SSE policy.\n   - Update IaC or deployment notes with IAM permissions for the proxy role.
2. **Proxy write path**\n   - Add `/log/transcript` endpoint to `proxy.mjs` that validates payload (size, persona) and stores to S3 using `PutObject` with retry logic.\n   - Ensure logging is non-blocking for the user (queue or fire-and-forget with background flush).\n3. **Frontend integration**\n   - On session end (and optionally periodic checkpoints), send transcript payload to the proxy.\n   - Gate behind feature flag until full pipeline is ready.\n4. **Replay Lambda + Step Function**\n   - Implement Lambda to iterate over S3 objects, call Bedrock agent (staging alias), and record results.\n   - Set up Step Function orchestration with EventBridge nightly trigger.\n5. **Reporting & dashboards**\n   - Aggregate replay outcomes into CloudWatch metrics.\n   - Export summary JSON/CSV back to S3; notify via SNS if regressions >0.\n6. **Persona parity guardrails**\n   - Add automated test (e.g., simple script in `test_suite/`) that diffs key instruction sections across Aris/Leo/Mira to flag drift.\n   - Document workflow in `docs/` to ensure every instruction change touches all three files.

## Immediate action items
- [ ] Draft IAM/S3 requirements and add to deployment checklist.\n- [ ] Implement transcript schema and proxy S3 upload path.\n- [ ] Add frontend hook to transmit transcripts (behind feature flag) with persona metadata.\n- [ ] Prepare replay Lambda skeleton (Bedrock staging alias + logging).\n- [ ] Create automated check ensuring persona instruction files share mandatory sections (date interpretation, tooling order, etc.).

Tracking this plan will move the “NLP Feedback Loop Project” items in `docs/TODO.md` toward completion while keeping the agent’s evolution aligned with AWS best practices.
