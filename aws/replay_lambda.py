# replay_lambda.py
# Lambda function that replays stored chat transcripts against the Bedrock agent and records
# basic pass/fail telemetry. Designed to run on a nightly schedule via Step Functions/EventBridge.

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

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
METRIC_NAMESPACE = os.environ.get("REPLAY_METRIC_NAMESPACE", "daisy/replay").strip() or "daisy/replay"
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "").strip()
ACTION_LAMBDA_NAME = os.environ.get("ACTION_LAMBDA_NAME", "").strip()
ACTION_GROUP_NAME = os.environ.get("ACTION_GROUP_NAME", "daisy_in_action").strip() or "daisy_in_action"
ACTION_API_PATH = os.environ.get("ACTION_API_PATH", "/tools/amadeus/search").strip() or "/tools/amadeus/search"

S3 = boto3.client("s3", region_name=AWS_REGION)
BEDROCK = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)
CLOUDWATCH = boto3.client("cloudwatch", region_name=AWS_REGION)
SNS = boto3.client("sns", region_name=AWS_REGION) if SNS_TOPIC_ARN else None
LAMBDA = boto3.client("lambda", region_name=AWS_REGION) if ACTION_LAMBDA_NAME else None


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
    variant_stats = defaultdict(lambda: {"total": 0, "failed": 0})
    for transcript in transcripts:
        outcome = replay_transcript(transcript)
        results.append(outcome)
        stats = variant_stats[outcome.get("variant", transcript.variant)]
        stats["total"] += 1
        if not outcome.get("success", False):
            stats["failed"] += 1

    variant_summary = {k: {"total": v["total"], "failed": v["failed"]} for k, v in variant_stats.items()}
    if variant_summary:
        publish_metrics(variant_summary)
        maybe_publish_failure_alert(target_date, variant_summary, results)

    summary = {
        "runAt": dt.datetime.utcnow().isoformat() + "Z",
        "targetDate": target_date.isoformat(),
        "totalTranscripts": len(transcripts),
        "results": results,
        "variantStats": variant_summary,
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

    structured = extract_structured_itinerary(transcript.messages)
    if structured and LAMBDA:
        action_result = invoke_action_lambda(
            session_id=session_id,
            itinerary=structured,
            variant=transcript.variant,
        )
        final_response = action_result.get("summary", "")
        steps.append(
            {
                "turn": 1,
                "input": structured.get("sourceText", "structured itinerary"),
                "output": final_response,
                "success": action_result.get("success", False),
                "transport": "lambda",
                "statusCode": action_result.get("status"),
                "itinerary": {
                    k: v
                    for k, v in structured.items()
                    if k not in {"sourceText", "extraction"} and v not in (None, "")
                },
                "extraction": structured.get("extraction", "unknown"),
            }
        )
        success = action_result.get("success", False)
        return {
            "transcriptKey": transcript.key,
            "variant": transcript.variant,
            "session": transcript.friendly_id,
            "success": success,
            "aliasId": alias_id,
            "steps": steps,
        }
    if structured and not LAMBDA:
        print("Structured itinerary detected but ACTION_LAMBDA_NAME is not configured; falling back to agent replay.")

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


def extract_structured_itinerary(messages: List[dict]) -> Optional[dict]:
    """Attempt to recover a full itinerary from transcript messages."""
    if not messages:
        return None

    for message in messages:
        text = message.get("text")
        if not isinstance(text, str) or "{" not in text:
            continue
        for candidate in iter_json_blocks(text):
            normalized = normalize_itinerary_payload(candidate)
            if normalized:
                normalized["sourceText"] = candidate.strip()
                normalized["extraction"] = "json"
                return normalized

    inferred = infer_itinerary_from_conversation(messages)
    if inferred:
        inferred["extraction"] = "heuristic"
        return inferred
    return None


def normalize_itinerary_payload(candidate: str) -> Optional[dict]:
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return normalize_itinerary_dict(parsed)


def normalize_itinerary_dict(data: dict) -> Optional[dict]:
    if not isinstance(data, dict):
        return None
    extracted = _extract_itinerary_fields(data)
    normalized = apply_itinerary_defaults(extracted)
    if normalized:
        return normalized
    for value in data.values():
        if isinstance(value, dict):
            nested = normalize_itinerary_dict(value)
            if nested:
                return nested
    return None


def apply_itinerary_defaults(values: dict) -> Optional[dict]:
    if not values:
        return None
    required = ("originLocationCode", "destinationLocationCode", "departureDate")
    if any(values.get(field) in (None, "", []) for field in required):
        return None
    result = {k: v for k, v in values.items() if v not in (None, "", [])}

    for field in ("originLocationCode", "destinationLocationCode"):
        if field in result:
            result[field] = str(result[field]).strip().upper()

    if "departureDate" in result:
        normalized_departure = normalize_date(str(result["departureDate"]))
        if not normalized_departure:
            return None
        result["departureDate"] = normalized_departure

    if "returnDate" in result:
        normalized_return = normalize_date(str(result["returnDate"]))
        if normalized_return:
            result["returnDate"] = normalized_return
        else:
            result.pop("returnDate", None)

    if "adults" in result:
        result["adults"] = normalize_int(result["adults"], minimum=1)
    else:
        result["adults"] = "1"

    if "max" in result:
        result["max"] = normalize_int(result["max"], minimum=1)

    for field in ("travelClass", "currencyCode"):
        if field in result:
            result[field] = str(result[field]).strip().upper()

    result.setdefault("travelClass", "ECONOMY")
    result.setdefault("currencyCode", "EUR")
    result.setdefault("max", "10")

    for field in ("nonstop", "lhGroupOnly"):
        if field in result:
            result[field] = normalize_bool(result[field])

    return result


def infer_itinerary_from_conversation(messages: List[dict]) -> Optional[dict]:
    departure_date = find_latest_date(messages)
    if not departure_date:
        return None
    codes = scan_iata_codes(messages)
    if len(codes) < 2:
        return None
    origin = codes[0]
    destination = codes[-1]
    if origin == destination:
        for code in reversed(codes):
            if code != origin:
                destination = code
                break
        else:
            return None
    candidate = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": departure_date,
    }
    normalized = apply_itinerary_defaults(candidate)
    if not normalized:
        return None
    normalized["sourceText"] = f"{origin}->{destination} {departure_date}"
    return normalized


