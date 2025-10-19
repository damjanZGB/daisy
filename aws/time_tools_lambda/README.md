# Bedrock Time Tools Lambda

Standalone Lambda package that exposes two utilities for Bedrock Agents:

- `parse_human_time` → natural-language phrase to **future** ISO date (`YYYY-MM-DD`).
- `normalize_any_date_to_iso` → any date-like string to ISO without biasing to the future.

## Project Layout

```
aws/time_tools_lambda/
├── lambda_function.py    # Lambda entry point
├── time_tools.py         # Parsing helpers
└── requirements.txt      # Third-party deps (dateparser, python-dateutil)
```

## Packaging

Run the helper script from the repo root (requires Python 3.11+):

```bash
python aws/time_tools_lambda/package.py
```

This builds `time_tools_lambda.zip` containing the code plus dependencies inside `dist/`.

## Deploying

1. Create an AWS Lambda function (Python 3.11) named e.g. `bedrock-time-tools`.
2. Upload `dist/time_tools_lambda.zip` as the function code.
3. Set handler to `lambda_function.lambda_handler`.
4. Configure timeout (10s) and memory (256 MB recommended).

Environment variables (optional):

- `DEFAULT_TIMEZONE` – override the timezone fallback (defaults to UTC).

## Register with Bedrock Agent

Add a new Action Group of type **Lambda** pointing to the Lambda ARN. Suggested schema:

- Operation `human_to_future_iso` → `{ "op": "human_to_future_iso", "phrase": "...", "locale": ["en"], "timezone": "Europe/Zagreb" }`
- Operation `normalize_any` → `{ "op": "normalize_any", "text": "...", "locale": ["en"] }`

Both responses include `{"success": true|false, "mode": "...", "iso_date"?: "YYYY-MM-DD", "error"?: "..."}`.
