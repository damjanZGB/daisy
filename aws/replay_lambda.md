# Replay Lambda Deployment Notes

This Lambda replays stored chat transcripts against the staging Bedrock agent to spot behavioural regressions. Build it as a separate function from the production action Lambda.

## Environment variables

| Variable | Description |
| --- | --- |
| `TRANSCRIPT_BUCKET` | S3 bucket that stores `prod/<variant>/YYYY/MM/DD/*.json` transcripts (for us: `origin-daisy-bucket`). |
| `TRANSCRIPT_ROOT_PREFIX` | Root prefix that precedes the variant folders (e.g. `prod`). |
| `REPLAY_VARIANTS` | Comma-separated persona list to scan (default `bianca,gina,origin,paul`). |
| `REPLAY_LOOKBACK_DAYS` | How many days back to replay (default `1`, i.e. “yesterday”). |
| `AGENT_ID` | Bedrock agent identifier. |
| `AGENT_ALIAS_ID` | Default alias to replay against when no variant-specific alias is mapped (set to Paul’s alias `R1YRB7NGUP`). |
| `REPLAY_ALIAS_MAP` | Optional comma-separated map `variant:aliasId` (e.g. `bianca:D84...,gina:UY0...,origin:R1...,paul:R1...`). Missing variants fall back to `AGENT_ALIAS_ID`. |
| `AWS_REGION` | Region for S3/Bedrock clients (`us-west-2`). |
| `REPLAY_RESULTS_BUCKET` | Bucket for run summaries; defaults to `TRANSCRIPT_BUCKET`. |
| `REPLAY_RESULTS_PREFIX` | Prefix for persisted run summaries (defaults to `<TRANSCRIPT_ROOT_PREFIX>/replay-results`). |

## Packaging

No third-party dependencies are required beyond the default Lambda runtime (`boto3`). Zip `aws/replay_lambda.py` as `lambda_function.py` or update the handler accordingly.

Example:

```bash
zip replay_lambda.zip aws/replay_lambda.py
aws lambda update-function-code \
  --function-name daisy-replay-lambda \
  --zip-file fileb://replay_lambda.zip
```

Set the handler to `replay_lambda.lambda_handler`.

## Scheduling

- Use EventBridge (cron) or a Step Functions state machine to invoke nightly.
- Pass `{ "targetDate": "YYYY-MM-DD" }` in the event to replay a specific day, otherwise the function replays “yesterday”.
- Results are written to `s3://<REPLAY_RESULTS_BUCKET>/<REPLAY_RESULTS_PREFIX>/<YYYY>/<MM>/<DD>/replay_HHMMSS.json`.

## IAM

Grant the Lambda execution role:

- `s3:GetObject`, `s3:ListBucket` on the transcript bucket/prefix.
- `s3:PutObject` (with `x-amz-server-side-encryption = AES256`) on the results prefix.
- `bedrock:InvokeAgent` for the chosen agent alias.
*** End Patch***
