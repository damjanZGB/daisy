# TimePhraseParser Smoke Test Runner

Daily health check for the `bedrock-time-tools` Lambda that backs the TimePhraseParser action group. The script makes sure both operations continue to resolve date phrases correctly before the travel agents rely on them.

## Script
- Location: `scripts/timephraseparser_smoke_tests.py`
- Requirements: Python 3.11+, `boto3`, AWS credentials with permission to invoke the Lambda.
- Default Lambda name: `bedrock-time-tools` (override with `--function`).

### Manual run
```bash
python scripts/timephraseparser_smoke_tests.py --region us-west-2
```

Useful flags:
- `--timezone Europe/Zurich` (default) adjusts the relative date expectation.
- `--qualifier <alias-or-version>` targets a published version before promoting it.
- `--json` emits structured results suitable for log aggregation.

## Expected behaviour
The runner currently issues two calls:
1. `human_to_future_iso` with the phrase `"next Saturday"` and confirms the ISO matches the next Saturday in the supplied timezone.
2. `normalize_any` with `"1 Nov 2025"` and verifies the ISO response equals `2025-11-01`.

On success the script exits `0`. Any failed check prints the Lambda payload for rapid debugging and exits `1`.

## Automation tips
1. **Windows Task Scheduler**
   - Action: `python <repo>\scripts\timephraseparser_smoke_tests.py --json > %TEMP%\time_tools_smoke.json`
   - Configure “Run whether user is logged on or not” so it survives reboots.
2. **Cron / CI**
   - Run the script daily with `--json` and push results to CloudWatch Logs or a metrics sink.
3. Wire the non-zero exit into alerting (e.g., SNS, PagerDuty) so failures trigger follow-up before the agents degrade.

## Follow-up actions
- Add a CloudWatch metric filter once the scheduler is wired.
- Keep the phrases fresh by rotating examples alongside transcript mining so we cover seasonal edge cases.

## Console checklist
- Amazon Bedrock → Agents → `dAisy` → Versions: confirm version `81` remains the default.
- Under Aliases (`Paul`, `Bianca`, `Gina`): verify routing configuration points at version `81` and that the orchestration prompt matches the persona markdown.
- Action Groups tab: both `daisy_in_action-0k2c0` and `bedrock-time-tools` show `Enabled`.
- After smoke tests and replays pass, delete legacy versions (`79`, `80`) to keep the agent list clean.
