# aws/lambda_function.py — Bedrock Return-Control bridge for Daisy microservices
import base64
import binascii
import calendar
import re
import json
import os
import os.path
import urllib.error
import urllib.parse
import urllib.request

import boto3

AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
AGENT_ID = os.getenv("AGENT_ID") or os.getenv("SUPERVISOR_AGENT_ID")
AGENT_ALIAS_ID = os.getenv("AGENT_ALIAS_ID") or os.getenv("SUPERVISOR_AGENT_ALIAS_ID")
PROXY_BASE_URL = (os.getenv("PROXY_BASE_URL") or "https://origin-daisy.onrender.com").rstrip("/")
GOOGLE_BASE_URL = (os.getenv("GOOGLE_BASE_URL") or "https://google-api-daisy.onrender.com").rstrip("/")
RC_MAX_HOPS = int(os.getenv("RETURN_CONTROL_MAX_HOPS") or "6")
HTTP_TIMEOUT = int(os.getenv("PROXY_TIMEOUT_SECONDS") or "60")

def _as_set(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}

AGENT_ID_ALLOWLIST = _as_set(os.getenv("AGENT_ID_ALLOWLIST"))
AGENT_ALIAS_ALLOWLIST = _as_set(os.getenv("AGENT_ALIAS_ALLOWLIST"))
if AGENT_ID:
    AGENT_ID_ALLOWLIST.add(AGENT_ID)
if AGENT_ALIAS_ID:
    AGENT_ALIAS_ALLOWLIST.add(AGENT_ALIAS_ID)

IATA_PATHS = [
    os.path.join(os.getcwd(), "backend", "iata.json"),
    os.path.join(os.getcwd(), "data", "iata.json"),
    os.path.join(os.getcwd(), "aws", "deploy_action", "data", "iata.json"),
    os.path.join(os.getcwd(), "aws", "deploy_action", "iata.json"),
    os.path.join(os.getcwd(), "iata.json"),
]
IATA_DATA = None

bedrock = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)


def _normalize_base64(value: str) -> str:
    stripped = "".join(value.split())
    if not stripped:
        return ""
    padding = len(stripped) % 4
    if padding:
        stripped += "=" * (4 - padding)
    return stripped


def _decode_chunk_payload(payload) -> str:
    if payload is None:
        return ""
    try:
        if isinstance(payload, bytes):
            try:
                return payload.decode("utf-8", "ignore")
            except Exception:
                return base64.b64encode(payload).decode("ascii", "ignore")
        if isinstance(payload, str):
            normalized = _normalize_base64(payload)
            try:
                return base64.b64decode(normalized).decode("utf-8", "ignore")
            except (binascii.Error, ValueError):
                return payload
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return str(payload)


def _load_iata_data():
    global IATA_DATA
    if IATA_DATA is not None:
        return IATA_DATA
    for candidate in IATA_PATHS:
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                IATA_DATA = json.load(handle)
                return IATA_DATA
        except Exception:
            continue
    IATA_DATA = {}
    return IATA_DATA


def _country_for_iata(code: str | None) -> str | None:
    if not code:
        return None
    data = _load_iata_data()
    entry = data.get(str(code).upper())
    if isinstance(entry, dict):
        country = entry.get("country")
        if isinstance(country, str) and len(country) == 2:
            return country.upper()
    return None


def _is_valid_gl(value: str | None) -> bool:
    return isinstance(value, str) and len(value) == 2 and value.isalpha()


def _normalize_time_period(value) -> str:
    if not value:
        return "one_week_trip_in_the_next_six_months"
    text = str(value).strip()
    lower = text.lower()
    year_match = None
    for token in lower.split():
        if token.isdigit() and len(token) == 4:
            year_match = int(token)
            break
    if "summer" in lower and year_match:
        return f"{year_match}-06-01..{year_match}-08-31"
    if "winter" in lower and year_match:
        return f"{year_match}-12-01..{year_match+1}-02-28"
    if calendar_re := re.match(r"^(\d{4})-(\d{2})$", text):
        year = int(calendar_re.group(1))
        month = int(calendar_re.group(2))
        last_day = calendar.monthrange(year, month)[1]
        return f"{year:04d}-{month:02d}-01..{year:04d}-{month:02d}-{last_day:02d}"
    if calendar_re := re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text):
        y, m, d = calendar_re.groups()
        return f"{y}-{m}-{d}..{y}-{m}-{d}"
    if ".." in text or text.startswith("custom_dates:"):
        return text
    return "one_week_trip_in_the_next_six_months"


