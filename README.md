# Lufthansa Group – Bedrock Agent Chat (One Page)

Modern single-file React chat UI plus a tiny Node proxy for Amazon Bedrock **InvokeAgent**.

## Structure
```
agent-chat/
  public/index.html   # the UI (open directly or served by server.mjs)
  server.mjs          # static server + /invoke proxy
  proxy.mjs           # proxy-only (if hosting HTML elsewhere)
  package.json
  .env.example
```

## Run locally
```bash
npm i
cp .env.example .env  # edit values
npm start             # serves http://localhost:8787 and /invoke
```

Open http://localhost:8787 and in **Settings** keep default `http://localhost:8787/invoke`.

## Deploy on VPS
- Use `pm2 start server.mjs --name agent-chat`.
- Put Nginx in front with TLS and proxy to 8787.
- Lock CORS by setting `ORIGIN=https://your.domain` in `.env`.

## Deploy proxy only (Render/AWS Lambda+API GW)
- Deploy `proxy.mjs` with env: `AWS_REGION`, `AGENT_ID`, `AGENT_ALIAS_ID` (+ AWS creds).
- Host `public/index.html` on static hosting and set the Proxy URL in Settings.

### s3escalator microservice
If you need a standalone worker to store transcripts, logs, or alerts in S3, deploy `s3escalator.mjs` (same pattern as `proxy.mjs`). Required env vars:

| Variable | Purpose |
|----------|---------|
| `PORT` | Port to listen on (default `8788`). |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Credentials with `PutObject` access to the bucket. |
| `S3_BUCKET` | Destination bucket root (defaults to `dAisys-diary` when combined with `S3_PREFIX`). |
| `S3_PREFIX` | Optional top-level prefix. |
| `UPLOADER_TOKEN` | Shared secret the proxy must send via `X-Proxy-Token`. |
| `AGENT_ALIAS_ID`, `AGENT_VERSION` | Optional defaults for folder naming (fallback to request payload). |
| `ORIGIN` | Comma/space-separated allowed origins (supports wildcard `*`). |

Payloads are accepted at `POST /tools/s3escalator` and must include `type`, `path`, `sender`, and a file payload (`file`, `fileBase64`, or `fileData`). Files are stored as:

```
dAisys-diary/{type-or-path}/{sender}/{YYYY-MM-DD}/{original-or-type_timestamp}.log
```

The proxy forwards `/log/transcript` payloads when the following env vars are set:

| Proxy env var | Description |
|---------------|-------------|
| `TRANSCRIPT_UPLOADER_URL` | Full URL to the s3escalator `/tools/s3escalator` endpoint. |
| `TRANSCRIPT_UPLOADER_TOKEN` | Shared secret matching the uploader's `UPLOADER_TOKEN`. |
| `AGENT_VERSION` | Version identifier used in S3 key paths (e.g. `v99`). |

### antiPhaser microservice
Need a lightweight phrase parser? Deploy `antiPhaser.mjs` (same render pattern) to interpret natural-language date ranges.

| Variable | Purpose |
|----------|---------|
| `PORT` | Port to listen on (default `8789`). |
| `ORIGIN` | Comma/space-separated list of allowed origins (supports wildcard `*`). |
| `DEFAULT_TIMEZONE` | Optional fallback timezone for parsing (default `UTC`). |

`POST /tools/antiPhaser` expects `{ "text": "next Friday", "timezone": "Europe/Berlin" }` and returns ISO-formatted depart/return dates plus chrono metadata. A `GET` variant accepts `text`/`timezone` query parameters for quick inspection.

### derDrucker microservice
Use `derDrucker.mjs` when you need itinerary Markdown or PDF ticket snippets.

| Variable | Purpose |
|----------|---------|
| `PORT` | Port to listen on (default `8790`). |
| `ORIGIN` | Comma/space-separated allowed origins (supports wildcard `*`). |
| `DEFAULT_TIMEZONE` | Optional fallback timezone for formatting (default `UTC`). |

`POST /tools/derDrucker/wannaCandy` → `{ offers, max?, timezone? }` and returns Markdown sections highlighting direct/connecting options.
`POST /tools/derDrucker/generateTickets` → `{ offer, pnr, timezone?, fileNamePrefix? }` and responds with base64-encoded PDF tickets per leg.


### Tool catalogue endpoint
- `GET /tools/give_me_tools` returns a JSON list (`tools: [...]`) with `tool_name`, `tool_description`, and `tool_route` for every major proxy capability.

If Amadeus returns no results for flexible dates searches, the proxy now responds with an empty payload (HTTP 200) instead of a 502 so the agent can report "no flights found" gracefully.

## IATA dataset & nearest-airport detection
- `data/iata.md` lists codes with latitude/longitude so the UI can map a user's location to the closest departure airport.
- Run `node scripts/enrich-iata.js` after refreshing `data/openflights_airports.dat` to regenerate `iata.json` and `backend/iata.json` with updated coordinates.
- `/tools/iata/lookup` accepts `lat`, `lon`, and `limit` query params and returns airports ordered by distance (each record includes a `distanceKm` field).
- The Paul/Gina/Origin frontends call the lookup endpoint with coordinates first and gracefully fall back to city/country searches if no nearby airport is found.