def invoke_action_lambda(*, session_id: str, itinerary: dict, variant: str) -> dict:
    if not LAMBDA or not ACTION_LAMBDA_NAME:
        return {"success": False, "summary": "Lambda client unavailable", "status": None}
    event = build_action_event(session_id=session_id, itinerary=itinerary, variant=variant)
    try:
        response = LAMBDA.invoke(
            FunctionName=ACTION_LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(event).encode("utf-8"),
        )
    except Exception as exc:  # pragma: no cover - network failure
        return {
            "success": False,
            "status": None,
            "summary": f"Lambda invoke failed: {exc}",
            "actionEvent": event,
            "offers": [],
        }
    payload_stream = response.get("Payload")
    raw_bytes = payload_stream.read() if payload_stream else b""
    raw_text = raw_bytes.decode("utf-8", errors="ignore") if raw_bytes else ""
    parsed = {}
    status = None
    summary_text = raw_text
    offers: List[dict] = []

    if raw_text:
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            parsed = {}
        else:
            response_block = parsed.get("response") or {}
            status = response_block.get("httpStatusCode") or response_block.get("statusCode")
            body = (
                (response_block.get("responseBody") or {})
                .get("application/json", {})
                .get("body")
            )
            if body:
                try:
                    body_json = json.loads(body)
                except json.JSONDecodeError:
                    summary_text = body
                else:
                    summary_text = json.dumps(body_json, indent=2)
                    if isinstance(body_json, dict):
                        offers_val = body_json.get("offers")
                        if isinstance(offers_val, list):
                            offers = offers_val

    success = status == 200 and bool(offers)
    return {
        "success": success,
        "status": status,
        "summary": truncate_text(summary_text),
        "raw": raw_text,
        "actionEvent": event,
        "payload": parsed,
        "offers": offers,
    }


def build_action_event(*, session_id: str, itinerary: dict, variant: str) -> dict:
    properties: List[dict] = []

    def add_property(name: str, value: Optional[str]) -> None:
        if value is None:
            return
        text = str(value).strip()
        if not text:
            return
        properties.append({"name": name, "value": text})

    add_property("originLocationCode", itinerary.get("originLocationCode"))
    add_property("destinationLocationCode", itinerary.get("destinationLocationCode"))
    add_property("departureDate", itinerary.get("departureDate"))
    add_property("returnDate", itinerary.get("returnDate"))
    add_property("adults", itinerary.get("adults"))
    add_property("travelClass", itinerary.get("travelClass"))
    add_property("currencyCode", itinerary.get("currencyCode"))
    add_property("max", itinerary.get("max"))
    add_property("nonstop", itinerary.get("nonstop"))
    add_property("lhGroupOnly", itinerary.get("lhGroupOnly"))

    event = {
        "messageVersion": "1.0",
        "actionGroup": ACTION_GROUP_NAME,
        "apiPath": ACTION_API_PATH,
        "httpMethod": "POST",
        "sessionId": session_id,
        "agent": {"name": variant or "replay"},
        "sessionAttributes": {},
        "promptSessionAttributes": {},
        "parameters": [],
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": properties,
                }
            }
        },
    }
    return event


def iter_json_blocks(text: str) -> Iterable[str]:
    depth = 0
    start = None
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start : index + 1]
                    start = None


