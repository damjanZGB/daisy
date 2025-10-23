# aws/lambda_function.py — Bedrock Return-Control bridge for Daisy microservices
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
AGENT_ID = os.getenv("AGENT_ID") or os.getenv("SUPERVISOR_AGENT_ID")
AGENT_ALIAS_ID = os.getenv("AGENT_ALIAS_ID") or os.getenv("SUPERVISOR_AGENT_ALIAS_ID")
PROXY_BASE_URL = (os.getenv("PROXY_BASE_URL") or "https://origin-daisy.onrender.com").rstrip("/")
RC_MAX_HOPS = int(os.getenv("RETURN_CONTROL_MAX_HOPS") or "6")
HTTP_TIMEOUT = int(os.getenv("PROXY_TIMEOUT_SECONDS") or "60")

bedrock = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)


def _invoke_once(session_id: str, text: str | None, session_state: dict | None):
    response = bedrock.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS_ID,
        sessionId=session_id,
        inputText=text or "",
        enableTrace=True,
        sessionState=session_state or {},
    )
    aggregated_text = ""
    return_control = None
    for event in response.get("completion", []):
        if "chunk" in event:
            chunk_bytes = event["chunk"].get("bytes")
            if chunk_bytes:
                aggregated_text += base64.b64decode(chunk_bytes).decode("utf-8", "ignore")
        if "returnControl" in event:
            return_control = event["returnControl"]
    return aggregated_text, return_control, response.get("sessionState")


def _proxy_url(path: str) -> str:
    return f"{PROXY_BASE_URL}/{path.lstrip('/')}"


def _call_proxy(path: str, method: str, params: dict | None, body: dict | None) -> dict:
    url = _proxy_url(path)
    method = (method or "POST").upper()
    data = None
    headers = {}
    if method == "GET":
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
    else:
        headers["content-type"] = "application/json"
        data = json.dumps(body or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
            payload = resp.read().decode("utf-8", "ignore")
            status = resp.getcode()
    except urllib.error.HTTPError as err:
        payload = err.read().decode("utf-8", "ignore")
        status = err.code
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    try:
        data = json.loads(payload) if payload else {}
    except Exception:
        data = {"raw": payload}
    return {"ok": status < 400, "status": status, "data": data}


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

    for hop in range(RC_MAX_HOPS):
        chunk, rc, returned_state = _invoke_once(session_id, initial_text if hop == 0 else "", state)
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
