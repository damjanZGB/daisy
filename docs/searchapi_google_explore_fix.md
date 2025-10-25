# Google Explore 400 Troubleshooting & Recommended Fix (2025-10-24)

## Issue Summary
During session **LH3792** (2025-10-24T19:56Z) the `daisy_in_action` Lambda called
`/google/explore/search` twice and received HTTP **400** responses. The agent
responded with “issue with the language/country code” and did not provide any
inspiration or flight options.

CloudWatch logs:
```
[lambda] tool_query {"path": "/google/explore/search", "method": "GET",
 "query": [["engine","google_travel_explore"],["hl","en"],["adults","1"],
           ["gl","EU"],["departure_id","default"], ... ]}
[lambda] tool_invocation {... "status": 400, "ok": false }
```
The retry modified `hl` to `en-US`, but kept `gl="EU"` and
`departure_id="default"`, triggering the same 400.

## Root Cause
- **`departure_id`** was not a valid value (SearchAPI expects a 3-letter IATA
  code, a Google kgmid (`/m/...`), or bounding box). The UI inferred ZAG but the
  request forwarded the placeholder string `"default"`.
- **`gl`** was set to `"EU"`, which is not a valid ISO-3166 alpha-2 country
  code (SearchAPI docs enumerate country codes; regional blocks like “EU” are
  rejected).

Because both parameters violated SearchAPI requirements, the explore tool could
not return results; the agent simply echoed the error.

Reference (SearchAPI documentation):
<https://www.searchapi.io/docs/google-travel-explore-api>
> *`departure_id`: “Departure location as 3-letter IATA code or kgmid starting
> with '/m/'”*  
> *`gl`: “Geolocation parameter … must be a valid ISO 3166-1 alpha-2 country
> code.”*

## Recommended Fix

1. **Sanitise Explore requests in Lambda**
   - When `_call_proxy` processes `/google/explore/`:
     - Treat values such as `"default"` or any non-IATA strings as invalid.
     - If invalid or missing, fall back to a safe default (`FRA`) and log the
       adjustment. (We plan to replace this with geo-derived inference in a
       later iteration.)
     - Recompute `gl` from the resolved IATA (`_country_for_iata` already does
       this); if the lookup fails, fall back to `DE`. Always send `hl=en-US`.
     - Log a warning when the fallback kicks in so we know how often it occurs.
     - Always pass `included_airlines=LH,LX,OS,SN,EW,4Y,EN` and clamp results to
       at most 10 entries before responding.

2. **Propagate actionable errors**
   - If SearchAPI still returns 400, inject a structured error back to the
     agent (e.g., “Explore requires a valid departure airport – please confirm
     your origin”). This prevents looping with vague messages.

3. **Instruction update (optional but recommended)**
   - Remind agents to call `/tools/iata/lookup` and reuse the resolved IATA
     code when tool schemas require `departure_id`.

Implementing the sanitiser in Lambda is sufficient to unblock users even if
some agent responses still pass placeholders.

## Next Steps Checklist
- [ ] Replace the FRA/DE hard-fallback with `_nearest_iata(lat, lon)` so the
      inferred airport comes from the user’s location.
- [x] Extend `_call_proxy` to replace invalid `departure_id`/`gl` before the
      SearchAPI call and enforce LH-only + 10-result cap.
- [ ] Update agent instructions once sanitiser ships.
- [ ] Smoke test: run a session without manually confirming origin; verify
      `/google/explore/search` succeeds and returns destinations.
