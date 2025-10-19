#!/usr/bin/env python3
import argparse, subprocess, json, sys, time, datetime as dt
from typing import List, Dict, Any

def epoch_ms(dt_str: str) -> int:
    return int(dt.datetime.fromisoformat(dt_str.replace('Z','+00:00')).timestamp()*1000)


def run_aws(cmd: List[str]) -> dict:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"AWS CLI failed: {p.stderr.strip()}")
    try:
        return json.loads(p.stdout or '{}')
    except json.JSONDecodeError:
        return {}


def parse_log_message(msg: str) -> Dict[str, Any]:
    out = {"raw": msg}
    if '|' in msg:
        parts = msg.split('|', 1)
        out["message"] = parts[0].strip()
        j = parts[1].strip()
        try:
            out["details"] = json.loads(j)
        except Exception:
            out["details_text"] = j
    else:
        out["message"] = msg.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--minutes', type=int, default=30)
    ap.add_argument('--region', default='us-west-2')
    ap.add_argument('--group', default='/aws/lambda/daisy_in_action-0k2c0')
    ap.add_argument('--out', default='analytics/replay/proxy_logs_summary.json')
    args = ap.parse_args()

    end = int(time.time()*1000)
    start = end - args.minutes*60*1000
    patterns = ['Proxy POST request','Proxy POST response received','Proxy GET request','Proxy GET response received','IATA lookup via proxy','OpenAPI proxy IATA lookup success','Amadeus search request prepared','Amadeus search completed','Summarizing offers','OpenAPI flight search success','OpenAPI normalized flight fields','OpenAPI flight request prepared']

    records: List[Dict[str, Any]] = []
    for pat in patterns:
        data = run_aws([
            'aws','logs','filter-log-events',
            '--log-group-name', args.group,
            '--start-time', str(start),
            '--end-time', str(end),
            '--filter-pattern', pat,
            '--region', args.region,
        ])
        for ev in data.get('events', []):
            item = parse_log_message(ev.get('message',''))
            item['timestamp'] = ev.get('timestamp')
            item['logStreamName'] = ev.get('logStreamName')
            item['pattern'] = pat
            records.append(item)

    # Sort and group crude sessions by log stream + temporal proximity
    records.sort(key=lambda x: (x.get('logStreamName',''), x.get('timestamp',0)))

    sessions: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {"logStreamName": None, "events": []}
    last_ts = None
    for r in records:
        if current["logStreamName"] != r['logStreamName'] or (last_ts and r['timestamp'] - last_ts > 90_000):
            if current["events"]:
                sessions.append(current)
            current = {"logStreamName": r['logStreamName'], "events": []}
        current['events'].append(r)
        last_ts = r['timestamp']
    if current['events']:
        sessions.append(current)

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump({"generatedAt": dt.datetime.utcnow().isoformat()+"Z", "windowMinutes": args.minutes, "sessions": sessions}, f, indent=2)
    print(f"Wrote {args.out} with {len(sessions)} grouped event sets and {len(records)} events")

if __name__ == '__main__':
    main()


