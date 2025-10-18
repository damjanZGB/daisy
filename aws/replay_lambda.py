# replay_lambda.py
# Lambda function that replays stored chat transcripts against the Bedrock agent and records
# basic pass/fail telemetry. Designed to run on a nightly schedule via Step Functions/EventBridge.

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from dataclasses import dataclass
from typing import Iterable, List, Optional

import boto3

# ---- Environment configuration ----

TRANSCRIPT_BUCKET = os.environ["TRANSCRIPT_BUCKET"]
TRANSCRIPT_ROOT_PREFIX = os.environ.get("TRANSCRIPT_ROOT_PREFIX", "").strip().strip("/")
REPLAY_VARIANTS = [
    v.strip()
    for v in os.environ.get("REPLAY_VARIANTS", "bianca,gina,origin,paul").split(",")
    if v.strip()
]
REPLAY_LOOKBACK_DAYS = int(os.environ.get("REPLAY_LOOKBACK_DAYS", "1"))
AGENT_ID = os.environ["AGENT_ID"]
AGENT_ALIAS_ID = os.environ["AGENT_ALIAS_ID"]
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
RESULTS_BUCKET = os.environ.get("REPLAY_RESULTS_BUCKET", TRANSCRIPT_BUCKET)
DEFAULT_RESULTS_PREFIX = (
    f"{TRANSCRIPT_ROOT_PREFIX}/replay-results".strip("/")
    if TRANSCRIPT_ROOT_PREFIX
    else "replay-results"
)
RESULTS_PREFIX = os.environ.get("REPLAY_RESULTS_PREFIX", DEFAULT_RESULTS_PREFIX).strip("/")

S3 = boto3.client("s3", region_name=AWS_REGION)
BEDROCK = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)


def parse_alias_map(raw_map: str) -> dict:
    mapping = {}
    if not raw_map:
        return mapping
    for entry in raw_map.split(","):
        if ":" not in entry:
            continue
        variant, alias = entry.split(":", 1)
        variant = variant.strip()
        alias = alias.strip()
        if variant and alias:
            mapping[variant] = alias
    return mapping


ALIAS_MAP = parse_alias_map(os.environ.get("REPLAY_ALIAS_MAP", ""))


@dataclass
class Transcript:
    key: str
    variant: str
    started_at: str
    session_id: str
    messages: List[dict]

    @property
    def friendly_id(self) -> str:
        return f"{self.variant}:{self.session_id}"


def lambda_handler(event, context):
    target_date = resolve_target_date(event)
    transcripts = collect_transcripts(target_date)
    results = []
    for transcript in transcripts:
        outcome = replay_transcript(transcript)
        results.append(outcome)

    summary = {
        "runAt": dt.datetime.utcnow().isoformat() + "Z",
        "targetDate": target_date.isoformat(),
        "totalTranscripts": len(transcripts),
        "results": results,
    }
    persist_summary(target_date, summary)
    print(json.dumps({"message": "Replay completed", **summary}, indent=2))
    return summary


def resolve_target_date(event: Optional[dict]) -> dt.date:
    if event and "targetDate" in event:
        return dt.date.fromisoformat(event["targetDate"])
    return dt.date.today() - dt.timedelta(days=REPLAY_LOOKBACK_DAYS)


def collect_transcripts(target_date: dt.date) -> List[Transcript]:
    date_prefix = target_date.strftime("%Y/%m/%d")
    transcripts: List[Transcript] = []
    for variant in REPLAY_VARIANTS:
        prefix_parts = [p for p in [TRANSCRIPT_ROOT_PREFIX, variant, date_prefix] if p]
        prefix = "/".join(prefix_parts) + "/"
        paginator = S3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=TRANSCRIPT_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                body = S3.get_object(Bucket=TRANSCRIPT_BUCKET, Key=obj["Key"])["Body"].read()
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    print(f"Skipping invalid JSON transcript: s3://{TRANSCRIPT_BUCKET}/{obj['Key']}")
                    continue
                messages = payload.get("messages") or []
                if not isinstance(messages, list):
                    continue
                transcripts.append(
                    Transcript(
                        key=obj["Key"],
                        variant=variant,
                        started_at=payload.get("startedAt") or "",
                        session_id=payload.get("sessionId") or uuid.uuid4().hex,
                        messages=messages,
                    )
                )
    return transcripts


def replay_transcript(transcript: Transcript) -> dict:
    session_id = f"replay-{uuid.uuid4().hex}"
    user_messages = [m for m in transcript.messages if m.get("role") == "user" and m.get("text")]
    final_response: Optional[str] = None
    steps = []
    alias_id = resolve_alias_for_variant(transcript.variant)

    for idx, message in enumerate(user_messages):
        response_text = invoke_agent(session_id, message["text"], alias_id=alias_id)
        final_response = response_text
        steps.append(
            {
                "turn": idx + 1,
                "input": message["text"],
                "output": response_text,
                "success": bool(response_text.strip()),
            }
        )

    success = all(step["success"] for step in steps) and bool(final_response and final_response.strip())
    return {
        "transcriptKey": transcript.key,
        "variant": transcript.variant,
        "session": transcript.friendly_id,
        "success": success,
        "aliasId": alias_id,
        "steps": steps,
    }


def resolve_alias_for_variant(variant: str) -> str:
    return ALIAS_MAP.get(variant) or AGENT_ALIAS_ID


def invoke_agent(session_id: str, text: str, *, alias_id: str) -> str:
    response = BEDROCK.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=alias_id,
        sessionId=session_id,
        inputText=text,
    )
    stream = response.get("completion")
    chunks: List[str] = []
    if stream:
        for event in stream:
            chunk = event.get("chunk")
            if chunk and "bytes" in chunk:
                chunks.append(chunk["bytes"].decode("utf-8", errors="ignore"))
    return "".join(chunks).strip()


def persist_summary(target_date: dt.date, summary: dict) -> None:
    date_prefix = target_date.strftime("%Y/%m/%d")
    key = f"{RESULTS_PREFIX}/{date_prefix}/replay_{dt.datetime.utcnow().strftime('%H%M%S')}.json"
    S3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=key,
        Body=json.dumps(summary, indent=2).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
