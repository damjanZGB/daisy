# Flexible Dates Adapter

This adds a calendar adapter using Amadeus "Flight Cheapest Date Search" and an optional pricing step for the top N days (one‑way only). It is compatible with the existing Amadeus adapter and preserves LH Group–only pricing.

## Proxy Route

- `GET /tools/amadeus/dates`
- Params
  - `originLocationCode` (IATA, e.g., `MUC`)
  - `destinationLocationCode` (IATA, e.g., `ZRH`)
  - Either `month=YYYY-MM` or `departureDateFrom=YYYY-MM-DD` and `departureDateTo=YYYY-MM-DD`
  - Optional: `oneWay` (bool), `nonStop` (bool), `currencyCode` (string), `limit` (int, <= 10)
- Returns
  - `window: { from, to }`
  - `days: [{ date, price, currency }]` (sorted by ascending price)
  - `top: [{ date, price, currency }]` (first N by price)

Caching: 5‑minute, in‑process cache keyed by O/D/date window and core flags.

## Lambda Usage (Optional Pricing)

OpenAPI: send the same route via Lambda with optional flags:

- Body fields (in addition to calendar params):
  - `priceTop=true` (bool) to enable pricing
  - `priceLimit` (int, <= 10) to cap priced days (defaults to calendar `limit`)
  - `adults` (default 1), `cabin`/`travelClass` (default ECONOMY), `nonStop`, `currencyCode`
  - Pricing supports `oneWay=true` only (roundtrip pricing not implemented here)

Response adds:

- `priced: [{ departureDate, offers: [...] }]`

Notes:

- Pricing applies LH Group only via `includedAirlineCodes` in the Amadeus pricing adapter.
- The calendar endpoint itself does not filter by airline.

## Orchestrator OpenAPI (One‑Call Flex)

To keep the proxy thin while giving the agent a single operation, the Lambda exposes an orchestrator route that runs the calendar and (optionally) prices the top days in one call.

- `GET /tools/amadeus/flex` (OpenAPI → Lambda)
- Body fields (JSON map, same keys as calendar):
  - Required: `originLocationCode`, `destinationLocationCode`
  - Date window: `month=YYYY-MM` or `departureDateFrom/To=YYYY-MM-DD`
  - Optional: `oneWay` (default true), `nonStop`, `currencyCode`, `limit<=10`
  - Pricing: `priceLimit<=10` (defaults to `limit`); always LH Group only
  - Passenger/class: `adults` (default 1), `cabin`/`travelClass` (default ECONOMY)

Returns only `priced[]` for the top N days (one‑way only) along with a `query` echo. The calendar is used internally for selection and is not included in the response to avoid showing unfiltered results. Roundtrip pairing is not implemented here.

Example body:

```
{
  "apiPath": "/tools/amadeus/flex",
  "httpMethod": "GET",
  "body": {
    "originLocationCode": "MUC",
    "destinationLocationCode": "ZRH",
    "month": "2025-11",
    "oneWay": true,
    "nonStop": true,
    "currencyCode": "EUR",
    "limit": 5,
    "priceLimit": 3,
    "adults": 1,
    "travelClass": "ECONOMY"
  }
}
```

## Example

Calendar only:

```
GET /tools/amadeus/dates?originLocationCode=MUC&destinationLocationCode=ZRH&month=2025-11&oneWay=true&nonStop=true&currencyCode=EUR&limit=5
```

Calendar + price top 3 days (OpenAPI → Lambda):

```
{
  "apiPath": "/tools/amadeus/dates",
  "httpMethod": "GET",
  "body": {
    "originLocationCode": "MUC",
    "destinationLocationCode": "ZRH",
    "month": "2025-11",
    "oneWay": true,
    "nonStop": true,
    "currencyCode": "EUR",
    "limit": 5,
    "priceTop": true,
    "priceLimit": 3,
    "adults": 1,
    "travelClass": "ECONOMY"
  }
}
```

## Deployment

Use the provided PowerShell script to package and update the Lambda code:

```
powershell -ExecutionPolicy Bypass -File scripts/deploy_lambda.ps1
```

The script zips `aws/lambda_function.py` and `data/lh_destinations_catalog.json`, then calls `aws lambda update-function-code` for `daisy_in_action-0k2c0` in `us-west-2`. Ensure your AWS CLI profile has access (default: `reStrike`).