def _event_to_dict(event) -> dict:
    if isinstance(event, dict):
        return event
    if hasattr(event, "to_dict"):
        try:
            return event.to_dict()
        except Exception:
            pass
    plain = {}
    for attr in (
        "chunk",
        "output_text",
        "outputText",
        "content_block",
        "contentBlock",
        "return_control",
        "returnControl",
    ):
        if hasattr(event, attr):
            plain[attr] = getattr(event, attr)
    return plain


def _resolve_agent_context(event: dict | None, session_state: dict | None = None) -> tuple[str, str]:
    """Ensure the Bedrock agent identifiers are available from env or event payload."""
    global AGENT_ID, AGENT_ALIAS_ID
    candidate_agent_id = None
    candidate_alias_id = None

    def extract(container):
        nonlocal candidate_agent_id, candidate_alias_id
        if not isinstance(container, dict):
            return
        if not candidate_agent_id:
            candidate_agent_id = (
                container.get("agentId")
                or container.get("id")
                or container.get("agent_id")
                or container.get("agent-id")
            )
        if not candidate_alias_id:
            candidate_alias_id = (
                container.get("agentAliasId")
                or container.get("aliasId")
                or container.get("agent_alias_id")
                or container.get("agent-alias-id")
                or container.get("alias")
            )
        nested_agent = container.get("agent")
        if isinstance(nested_agent, dict):
            extract(nested_agent)
        nested_session = container.get("sessionState") or container.get("session_state")
        if isinstance(nested_session, dict):
            extract(nested_session)
        nested_attrs = container.get("sessionAttributes") or container.get("session_attributes")
        if isinstance(nested_attrs, dict):
            extract(nested_attrs)
        prompt_attrs = container.get("promptSessionAttributes") or container.get("prompt_session_attributes")
        if isinstance(prompt_attrs, dict):
            extract(prompt_attrs)

    if isinstance(event, dict):
        extract(event)
        headers = event.get("headers")
        if isinstance(headers, dict):
            lowered = {str(k).lower(): v for k, v in headers.items()}
            candidate_agent_id = candidate_agent_id or lowered.get("x-agent-id") or lowered.get("x-bedrock-agent-id")
            candidate_alias_id = candidate_alias_id or lowered.get("x-agent-alias-id") or lowered.get("x-bedrock-agent-alias-id")
        body = event.get("body")
        parsed_body = None
        if isinstance(body, str):
            try:
                parsed_body = json.loads(body)
            except json.JSONDecodeError:
                parsed_body = None
        elif isinstance(body, dict):
            parsed_body = body
        if isinstance(parsed_body, dict):
            extract(parsed_body)

    if isinstance(session_state, dict):
        extract(session_state)

    if not AGENT_ID and candidate_agent_id:
        AGENT_ID = candidate_agent_id
    if not AGENT_ALIAS_ID and candidate_alias_id:
        AGENT_ALIAS_ID = candidate_alias_id

    def ensure_allowed(label: str, value: str | None, allowlist: set[str]) -> str | None:
        if value and allowlist and value not in allowlist:
            try:
                print(
                    f"[lambda] {label} {value} not in allowlist",
                    json.dumps({"allowlist": sorted(allowlist)}),
                )
            except Exception:
                print(f"[lambda] {label} {value} not in allowlist")
        return value

    AGENT_ID = ensure_allowed("agentId", AGENT_ID, AGENT_ID_ALLOWLIST)
    AGENT_ALIAS_ID = ensure_allowed("agentAliasId", AGENT_ALIAS_ID, AGENT_ALIAS_ALLOWLIST)

    if not isinstance(event, dict):
        event = {}
    if not AGENT_ID or not AGENT_ALIAS_ID:
        try:
            print(
                "[lambda] missing agent context",
                json.dumps(
                    {
                        "eventKeys": sorted(event.keys()),
                        "agentShape": agent_blob,
                        "agentId": candidate_agent_id,
                        "agentAliasId": candidate_alias_id,
                    }
                ),
            )
        except Exception:
            print("[lambda] missing agent context (unable to serialize diagnostics)")
        raise ValueError("Missing agent identifiers (AGENT_ID / AGENT_ALIAS_ID). Set them via environment variables or include agent.id and agent.aliasId in the request.")
    return AGENT_ID, AGENT_ALIAS_ID


