#!/usr/bin/env python3
"""
Lightweight smoke tests for the TimePhraseParser Lambda (`bedrock-time-tools`).

Intended for daily automation (cron, Windows Task Scheduler, etc.).
Checks both supported operations:
  * `human_to_future_iso` with the phrase "next Saturday"
  * `normalize_any` with the literal date "1 Nov 2025"
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from zoneinfo import ZoneInfo


def resolve_region(explicit: Optional[str]) -> str:
    return (
        explicit
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run basic health checks against the TimePhraseParser Lambda."
    )
    parser.add_argument(
        "--function",
        "-f",
        default="bedrock-time-tools",
        help="Lambda function name or ARN.",
    )
    parser.add_argument(
        "--region",
        "-r",
        default=None,
        help="AWS region (defaults to env `AWS_REGION`/`AWS_DEFAULT_REGION` or us-west-2).",
    )
    parser.add_argument(
        "--timezone",
        "-t",
        default="Europe/Zurich",
        help="IANA timezone used to evaluate relative phrases.",
    )
    parser.add_argument(
        "--qualifier",
        "-q",
        default=None,
        help="Optional Lambda qualifier (version or alias).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable output.",
    )
    return parser.parse_args()


def compute_next_weekday(start_date: dt.date, target_weekday: int) -> dt.date:
    delta = (target_weekday - start_date.weekday()) % 7
    if delta == 0:
        delta = 7
    return start_date + dt.timedelta(days=delta)


def expected_dates(tz_name: str) -> Dict[str, str]:
    tz = ZoneInfo(tz_name)
    today = dt.datetime.now(tz).date()
    next_saturday = compute_next_weekday(today, 5)  # Monday=0 ... Saturday=5
    return {
        "next_saturday": next_saturday.isoformat(),
        "normalize_literal": "2025-11-01",
    }


@dataclass
class TestCase:
    name: str
    payload: Dict[str, object]
    validator: Callable[[Dict[str, object]], Tuple[bool, str]]
    description: str


def make_test_cases(timezone: str, expectations: Dict[str, str]) -> List[TestCase]:
    def expect_iso(expected_iso: str) -> Callable[[Dict[str, object]], Tuple[bool, str]]:
        def _check(response: Dict[str, object]) -> Tuple[bool, str]:
            if not response.get("success"):
                return False, f"success flag false, payload={response}"
            iso_date = response.get("iso_date")
            if iso_date != expected_iso:
                return False, f"expected {expected_iso}, got {iso_date}"
            return True, f"ok ({iso_date})"

        return _check

    def expect_success(response: Dict[str, object]) -> Tuple[bool, str]:
        if not response.get("success"):
            return False, f"success flag false, payload={response}"
        if not response.get("iso_date"):
            return False, f"missing iso_date, payload={response}"
        try:
            converted = dt.date.fromisoformat(str(response["iso_date"]))
        except ValueError:
            return False, f"iso_date not ISO, payload={response}"
        expected = dt.date.fromisoformat(expectations["next_saturday"])
        if converted != expected:
            return (
                False,
                f"computed {converted.isoformat()} but expected {expected.isoformat()}",
            )
        return True, f"ok ({converted.isoformat()})"

    return [
        TestCase(
            name="human_to_future_iso_next_saturday",
            description='Phrase "next Saturday" resolves in the given timezone.',
            payload={
                "op": "human_to_future_iso",
                "phrase": "next Saturday",
                "timezone": timezone,
                "locale": ["en"],
            },
            validator=expect_success,
        ),
        TestCase(
            name="normalize_any_literal_date",
            description='Literal "1 Nov 2025" normalizes to 2025-11-01.',
            payload={
                "op": "normalize_any",
                "text": "1 Nov 2025",
                "timezone": timezone,
                "locale": ["en"],
            },
            validator=expect_iso(expectations["normalize_literal"]),
        ),
    ]


def invoke_lambda(
    client,
    function_name: str,
    payload: Dict[str, object],
    qualifier: Optional[str] = None,
) -> Dict[str, object]:
    kwargs = {
        "FunctionName": function_name,
        "Payload": json.dumps(payload).encode("utf-8"),
    }
    if qualifier:
        kwargs["Qualifier"] = qualifier
    response = client.invoke(**kwargs)
    status = response.get("StatusCode", 0)
    body = response.get("Payload")
    raw_payload = body.read() if body else b"{}"
    try:
        parsed = json.loads(raw_payload or b"{}")
    except json.JSONDecodeError:
        parsed = {"success": False, "error": "invalid-json-response", "raw": raw_payload.decode("utf-8", "replace")}
    if response.get("FunctionError"):
        parsed.setdefault("success", False)
        parsed["functionError"] = response["FunctionError"]
    parsed["_http_status"] = status
    return parsed


def run_case(
    client,
    case: TestCase,
    function_name: str,
    qualifier: Optional[str] = None,
) -> Tuple[bool, str, Dict[str, object]]:
    try:
        response = invoke_lambda(client, function_name, case.payload, qualifier)
    except (ClientError, BotoCoreError) as exc:
        return False, f"invoke failed: {exc}", {}
    ok, detail = case.validator(response)
    if not ok:
        detail = f"{detail}; lambda_response={response}"
    return ok, detail, response


def format_human(results: List[Tuple[TestCase, bool, str]]) -> str:
    lines = []
    for test_case, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        lines.append(f"[{status}] {test_case.name}: {test_case.description} -> {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    lines.append(f"-- {passed}/{total} checks passed --")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    region = resolve_region(args.region)
    expectations = expected_dates(args.timezone)
    client = boto3.client("lambda", region_name=region)
    test_cases = make_test_cases(args.timezone, expectations)

    results: List[Tuple[TestCase, bool, str]] = []
    for case in test_cases:
        ok, detail, _response = run_case(
            client,
            case,
            function_name=args.function,
            qualifier=args.qualifier,
        )
        results.append((case, ok, detail))

    if args.json:
        serializable = []
        for case, ok, detail in results:
            serializable.append(
                {
                    "name": case.name,
                    "description": case.description,
                    "ok": ok,
                    "detail": detail,
                }
            )
        output = {
            "function": args.function,
            "region": region,
            "timezone": args.timezone,
            "total": len(results),
            "passed": sum(1 for _, ok, _ in results if ok),
            "results": serializable,
        }
        print(json.dumps(output, indent=2))
    else:
        print(format_human(results))

    failed = any(not ok for _, ok, _ in results)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

