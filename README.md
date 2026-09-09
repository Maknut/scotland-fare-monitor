# Scotland award + cash fare monitor

Daily check of **RDU → GLA/EDI on 2027-07-09** and **GLA/EDI → RDU on 2027-07-19**.

- **Points** (seats.aero Partner API): every program/cabin with ≥ 2 seats and ≤ 1 stop. Alerts on new options, per-program/per-cabin mileage drops, and flags any itinerary with a layover > 3h.
- **Cash** (Google Flights via SerpApi): per-person USD for economy + business, one-way each direction and round-trip. Alerts on a new all-time low, a ≥ 10% drop vs. the last check, or a fare at/below the targets in `config.json`.
- **Points vs cash**: cents-per-point = (cash − award taxes) ÷ miles. Marked ★ good deal when ≥ 1.25× that program's baseline valuation (baselines in `cash.py`, override in `config.json`).

## How alerts reach you

Runs at 06:30 ET. When anything changed, the workflow opens a GitHub Issue titled "✈️ Scotland fares: changes YYYY-MM-DD" containing the full report — GitHub emails you and pings the GitHub mobile app. No issue = nothing changed. `data/report.md` always holds the latest full report; `data/state.json` holds the history.

## Setup (one time)

1. Private repo → Settings → Secrets and variables → Actions → add `SEATSAERO_API_KEY` and `SERPAPI_API_KEY`.
2. Actions tab → enable workflows → run "Scotland award + cash fare monitor" once manually (establishes the baseline; no issue is opened on the first run).
3. Optional: create a label `fare-alert` so alerts are filterable.

## Route coverage note

seats.aero's cached feed (all the API exposes to Pro keys) has **no data for RDU→GLA/EDI** — the web UI fills that route with live searches, which aren't available via API. So the points search is widened with `award_destinations` / `award_origins` (LHR, DUB, MAN) in `config.json`; add a separate seats.aero **Alert** in the web UI for the exact GLA/EDI route to cover live-only results. Cash searches still use the exact GLA/EDI route.

## Tuning

Edit `config.json` — dates, airports, `min_seats`, `layover_flag_hours`, `cash_targets`, `cash_cabins` (adding `premium` costs 3 more SerpApi searches/day; free plan is 250/month, current usage ≈ 180/month). Local run:

```
SEATSAERO_API_KEY=... SERPAPI_API_KEY=... python3 monitor.py --state data/state.json --config config.json --dry-run
```

## Caveats

- Cash fares are per person; award taxes come back in the program's currency and are converted with rough fixed FX rates for the cpp math.
- The seats.aero cached feed refreshes on its own schedule per program; a "new option" may have existed for a few hours before it shows up.
- The trip is ~10 months out; some airlines haven't loaded schedules yet, so early reports will be thin and fill in over the fall.