def _invoke_once(agent_id: str, agent_alias_id: str, session_id: str, text: str | None, session_state: dict | None):
    response = bedrock.invoke_agent(
        agentId=agent_id,
        agentAliasId=agent_alias_id,
        sessionId=session_id,
        inputText=text or "",
        enableTrace=True,
        sessionState=session_state or {},
    )
    text_fragments: list[str] = []
    return_control = None
    completion_stream = getattr(response, "completion", None)
    if completion_stream is None and isinstance(response, dict):
        completion_stream = response.get("completion", [])
    if completion_stream is None:
        completion_stream = []
    for event in completion_stream:
        try:
            event_dict = _event_to_dict(event)
            chunk = None
            chunk = event_dict.get("chunk")
            if chunk is not None:
                chunk_bytes = None
                chunk_text = None
                if isinstance(chunk, dict):
                    chunk_bytes = chunk.get("bytes")
                    chunk_text = chunk.get("text")
                else:
                    chunk_bytes = getattr(chunk, "bytes", None)
                    chunk_text = getattr(chunk, "text", None)
                if chunk_bytes is not None:
                    text_fragments.append(_decode_chunk_payload(chunk_bytes))
                if chunk_text:
                    text_fragments.append(str(chunk_text))

            output_text = None
            output_text = event_dict.get("outputText") or event_dict.get("output_text")
            if output_text is None and hasattr(event, "output_text"):
                output_text = getattr(event, "output_text")
            if output_text is None and hasattr(event, "outputText"):
                output_text = getattr(event, "outputText")
            items = None
            if isinstance(output_text, dict):
                items = output_text.get("items")
            else:
                items = getattr(output_text, "items", None)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        text_value = item.get("text")
                    else:
                        text_value = getattr(item, "text", item)
                    if text_value:
                        text_fragments.append(str(text_value))

            content_block = event_dict.get("contentBlock") or event_dict.get("content_block")
            if content_block is None and hasattr(event, "content_block"):
                content_block = getattr(event, "content_block")
            if content_block is None and hasattr(event, "contentBlock"):
                content_block = getattr(event, "contentBlock")
            if content_block:
                if isinstance(content_block, dict):
                    block_text = content_block.get("text")
                else:
                    block_text = getattr(content_block, "text", None)
                if block_text:
                    text_fragments.append(str(block_text))

            rc_candidate = event_dict.get("returnControl") or event_dict.get("return_control")
            if rc_candidate is None and hasattr(event, "return_control"):
                rc_candidate = getattr(event, "return_control")
            if rc_candidate is None and hasattr(event, "returnControl"):
                rc_candidate = getattr(event, "returnControl")
            if rc_candidate:
                return_control = rc_candidate
        except Exception as err:
            try:
                print("[lambda] chunk_processing_error", json.dumps({"error": str(err), "event": event}))
            except Exception:
                print(f"[lambda] chunk_processing_error {err}")
    aggregated_text = "".join(text_fragments)
    return aggregated_text, return_control, response.get("sessionState")


def _target_bases(path: str) -> list[str]:
    targets: list[str] = []
    if path.startswith("/google/"):
        if GOOGLE_BASE_URL:
            targets.append(GOOGLE_BASE_URL)
        elif PROXY_BASE_URL:
            targets.append(PROXY_BASE_URL)
    else:
        if PROXY_BASE_URL:
            targets.append(PROXY_BASE_URL)
    return targets or [PROXY_BASE_URL or GOOGLE_BASE_URL]


