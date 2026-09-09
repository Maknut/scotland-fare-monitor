#!/usr/bin/env python3
"""
seats.aero award-availability monitor for Eric's Scotland trip.

Watches:
  RDU -> GLA/EDI on 2027-07-09
  GLA/EDI -> RDU on 2027-07-19

Rules:
  * >= 2 seats available
  * 1 stop or fewer
  * flag any itinerary with a layover > 3h
  * alert on NEW options (program + cabin + flight numbers + date not seen before)
  * alert on PRICE DROPS (per program + cabin + route + date, lowest mileage cost)

Usage:
  SEATSAERO_API_KEY=... python3 monitor.py --state state.json [--config config.json] [--dry-run] [--json]

Exit code 0 always (so the scheduler treats a "no change" run as success);
the report on stdout starts with either "NO CHANGES" or "CHANGES DETECTED".
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cash as cash_mod  # noqa: E402

API_BASE = "https://seats.aero/partnerapi"

DEFAULT_CONFIG = {
    "searches": [
        {"label": "Outbound RDU -> GLA/EDI", "origins": ["RDU"], "destinations": ["GLA", "EDI"], "date": "2027-07-09"},
        {"label": "Return GLA/EDI -> RDU", "origins": ["GLA", "EDI"], "destinations": ["RDU"], "date": "2027-07-19"},
    ],
    "min_seats": 2,
    "max_stops": 1,
    "layover_flag_hours": 3.0,
    # seats.aero reports TotalTaxes in the smallest currency unit (cents). The web search
    # URL used max_fees=40000, which we mirror here. Set to null to disable.
    "max_taxes_minor_units": 40000,
    "cabins": ["economy", "premium", "business", "first"],
}

CABIN_NAMES = {"economy": "Economy", "premium": "Premium Economy", "business": "Business", "first": "First"}


# ---------------------------------------------------------------- HTTP ----
_AUTH_STYLE = {"prefix": ""}  # seats.aero docs: raw key in Partner-Authorization. We fall back to "Bearer " on 401.


def api_get(path, params, api_key, retries=3):
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=False)
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "Partner-Authorization": f"{_AUTH_STYLE['prefix']}{api_key}",
            "Accept": "application/json",
            "User-Agent": "eric-scotland-award-monitor/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            if e.code in (401, 403):
                if _AUTH_STYLE["prefix"] == "":
                    _AUTH_STYLE["prefix"] = "Bearer "   # retry once with the other header style
                    continue
                raise SystemExit(f"AUTH ERROR {e.code} from seats.aero — tried both raw and Bearer forms of Partner-Authorization. "
                                 f"Check the key at https://seats.aero/settings (API tab) and that the account still has Pro. Body: {body}")
            if e.code == 404:
                return None
            last_err = f"HTTP {e.code}: {body}"
            if e.code == 429:
                time.sleep(15 * (attempt + 1))
            else:
                time.sleep(3 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = str(e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"seats.aero request failed after {retries} attempts: {path} {params} -> {last_err}")


def cached_search(cfg_search, api_key):
    """Return list of availability objects for one search (single date)."""
    # award_origins/award_destinations widen the points search to hubs seats.aero actually caches
    # (RDU->GLA/EDI has no cached data); cash searches keep using origins/destinations.
    params = {
        "origin_airport": ",".join(cfg_search.get("award_origins") or cfg_search["origins"]),
        "destination_airport": ",".join(cfg_search.get("award_destinations") or cfg_search["destinations"]),
        "start_date": cfg_search["date"],
        "end_date": cfg_search["date"],
        "take": 1000,
        "include_trips": "true",
        "only_direct_flights": "false",
    }
    out, cursor = [], None
    while True:
        if cursor:
            params["cursor"] = cursor
        data = api_get("/search", params, api_key) or {}
        out.extend(data.get("data") or [])
        if not data.get("hasMore"):
            break
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def get_trips(availability_id, api_key):
    data = api_get(f"/trips/{availability_id}", {"include_filtered": "false"}, api_key)
    if not data:
        return []
    return data.get("data") or []


# ------------------------------------------------------------ parsing ----
def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def hours_between(a, b):
    ta, tb = parse_ts(a), parse_ts(b)
    if not ta or not tb:
        return None
    return (tb - ta).total_seconds() / 3600.0


def normalise_cabin(c):
    c = (c or "").lower()
    if c in ("y", "economy"):
        return "economy"
    if c in ("w", "premium", "premium economy", "premium_economy"):
        return "premium"
    if c in ("j", "business"):
        return "business"
    if c in ("f", "first"):
        return "first"
    return c or "unknown"


def trip_to_option(trip, avail, search_label):
    """Flatten a seats.aero trip object into our own record."""
    segs = trip.get("AvailabilitySegments") or trip.get("Segments") or []
    segs = sorted(segs, key=lambda s: (s.get("Order", 0), s.get("DepartsAt", "")))
    stops = trip.get("Stops")
    if stops is None:
        stops = max(len(segs) - 1, 0)

    layovers = []
    for prev, nxt in zip(segs, segs[1:]):
        h = hours_between(prev.get("ArrivesAt"), nxt.get("DepartsAt"))
        layovers.append({
            "airport": nxt.get("OriginAirport") or prev.get("DestinationAirport"),
            "hours": round(h, 2) if h is not None else None,
        })

    flight_numbers = trip.get("FlightNumbers") or ", ".join(
        s.get("FlightNumber", "") for s in segs if s.get("FlightNumber"))

    route = avail.get("Route") or {}
    origin = trip.get("OriginAirport") or (segs[0].get("OriginAirport") if segs else route.get("OriginAirport"))
    dest = trip.get("DestinationAirport") or (segs[-1].get("DestinationAirport") if segs else route.get("DestinationAirport"))
    source = trip.get("Source") or avail.get("Source") or route.get("Source")
    cabin = normalise_cabin(trip.get("Cabin"))
    date = (trip.get("DepartsAt") or avail.get("Date") or "")[:10]

    mileage = trip.get("MileageCost")
    try:
        mileage = int(mileage) if mileage not in (None, "") else None
    except (TypeError, ValueError):
        mileage = None

    taxes = trip.get("TotalTaxes")
    try:
        taxes = int(taxes) if taxes not in (None, "") else None
    except (TypeError, ValueError):
        taxes = None

    key = "|".join([search_label, date, source or "?", cabin, origin or "?", dest or "?", flight_numbers or "?"])
    return {
        "key": key,
        "search": search_label,
        "date": date,
        "source": source,
        "cabin": cabin,
        "origin": origin,
        "destination": dest,
        "flight_numbers": flight_numbers,
        "carriers": trip.get("Carriers"),
        "stops": stops,
        "seats": trip.get("RemainingSeats"),
        "mileage": mileage,
        "taxes": taxes,
        "taxes_currency": trip.get("TaxesCurrency"),
        "departs_at": trip.get("DepartsAt") or (segs[0].get("DepartsAt") if segs else None),
        "arrives_at": trip.get("ArrivesAt") or (segs[-1].get("ArrivesAt") if segs else None),
        "total_duration_min": trip.get("TotalDuration"),
        "layovers": layovers,
        "availability_id": avail.get("ID"),
        "trip_id": trip.get("ID"),
    }


def passes_filters(opt, cfg):
    if opt["seats"] is None or opt["seats"] < cfg["min_seats"]:
        return False
    if opt["stops"] is None or opt["stops"] > cfg["max_stops"]:
        return False
    if opt["cabin"] not in cfg["cabins"]:
        return False
    mt = cfg.get("max_taxes_minor_units")
    if mt is not None and opt["taxes"] is not None and opt["taxes"] > mt:
        return False
    if opt["mileage"] is None:
        return False
    return True


def long_layover(opt, cfg):
    thr = cfg["layover_flag_hours"]
    return [l for l in opt["layovers"] if l["hours"] is not None and l["hours"] > thr]


# -------------------------------------------------------------- fetch ----
def fetch_all(cfg, api_key, log):
    options = {}
    for s in cfg["searches"]:
        avails = cached_search(s, api_key)
        log(f"[{s['label']}] {len(avails)} availability rows from cached search")
        for avail in avails:
            trips = avail.get("AvailabilityTrips")
            if not trips:
                trips = get_trips(avail.get("ID"), api_key)
            for t in trips or []:
                # trips endpoint returns each route/date/source/cabin; re-check date matches the search date
                opt = trip_to_option(t, avail, s["label"])
                if opt["date"] != s["date"]:
                    continue
                if not passes_filters(opt, cfg):
                    continue
                # Keep the cheapest record per key (same flights can be listed under several fare buckets)
                prev = options.get(opt["key"])
                if prev is None or (opt["mileage"] or 1e12) < (prev["mileage"] or 1e12):
                    options[opt["key"]] = opt
        log(f"[{s['label']}] {sum(1 for o in options.values() if o['search'] == s['label'])} options pass filters")
    return options


# --------------------------------------------------------------- diff ----
def price_key(opt):
    return "|".join([opt["search"], opt["date"], opt["source"] or "?", opt["cabin"], opt["origin"] or "?", opt["destination"] or "?"])


def lowest_by_price_key(options):
    best = {}
    for o in options.values():
        k = price_key(o)
        if k not in best or o["mileage"] < best[k]["mileage"]:
            best[k] = o
    return best


def diff(old_state, new_options, cfg):
    old_options = old_state.get("options", {})
    old_best = old_state.get("lowest", {})
    new_best = {k: v["mileage"] for k, v in lowest_by_price_key(new_options).items()}

    new_keys = [k for k in new_options if k not in old_options]
    gone_keys = [k for k in old_options if k not in new_options]
    drops = []
    for k, m in new_best.items():
        if k in old_best and m < old_best[k]:
            drops.append((k, old_best[k], m))
    # Flag long layovers only for options that are new (so the flag doesn't repeat every day)
    flagged = [new_options[k] for k in new_keys if long_layover(new_options[k], cfg)]
    return {
        "new": [new_options[k] for k in new_keys],
        "gone": [old_options[k] for k in gone_keys],
        "drops": drops,
        "flagged": flagged,
        "lowest": new_best,
    }


# ------------------------------------------------------------- report ----
def fmt_time(s):
    t = parse_ts(s)
    return t.strftime("%a %b %-d %H:%M") if t else "?"


def fmt_opt(o, cfg):
    stops_txt = "nonstop" if o["stops"] == 0 else f"{o['stops']} stop"
    lay = ""
    if o["layovers"]:
        lay = " via " + ", ".join(f"{l['airport']} ({l['hours']:.1f}h)" if l["hours"] is not None else str(l["airport"]) for l in o["layovers"])
    flag = "  ⚠ LAYOVER >%gh" % cfg["layover_flag_hours"] if long_layover(o, cfg) else ""
    taxes = ""
    if o["taxes"] is not None:
        taxes = f" + {o['taxes']/100:.0f} {o['taxes_currency'] or ''}".rstrip()
    return (f"{o['origin']}→{o['destination']} {fmt_time(o['departs_at'])} → {fmt_time(o['arrives_at'])}  "
            f"{o['source']} {CABIN_NAMES.get(o['cabin'], o['cabin'])}: {o['mileage']:,} mi{taxes}, "
            f"{o['seats']} seats, {stops_txt}{lay}, {o['flight_numbers']}{flag}")


def report(d, options, cfg, first_run):
    lines = []
    changed = bool(d["new"] or d["drops"] or d["gone"])
    if first_run:
        lines.append("BASELINE ESTABLISHED (first run) — %d options currently pass your filters." % len(options))
    elif changed:
        lines.append("CHANGES DETECTED")
    else:
        lines.append("NO CHANGES — %d options still pass your filters." % len(options))

    if d["drops"]:
        lines.append("\n## Points went DOWN")
        for k, old, new in sorted(d["drops"]):
            search, date, source, cabin, o, dst = k.split("|")
            lines.append(f"- {search} {o}→{dst} {source} {CABIN_NAMES.get(cabin, cabin)}: {old:,} → {new:,} mi (−{old-new:,})")

    if d["new"] and not first_run:
        lines.append("\n## New options")
        for o in sorted(d["new"], key=lambda x: (x["search"], x["mileage"])):
            lines.append("- " + fmt_opt(o, cfg))

    if d["flagged"] and not first_run:
        lines.append("\n## Long-layover flags (new options with a layover > %gh)" % cfg["layover_flag_hours"])
        for o in d["flagged"]:
            lines.append("- " + fmt_opt(o, cfg))

    if d["gone"] and not first_run:
        lines.append("\n## Disappeared since last check")
        for o in sorted(d["gone"], key=lambda x: (x["search"], x["mileage"])):
            lines.append("- " + fmt_opt(o, cfg))

    lines.append("\n## Current best by program + cabin")
    best = lowest_by_price_key(options)
    for s in cfg["searches"]:
        lines.append(f"### {s['label']} ({s['date']})")
        rows = [o for o in best.values() if o["search"] == s["label"]]
        if not rows:
            lines.append("- (nothing passes filters)")
        for o in sorted(rows, key=lambda x: (x["cabin"], x["mileage"])):
            lines.append("- " + fmt_opt(o, cfg))
    return "\n".join(lines)


# --------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, help="path to state.json (read + written)")
    ap.add_argument("--config", help="path to config.json (optional; defaults built in)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + diff but do not write state")
    ap.add_argument("--json", action="store_true", help="also print machine-readable diff")
    ap.add_argument("--dump", help="write raw API responses' parsed options to this path (debugging)")
    args = ap.parse_args()

    api_key = os.environ.get("SEATSAERO_API_KEY")
    if not api_key:
        sys.exit("SEATSAERO_API_KEY env var is required")
    serp_key = os.environ.get("SERPAPI_API_KEY")  # optional: cash tracking is skipped without it

    cfg = dict(DEFAULT_CONFIG)
    cfg.update(cash_mod.DEFAULT_CASH_CONFIG)
    if args.config and os.path.exists(args.config):
        with open(args.config) as f:
            cfg.update(json.load(f))

    old_state = {}
    if os.path.exists(args.state):
        with open(args.state) as f:
            old_state = json.load(f)
    first_run = not old_state.get("options")

    log = lambda m: print(m, file=sys.stderr)
    options = fetch_all(cfg, api_key, log)
    if args.dump:
        with open(args.dump, "w") as f:
            json.dump(options, f, indent=2)

    d = diff(old_state, options, cfg)

    # ---- cash + comparison
    cash, cdiff, rows, new_deals, good_now = {}, {"events": [], "alltime_low": old_state.get("cash_alltime_low", {})}, [], [], {}
    if serp_key:
        try:
            cash = cash_mod.fetch_cash(cfg, cfg, serp_key, log)
            cdiff = cash_mod.diff_cash(old_state, cash, cfg)
            rows = cash_mod.compare_points_to_cash(lowest_by_price_key(options), cash, cfg, cfg["searches"])
            new_deals, good_now = cash_mod.diff_good_deals(old_state, rows)
        except Exception as e:  # cash is best-effort; never lose the points run over it
            log(f"CASH TRACKING FAILED: {e}")
            cash = old_state.get("cash", {})

    changed = bool(d["new"] or d["drops"] or d["gone"] or cdiff["events"] or new_deals)
    header = report(d, options, cfg, first_run)
    if not first_run and changed and not header.startswith("CHANGES"):
        header = "CHANGES DETECTED\n" + header.split("\n", 1)[1]
    print(header)
    if serp_key:
        print(cash_mod.report_cash(cash, cdiff, rows, new_deals, cfg, cfg["layover_flag_hours"], first_run))
    if args.json:
        print("\n--- JSON ---")
        print(json.dumps({"new": [o["key"] for o in d["new"]], "gone": [o["key"] for o in d["gone"]],
                          "drops": d["drops"], "flagged": [o["key"] for o in d["flagged"]],
                          "cash_events": [(e["key"], e["price"], e["reasons"]) for e in cdiff["events"]],
                          "good_deals": [r["key"] for r in new_deals]}, indent=2))

    if not args.dry_run:
        new_state = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "options": options,
            "lowest": d["lowest"],
            "cash": cash,
            "cash_alltime_low": cdiff["alltime_low"],
            "good_deal_cpp": good_now if serp_key else old_state.get("good_deal_cpp", {}),
            "history": (old_state.get("history") or [])[-90:] + [{
                "at": datetime.now(timezone.utc).isoformat(),
                "n_options": len(options),
                "n_new": len(d["new"]), "n_gone": len(d["gone"]), "n_drops": len(d["drops"]),
                "cash_lowest": {k: (v.get("lowest") or {}).get("price") for k, v in cash.items()},
            }],
        }
        with open(args.state, "w") as f:
            json.dump(new_state, f, indent=2)
        with open(os.path.join(os.path.dirname(os.path.abspath(args.state)), "last_run_changed"), "w") as f:
            f.write("1" if (changed and not first_run) else "0")


if __name__ == "__main__":
    main()
