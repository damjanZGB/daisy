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

## IATA dataset & nearest-airport detection
- `data/iata.md` lists codes with latitude/longitude so the UI can map a user's location to the closest departure airport.
- Run `node scripts/enrich-iata.js` after refreshing `data/openflights_airports.dat` to regenerate `iata.json` and `backend/iata.json` with updated coordinates.
- `/tools/iata/lookup` accepts `lat`, `lon`, and `limit` query params and returns airports ordered by distance (each record includes a `distanceKm` field).
- The Paul/Gina/Origin frontends call the lookup endpoint with coordinates first and gracefully fall back to city/country searches if no nearby airport is found.