def _proxy_url(base: str, path: str) -> str:
    base = (base or "").rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _call_proxy(path: str, method: str, params: dict | None, body: dict | None) -> dict:
    method = (method or "POST").upper()
    data = None
    headers = {}
    if method == "GET":
        query_pairs = []
        param_dict = {}
        if isinstance(params, dict):
            query_pairs = list(params.items())
            param_dict = dict(query_pairs)
        elif isinstance(params, list):
            for item in params:
                if isinstance(item, dict):
                    key = item.get("name") or item.get("key") or item.get("param")
                    value = item.get("value")
                    if key and value is not None:
                        query_pairs.append((key, value))
                        param_dict[key] = value
        elif isinstance(params, tuple):
            query_pairs = list(params)
            param_dict = dict(query_pairs)
        if path.startswith("/google/explore/"):
            inferred_gl = _country_for_iata(param_dict.get("departure_id"))
            if inferred_gl:
                param_dict["gl"] = inferred_gl
            else:
                current_gl = param_dict.get("gl")
                if not _is_valid_gl(current_gl):
                    param_dict["gl"] = "US"
            if not param_dict.get("hl"):
                param_dict["hl"] = "en-US"
            if not param_dict.get("engine"):
                param_dict["engine"] = "google_travel_explore"
            normalized_period = _normalize_time_period(param_dict.get("time_period"))
            param_dict["time_period"] = normalized_period
            interest_value = param_dict.get("interests")
            if interest_value:
                interest_text = str(interest_value).strip().lower()
                interest_aliases = {
                    "beach": "beaches",
                    "beaches": "beaches",
                    "popular": "popular",
                    "outdoor": "outdoors",
                    "outdoors": "outdoors",
                    "museum": "museums",
                    "museums": "museums",
                    "history": "history",
                    "ski": "skiing",
                    "skiing": "skiing",
                }
                canonical_interest = interest_aliases.get(interest_text)
                if canonical_interest:
                    param_dict["interests"] = canonical_interest
            allowed_keys = {
                "engine",
                "departure_id",
                "arrival_id",
                "gl",
                "hl",
                "search_query",
                "time_period",
                "travel_mode",
                "adults",
                "currency",
                "interests",
                "travel_class",
                "max_price",
                "children",
                "infants_in_seat",
                "infants_on_lap",
            }
            for extra_key in list(param_dict.keys()):
                if extra_key not in allowed_keys:
                    param_dict.pop(extra_key, None)
            arrival_value = param_dict.get("arrival_id")
            if arrival_value:
                arrival_str = str(arrival_value).strip()
                valid_arrival = False
                if arrival_str.startswith("/m/"):
                    valid_arrival = True
                elif arrival_str.startswith("[[") and arrival_str.endswith("]]"):
                    valid_arrival = True
                elif len(arrival_str) == 3 and arrival_str.isalpha():
                    valid_arrival = True
                if not valid_arrival:
                    param_dict.pop("arrival_id", None)
                    existing_query = param_dict.get("search_query")
                    param_dict["search_query"] = f"{existing_query} {arrival_str}".strip() if existing_query else arrival_str
            if not param_dict.get("travel_mode"):
                param_dict["travel_mode"] = "flights_only"
            if not param_dict.get("adults"):
                param_dict["adults"] = "1"
            if not param_dict.get("currency"):
                param_dict["currency"] = "EUR"
            query_pairs = list(param_dict.items())
        query_suffix = f"?{urllib.parse.urlencode(query_pairs, doseq=True)}" if query_pairs else ""
        try:
            print(
                "[lambda] tool_query",
                json.dumps(
                    {"path": path, "method": method, "query": query_pairs},
                    default=str,
                ),
            )
        except Exception:
            pass
    else:
        headers["content-type"] = "application/json"
        payload_obj = body
        if isinstance(body, str):
            try:
                payload_obj = json.loads(body)
            except json.JSONDecodeError:
                payload_obj = {"raw": body}
        elif body is None:
            payload_obj = {}
        data = json.dumps(payload_obj or {}).encode("utf-8")
        query_suffix = ""

    targets = _target_bases(path or "")
    last_failure: dict | None = None
    for base in targets:
        url = _proxy_url(base, path or "")
        if method == "GET" and query_suffix:
            url = f"{url}{query_suffix}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
                payload = resp.read().decode("utf-8", "ignore")
                status = resp.getcode()
        except urllib.error.HTTPError as err:
            payload = err.read().decode("utf-8", "ignore")
            status = err.code
            try:
                data = json.loads(payload) if payload else {}
            except Exception:
                data = {"raw": payload}
            last_failure = {"ok": status < 400, "status": status, "data": data}
            continue
        except Exception as exc:
            try:
                print(
                    "[lambda] tool_exception",
                    json.dumps(
                        {"path": path, "url": url, "error": str(exc), "exc_type": type(exc).__name__},
                        default=str,
                    ),
                )
            except Exception:
                pass
            last_failure = {"ok": False, "error": str(exc)}
            continue

        try:
            data = json.loads(payload) if payload else {}
        except Exception:
            data = {"raw": payload}
        result = {"ok": status < 400, "status": status, "data": data}
        if not result["ok"]:
            try:
                print(
                    "[lambda] tool_error_detail",
                    json.dumps({"status": status, "data": data}, default=str),
                )
            except Exception:
                pass
        return result
    if last_failure:
        return last_failure
    return {"ok": False, "status": 520, "data": {"error": "tool_call_failed"}}