FIELD_ALIASES: List[Tuple[str, Tuple[str, ...]]] = [
    ("originLocationCode", ("originLocationCode", "origin", "origin_code", "originCode")),
    (
        "destinationLocationCode",
        ("destinationLocationCode", "destination", "destination_code", "destinationCode"),
    ),
    ("departureDate", ("departureDate", "departDate", "outboundDate")),
    ("returnDate", ("returnDate", "returningDate", "arrivalDate")),
    ("adults", ("adults", "adultCount", "numAdults")),
    ("travelClass", ("travelClass", "cabin", "class", "fareClass")),
    ("currencyCode", ("currencyCode", "currency", "currency_code")),
    ("max", ("max", "limit", "maxResults")),
    ("nonstop", ("nonstop", "directOnly")),
    ("lhGroupOnly", ("lhGroupOnly", "lufthansaGroupOnly", "lhOnly", "groupOnly")),
]


def _extract_itinerary_fields(data: dict) -> dict:
    fields = {}
    for canonical, aliases in FIELD_ALIASES:
        value = first_non_empty(data, aliases)
        if value is not None:
            fields[canonical] = value
    return fields


def first_non_empty(data: dict, keys: Tuple[str, ...]) -> Optional[str]:
    for key in keys:
        if key not in data:
            continue
        value = data[key]
        if value in (None, "", []):
            continue
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not value.is_integer():
                return str(value)
            return str(int(value))
        return str(value)
    return None


DATE_PATTERN = re.compile(r"\b\d{4}[-/.\s]\d{1,2}[-/.\s]\d{1,2}\b")
IATA_PAREN_PATTERN = re.compile(r"\(([A-Z]{3})\)")
IATA_WORD_PATTERN = re.compile(r"\b([A-Z]{3})\b")


def find_latest_date(messages: List[dict]) -> Optional[str]:
    latest: Optional[str] = None
    for message in messages:
        if message.get("role") != "user":
            continue
        text = message.get("text")
        if not isinstance(text, str):
            continue
        for match in DATE_PATTERN.finditer(text):
            candidate = normalize_date(match.group())
            if candidate:
                latest = candidate
    return latest


def normalize_date(raw: str) -> Optional[str]:
    if not raw:
        return None
    cleaned = raw.strip().replace("/", "-").replace(".", "-").replace(" ", "-")
    parts = [p for p in cleaned.split("-") if p]
    if len(parts) < 3:
        return None
    try:
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        return f"{year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


def normalize_int(value: str, *, minimum: int = 1) -> str:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        number = minimum
    if minimum is not None:
        number = max(minimum, number)
    return str(number)


def normalize_bool(value: str) -> str:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "t", "1", "yes", "y"}:
            return "true"
        if lowered in {"false", "f", "0", "no", "n"}:
            return "false"
    return "true" if value else "false"


def scan_iata_codes(messages: List[dict]) -> List[str]:
    codes: List[str] = []
    for message in messages:
        text = message.get("text")
        if not isinstance(text, str):
            continue
        codes.extend(match.group(1) for match in IATA_PAREN_PATTERN.finditer(text))
    if len(codes) >= 2:
        return codes

    for message in messages:
        text = message.get("text")
        if not isinstance(text, str):
            continue
        for match in IATA_WORD_PATTERN.finditer(text):
            candidate = match.group(1)
            if candidate.isalpha() and candidate.isupper():
                codes.append(candidate)
        if len(codes) >= 2:
            break
    return codes


def truncate_text(text: str, limit: int = 2000) -> str:
    if not text or len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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


def publish_metrics(stats: dict) -> None:
    if not stats:
        return
    metric_data = []
    timestamp = dt.datetime.utcnow()
    for variant, values in stats.items():
        total = values.get("total", 0)
        failed = values.get("failed", 0)
        metric_data.append(
            {
                "MetricName": "ReplayTotal",
                "Dimensions": [{"Name": "Variant", "Value": variant}],
                "Timestamp": timestamp,
                "Value": total,
                "Unit": "Count",
            }
        )
        metric_data.append(
            {
                "MetricName": "ReplayFailed",
                "Dimensions": [{"Name": "Variant", "Value": variant}],
                "Timestamp": timestamp,
                "Value": failed,
                "Unit": "Count",
            }
        )
    for chunk in chunked(metric_data, 20):
        CLOUDWATCH.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=chunk)


def maybe_publish_failure_alert(target_date: dt.date, stats: dict, results: List[dict]) -> None:
    if not SNS or not SNS_TOPIC_ARN:
        return
    failed_total = sum(v.get("failed", 0) for v in stats.values())
    if failed_total == 0:
        return
    failed_results = [
        {
            "transcriptKey": r.get("transcriptKey"),
            "variant": r.get("variant"),
            "session": r.get("session"),
            "aliasId": r.get("aliasId"),
        }
        for r in results
        if not r.get("success")
    ]
    subject = f"dAisy replay failures {target_date.isoformat()} ({failed_total})"
    payload = {
        "targetDate": target_date.isoformat(),
        "failures": failed_total,
        "variantStats": stats,
        "failedTranscripts": failed_results,
    }
    SNS.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject[:100],
        Message=json.dumps(payload, indent=2),
    )


def chunked(seq: List[dict], size: int) -> Iterable[List[dict]]:
    for idx in range(0, len(seq), size):
        yield seq[idx : idx + size]
