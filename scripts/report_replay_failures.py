#!/usr/bin/env python3
"""
Generate a markdown summary of replay failures for a given date.
The script scans the replay-results prefix in S3 (or a local JSON file) and
outputs the transcripts/steps that need instruction attention.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError


DEFAULT_BUCKET = os.environ.get("REPLAY_RESULTS_BUCKET") or os.environ.get("TRANSCRIPT_BUCKET")
DEFAULT_PREFIX = os.environ.get("REPLAY_RESULTS_PREFIX") or "replay-results"
DEFAULT_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


@dataclass
class ReplayFailure:
    variant: str
    transcript_key: str
    session: str
    step_turn: int
    step_input: str
    step_output: str
    notes: List[str] = field(default_factory=list)


def iter_replay_objects(
    *, bucket: str, prefix: str, target_date: dt.date, s3_client
) -> Iterable[dict]:
    date_prefix = target_date.strftime("%Y/%m/%d")
    full_prefix = "/".join(part.strip("/") for part in (prefix, date_prefix))
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{full_prefix}/"):
        for obj in page.get("Contents", []):
            yield obj


def load_summary(bucket: str, key: str, *, s3_client) -> Optional[dict]:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        print(f"# Warning: unable to fetch s3://{bucket}/{key}: {exc}", file=sys.stderr)
        return None
    body = response["Body"].read()
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        print(f"# Warning: invalid JSON in {key}: {exc}", file=sys.stderr)
        return None


def parse_run_timestamp(summary: dict) -> Optional[dt.datetime]:
    raw = summary.get("runAt")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_failures(summary: dict) -> Dict[str, Tuple[Optional[dt.datetime], List[ReplayFailure]]]:
    run_at = parse_run_timestamp(summary)
    failures: Dict[str, Tuple[Optional[dt.datetime], List[ReplayFailure]]] = {}
    for result in summary.get("results", []):
        variant = result.get("variant", "unknown")
        transcript_key = result.get("transcriptKey", "unknown")
        session = result.get("session", "")
        if result.get("success"):
            failures[transcript_key] = (run_at, [])
            continue
        step_failures: List[ReplayFailure] = []
        for step in result.get("steps", []):
            if step.get("success"):
                continue
            notes = []
            status = step.get("statusCode")
            if status and status not in (200, "200"):
                notes.append(f"HTTP {status}")
            if not step.get("output", "").strip():
                notes.append("Empty output")
            transport = step.get("transport")
            if transport:
                notes.append(f"transport={transport}")
            step_failures.append(
                ReplayFailure(
                    variant=variant,
                    transcript_key=transcript_key,
                    session=session,
                    step_turn=step.get("turn", -1),
                    step_input=step.get("input", ""),
                    step_output=step.get("output", ""),
                    notes=notes,
                )
            )
        failures[transcript_key] = (run_at, step_failures)
    return failures


def print_markdown_report(failures: List[ReplayFailure], *, target_date: dt.date) -> None:
    header = f"# Replay Failures - {target_date.isoformat()}"
    print(header)
    print()
    if not failures:
        print("No failures recorded.")
        return
    print("| Variant | Turn | Session | Transcript | Notes |")
    print("|---------|------|---------|------------|-------|")
    for failure in failures:
        notes = "; ".join(failure.notes) if failure.notes else ""
        print(
            f"| {failure.variant} | {failure.step_turn} | {failure.session} | "
            f"`{failure.transcript_key}` | {notes} |"
        )
    print()
    print("## Details")
    for failure in failures:
        print(f"### {failure.variant} · turn {failure.step_turn}")
        print(f"- Transcript: `{failure.transcript_key}`")
        if failure.session:
            print(f"- Session: `{failure.session}`")
        if failure.notes:
            print(f"- Notes: {', '.join(failure.notes)}")
        print("- Input:")
        print(f"  ```\n  {failure.step_input}\n  ```")
        print("- Output:")
        output = failure.step_output or "(empty response)"
        print(f"  ```\n  {output}\n  ```")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarise replay failures from S3 results.")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="Replay results bucket.")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="Replay results prefix.")
    parser.add_argument("--date", required=True, help="Target date (YYYY-MM-DD).")
    parser.add_argument(
        "--local-file",
        help="Optional local summary JSON file (skip S3 lookup).",
    )
    args = parser.parse_args()

    try:
        target_date = dt.date.fromisoformat(args.date)
    except ValueError:
        parser.error("Invalid --date value. Use YYYY-MM-DD.")

    if args.local_file:
        with open(args.local_file, "r", encoding="utf-8") as handle:
            summary = json.load(handle)
        failures = extract_failures(summary)
        print_markdown_report(failures, target_date=target_date)
        return

    if not args.bucket or not args.prefix:
        parser.error("Bucket/prefix not provided and environment defaults missing.")

    s3_client = boto3.client("s3", region_name=DEFAULT_REGION)
    aggregate: Dict[str, Tuple[Optional[dt.datetime], List[ReplayFailure]]] = {}
    for obj in iter_replay_objects(
        bucket=args.bucket, prefix=args.prefix, target_date=target_date, s3_client=s3_client
    ):
        summary = load_summary(args.bucket, obj["Key"], s3_client=s3_client)
        if not summary:
            continue
        extracted = extract_failures(summary)
        for transcript, payload in extracted.items():
            run_at, step_failures = payload
            existing = aggregate.get(transcript)
            if not existing:
                aggregate[transcript] = (run_at, step_failures)
                continue
            existing_run_at, _ = existing
            if (
                existing_run_at is None
                or (run_at and existing_run_at and run_at > existing_run_at)
            ):
                aggregate[transcript] = (run_at, step_failures)
    failures = [
        failure
        for _, (_, step_failures) in aggregate.items()
        for failure in step_failures
        if step_failures
    ]
    print_markdown_report(failures, target_date=target_date)


if __name__ == "__main__":
    main()