def _rc_result(invocation_id: str, inputs: list, results: list) -> list:
    mapped = []
    for idx, result in enumerate(results):
        entry = inputs[idx] if idx < len(inputs) else {}
        if isinstance(entry, dict):
            action_group = entry.get("actionGroup", "unknown")
            api_path = entry.get("apiPath") or entry.get("operation") or entry.get("endpoint") or "unknown"
            http_method = entry.get("httpMethod") or "POST"
        else:
            action_group = "unknown"
            api_path = "unknown"
            http_method = "POST"
        mapped.append(
            {
                "actionGroup": action_group,
                "apiPath": api_path,
                "httpMethod": http_method,
                "result": result,
            }
        )
    return [{"invocationId": invocation_id, "returnControlInvocationResults": mapped}]


def _extract_payload(event: dict) -> tuple[str, str, dict | None]:
    body = {}
    if isinstance(event, dict):
        raw_body = event.get("body")
        if isinstance(raw_body, str):
            try:
                body = json.loads(raw_body or "{}")
            except json.JSONDecodeError:
                body = {}
        elif isinstance(raw_body, dict):
            body = raw_body
        else:
            body = {}
    else:
        body = {}

    query = event.get("queryStringParameters") or {}
    headers = event.get("headers") or {}

    session_id = (
        body.get("sessionId")
        or query.get("sessionId")
        or query.get("sid")
        or headers.get("x-session-id")
        or "lambda-session"
    )
    text = (
        body.get("inputText")
        or body.get("text")
        or query.get("inputText")
        or query.get("text")
        or ""
    )
    session_state = body.get("sessionState")
    return session_id, text, session_state


