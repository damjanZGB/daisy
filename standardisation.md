# Standardisation

This document defines response rules, formatting, code touch-points, and test steps to make all flight responses consistent, truthful, and PDF-compatible.

Link back: see `todo.md` for task list.

---

## Global Rules

- Max options: Always show at most 10 options across any reply.
- Partitioning: Divide options into two sections:
  - Direct Flights — flights with 0 stops
  - Connecting Flights — flights with 1+ stops
- Nonstop filter: Never exclude connecting flights unless the user explicitly asks for “nonstop/direct only”.
- Truthfulness: Lists are created only from returned offers. If no offers, say so and show nearby-date alternatives or ask for clarifications. Do not hallucinate.
- Return trips: If a return is requested or a `returnDate` is present, include inbound legs and display all segments.
- Required details: Include where available — date, time (HH:MM), duration, carrier, price, and connection carriers.
- PDF compatibility: Keep segment lines using this exact pattern so the frontend PDF generator can parse:
  - `    - THEN {CARRIER}{FLIGHT_NO} {FROM} {HH:MM} -> {TO} {HH:MM}`
  - Avoid Markdown on the THEN line; it must remain plain text.

## Standard Message Template

- Section header (plain text):
  - `Direct Flights`
  - `Connecting Flights`
- Option header (numbered):
  - `{N}) {LABEL} - {DEP} -> {ARR} | {DATE} | {STOPS_TXT} | {DURATION} | **{PRICE CUR}** [| {FIRST_CARRIER}{FLIGHT_NO}]`
- Optional carriers line:
  - `    - Carriers: LH, LX`
- Segment lines (repeat for every segment; inbound legs naturally listed in order when return is present):
  - `    - THEN LH123 FRA 08:00 -> MUC 09:30`

Notes:
- Keep THEN lines unformatted (no bold/italics). The bold price in the header is safe.
- Use `nonstop` for 0 stops; otherwise `N stop` or `N stops`.

## Code Touch-Points (by task)

1) Enforce max 10 options everywhere
- File: `aws/lambda_function.py`
  - Set default `RECOMMENDER_MAX_OPTIONS = 10`.
  - Function search results: limit combined (direct + connecting) to 10 before rendering.
  - Recommender aggregator: partition and limit combined to 10.

2) Partition direct vs connecting sections
- Function search: Build two sections from the `offers` list (`stops == 0` vs others). Cap combined to 10.
- OpenAPI search: Use the exact same partitioned output and 10-option cap.
- Recommender aggregator: Partition `options` the same way before rendering.

3) Always include return legs when requested
- When `returnDate` exists, render THEN lines for all segments (outbound and return). Use segment order returned by the search.

4) Truthful-only lists
- Only render lists when offers/options present.
- If empty: show a short message with nearby alternatives and/or ask for clearer inputs.
- Avoid any synthetic “sample” flights.

5) Standard message template for PDF compatibility
- Ensure the header/section formats match the template above.
- Preserve the exact THEN line regex pattern.
- Use ASCII separators only: `->` and `|` (no `→`, bullets, or control chars). If any non-ASCII ever slip in, sanitize to ASCII before returning.

6) Include full details
- Populate `{DATE}` from the outbound date; duration from `offer.duration`; price from `totalPrice` + currency.
- Add `Carriers:` line from `offer.carriers`.

7) Nonstop only on request
- Respect `nonstop=false` by default; if user asks “nonstop” then set `nonstop=true` in search params; if no results, retry with connections allowed and disclose the relaxation.

8) Recommender (inspiration) uses the same template
- In `recommend_destinations`, when `withItineraries=true` and origin is known, the formatted list must follow the same template and the 10-option cap.

9) Strengthen replay suite
- File: `scripts/replay_sessions.mjs`
  - Add checks per turn for presence of at least one numbered header and at least one THEN line when an action was invoked.
  - Persist a short ‘format_ok’ flag in the saved JSON.

10) Wire PDF generator to real data
- Frontend PDF parser consumes `THEN` lines; ensure we always print them for every segment.
- For multi-segment bookings, the PDF builder produces a page per segment (already supported).
- Round trips: list both outbound and inbound segments so tickets are generated for all legs.

## Alternatives Formatting

- When no offers on requested dates, provide nearby alternatives partitioned into two sections:
  - `Direct Alternatives`
  - `Connecting Alternatives`
- Keep alternatives compact (no THEN lines), but include in each header: date, HH:MM (departure), duration, price, and first segment carrier+flight when available. Include a `Carriers:` line when present.
- Cap combined alternatives to 10.

## Implementation Notes

- “application/json” should be returned only for OpenAPI responses; function responses must return TEXT only (already fixed) to avoid `functionContentType` errors.
- Use utilities already in the codebase for price and HH:MM formatting to keep output consistent.
- Keep message byte size below `RECOMMENDER_MAX_TEXT_BYTES`; for large sets, shorten `Carriers:` lines but keep headers + THEN lines intact.

## Verification

- Manual: Ask for round trip ZAG -> ZRH with return, confirm both sections appear and THEN lines cover outbound + return.
- Replay: Add scripted turns that explicitly request round trip and verify formatting flags.
- PDF: Trigger PDF after selecting an option; ensure a page is created per segment with the correct leg data.