def lambda_handler(event, context):
    try:
        session_id, initial_text, incoming_state = _extract_payload(event or {})
    except Exception as exc:
        return {
            "statusCode": 400,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"error": f"Invalid request payload: {exc}"}),
        }

    final_text = ""
    state = incoming_state or {}

    direct_invocation_inputs = None
    direct_invocation_id = None
    if isinstance(event, dict):
        direct_invocation_inputs = event.get("invocationInputs")
        direct_invocation_id = event.get("invocationId")
        if not direct_invocation_id:
            direct_invocation_id = event.get("invocation_id") or event.get("invocation-id")

    if isinstance(event, dict) and event.get("actionGroup"):
        path = event.get("apiPath") or event.get("operation") or ""
        method = (event.get("httpMethod") or event.get("method") or "POST").upper()
        params = event.get("parameters") or event.get("query") or {}
        body = event.get("requestBody") or event.get("body") or {}
        proxy_result = _call_proxy(path, method, params, body)
        try:
            print(
                "[lambda] tool_invocation",
                json.dumps(
                    {
                        "actionGroup": event.get("actionGroup"),
                        "apiPath": path,
                        "httpMethod": method,
                        "status": proxy_result.get("status"),
                        "ok": proxy_result.get("ok"),
                    }
                ),
            )
        except Exception:
            pass
        try:
            print(
                "[lambda] tool_payload",
                json.dumps(
                    {
                        "params": params,
                        "body": body,
                    },
                    default=str,
                ),
            )
        except Exception:
            pass
        status_code = proxy_result.get("status", 200 if proxy_result.get("ok") else 502)
        response_body = proxy_result.get("data")
        try:
            encoded_body = json.dumps(response_body)
        except Exception:
            encoded_body = json.dumps({"raw": str(response_body)})
        return {
            "messageVersion": event.get("messageVersion", "1.0"),
            "response": {
                "actionGroup": event.get("actionGroup"),
                "apiPath": path,
                "httpMethod": method,
                "httpStatusCode": status_code,
                "responseBody": {
                    "application/json": {
                        "body": encoded_body
                    }
                },
            },
            "sessionAttributes": event.get("sessionAttributes") or {},
            "promptSessionAttributes": event.get("promptSessionAttributes") or {},
        }

    if isinstance(direct_invocation_inputs, list) and direct_invocation_inputs:
        results = []
        for entry in direct_invocation_inputs:
            path = entry.get("apiPath") or entry.get("operation") or ""
            method = (entry.get("httpMethod") or entry.get("method") or "POST").upper()
            params = entry.get("parameters") or entry.get("query") or {}
            body = entry.get("requestBody") or entry.get("body") or {}
            results.append(_call_proxy(path, method, params, body))
        rc_results = _rc_result(direct_invocation_id or "direct", direct_invocation_inputs, results)
        state = state or {}
        state["returnControlInvocationResults"] = rc_results
        return {
            "statusCode": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(
                {
                    "returnControlInvocationResults": rc_results,
                    "sessionState": state,
                }
            ),
        }

    try:
        print(
            "[lambda] event_summary",
            json.dumps(
                {
                    "keys": sorted(event.keys()) if isinstance(event, dict) else None,
                    "has_body": bool(event.get("body")) if isinstance(event, dict) else False,
                    "has_invocation_inputs": bool(event.get("invocationInputs")) if isinstance(event, dict) else False,
                    "initial_text_len": len(initial_text or ""),
                    "session_state_keys": sorted((state or {}).keys()) if isinstance(state, dict) else None,
                }
            ),
        )
    except Exception:
        pass

    try:
        agent_id, agent_alias_id = _resolve_agent_context(event, state)
    except ValueError as exc:
        return {
            "statusCode": 500,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"error": str(exc)}),
        }

    for hop in range(RC_MAX_HOPS):
        outbound_text = initial_text if hop == 0 else ""
        if hop > 0 and isinstance(state, dict):
            has_results = bool(state.get("returnControlInvocationResults"))
        else:
            has_results = False
        is_http_event = isinstance(event, dict) and "body" in event
        if is_http_event and not outbound_text and not has_results:
            return {
                "statusCode": 400,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"error": "inputText_required"}),
            }
        try:
            print(
                "[lambda] hop_dispatch",
                json.dumps(
                    {
                        "hop": hop,
                        "textLength": len(outbound_text or ""),
                        "hasReturnControlResults": has_results,
                        "sessionAttributes": list((state or {}).get("sessionAttributes", {}).keys()) if isinstance(state, dict) else None,
                    }
                ),
            )
        except Exception:
            print(f"[lambda] hop_dispatch hop={hop} textLength={len(outbound_text or '')} hasReturnControlResults={has_results}")

        chunk, rc, returned_state = _invoke_once(agent_id, agent_alias_id, session_id, outbound_text, state)
        final_text += chunk or ""
        if returned_state:
            state = returned_state

        if not rc:
            break

        inputs = rc.get("invocationInputs") or []
        results = []
        for entry in inputs:
            path = entry.get("apiPath") or entry.get("operation") or ""
            method = entry.get("httpMethod") or "POST"
            params = entry.get("parameters") or entry.get("query") or {}
            body = entry.get("requestBody") or entry.get("body") or {}
            results.append(_call_proxy(path, method, params, body))

        state = state or {}
        state["returnControlInvocationResults"] = _rc_result(rc.get("invocationId", f"hop-{hop}"), inputs, results)

    response_payload = {"text": final_text}
    if state:
        response_payload["sessionState"] = state

    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(response_payload),
    }

