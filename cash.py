"""
Cash-fare tracking via SerpApi's Google Flights engine, plus points-vs-cash comparison.

Searches (per person, USD):
  * One-way RDU -> GLA/EDI  on the outbound date, per cabin
  * One-way GLA/EDI -> RDU  on the return date,   per cabin
  * Round-trip RDU <-> GLA/EDI, per cabin   (what Eric would actually pay)

Filters mirror the points rules: <= 1 stop; layovers > 3h are flagged.

"Good deal" logic (all thresholds live in config):
  CASH   - new all-time low since tracking began for that leg+cabin
         - drop of >= cash_drop_pct vs. last check
         - at/below the target price you set (cash_targets)
  POINTS - cents-per-point = (cash - taxes) / miles * 100, computed against the
           matching one-way cash fare for the same cabin. Flagged when cpp >=
           program baseline valuation * good_cpp_multiplier (default 1.25x).
"""
import json
import time
import urllib.parse
import urllib.request
import urllib.error

SERP_URL = "https://serpapi.com/search.json"
TRAVEL_CLASS = {"economy": 1, "premium": 2, "business": 3, "first": 4}

DEFAULT_CASH_CONFIG = {
    "cash_cabins": ["economy", "business"],       # 2 cabins x 3 searches = 6 calls/day = ~180/mo (free plan = 250)
    "cash_adults": 1,                               # per-person pricing so it lines up with per-person miles
    "cash_drop_pct": 10,                            # alert on >=10% drop vs last check
    "cash_targets": {                               # per-person USD; alert when at/below. Tune these.
        "outbound|economy": 500, "return|economy": 500, "roundtrip|economy": 900,
        "outbound|business": 2200, "return|business": 2200, "roundtrip|business": 3500,
    },
    # Rough "what a point is worth" baselines (cents). Used only for the good-deal flag.
    "program_cpp_baseline": {
        "aeroplan": 1.5, "united": 1.3, "delta": 1.2, "american": 1.5, "alaska": 1.5,
        "virginatlantic": 1.4, "flyingblue": 1.3, "british": 1.3, "aeromexico": 1.3,
        "lifemiles": 1.5, "etihad": 1.2, "emirates": 1.2, "qantas": 1.3, "velocity": 1.3,
        "jetblue": 1.3, "smiles": 1.2, "eurobonus": 1.2, "turkish": 1.3, "azul": 1.2,
        "saudia": 1.2, "copa": 1.4, "ethiopian": 1.0, "finnair": 1.3, "default": 1.3,
    },
    "good_cpp_multiplier": 1.25,
    # crude FX for seats.aero taxes that come back in other currencies
    "fx_to_usd": {"USD": 1.0, "GBP": 1.30, "EUR": 1.12, "CAD": 0.73, "AUD": 0.66, "JPY": 0.0068, "MXN": 0.052, "BRL": 0.18},
}


# ---------------------------------------------------------------- HTTP ----
def serp_get(params, api_key, retries=3):
    params = dict(params, api_key=api_key, engine="google_flights", hl="en", gl="us", currency="USD")
    url = SERP_URL + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "eric-scotland-monitor/1.0"}), timeout=90) as r:
                data = json.load(r)
            if data.get("error"):
                last = data["error"]
                # "no results" style errors are terminal, not retryable
                if "hasn't returned any results" in last.lower() or "no results" in last.lower():
                    return {"best_flights": [], "other_flights": [], "_error": last}
                time.sleep(5 * (attempt + 1))
                continue
            return data
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403):
                raise SystemExit(f"SERPAPI AUTH ERROR {e.code}: {body}")
            last = f"HTTP {e.code}: {body}"
            time.sleep(5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"SerpApi request failed: {last}")


def search(leg, cabin, cfg, searches, api_key):
    """leg in {'outbound','return','roundtrip'}; returns list of normalized itineraries."""
    out, ret = searches[0], searches[1]
    if leg == "outbound":
        p = {"departure_id": ",".join(out["origins"]), "arrival_id": ",".join(out["destinations"]),
             "outbound_date": out["date"], "type": 2}
    elif leg == "return":
        p = {"departure_id": ",".join(ret["origins"]), "arrival_id": ",".join(ret["destinations"]),
             "outbound_date": ret["date"], "type": 2}
    else:
        p = {"departure_id": ",".join(out["origins"]), "arrival_id": ",".join(out["destinations"]),
             "outbound_date": out["date"], "return_date": ret["date"], "type": 1}
    p.update({"travel_class": TRAVEL_CLASS[cabin], "adults": cfg["cash_adults"], "stops": 2})  # stops=2 => 1 stop or fewer
    data = serp_get(p, api_key)
    itins = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    result = []
    for it in itins:
        flights = it.get("flights") or []
        if not flights or it.get("price") is None:
            continue
        layovers = [{"airport": l.get("id"), "hours": round((l.get("duration") or 0) / 60.0, 2)} for l in (it.get("layovers") or [])]
        result.append({
            "leg": leg, "cabin": cabin,
            "price": it["price"],
            "origin": flights[0]["departure_airport"]["id"],
            "destination": flights[-1]["arrival_airport"]["id"],
            "departs_at": flights[0]["departure_airport"].get("time"),
            "arrives_at": flights[-1]["arrival_airport"].get("time"),
            "airlines": sorted({f.get("airline") for f in flights if f.get("airline")}),
            "flight_numbers": ", ".join(f.get("flight_number", "") for f in flights),
            "stops": len(flights) - 1,
            "layovers": layovers,
            "total_duration_min": it.get("total_duration"),
        })
    result.sort(key=lambda x: x["price"])
    return result, data.get("_error")


def fetch_cash(cfg, points_cfg, api_key, log):
    """Returns {leg|cabin: {"lowest": itinerary, "lowest_clean": itinerary-without-long-layover, "all": [...]}}"""
    res = {}
    for cabin in cfg["cash_cabins"]:
        for leg in ("outbound", "return", "roundtrip"):
            itins, err = search(leg, cabin, cfg, points_cfg["searches"], api_key)
            key = f"{leg}|{cabin}"
            thr = points_cfg["layover_flag_hours"]
            clean = [i for i in itins if not any(l["hours"] > thr for l in i["layovers"])]
            res[key] = {"lowest": itins[0] if itins else None,
                        "lowest_clean": clean[0] if clean else None,
                        "count": len(itins), "error": err}
            log(f"[cash {key}] {len(itins)} itineraries; lowest={itins[0]['price'] if itins else None}")
            time.sleep(1.0)
    return res


# ---------------------------------------------------------------- diff ----
def diff_cash(old_state, cash, cfg):
    old = old_state.get("cash", {}) or {}
    lows = dict(old_state.get("cash_alltime_low", {}) or {})
    events = []
    for key, cur in cash.items():
        lo = cur["lowest"]
        if not lo:
            continue
        price = lo["price"]
        prev = (old.get(key) or {}).get("lowest") or {}
        prev_price = prev.get("price")
        leg, cabin = key.split("|")
        target = cfg["cash_targets"].get(key)
        reasons = []
        if lows.get(key) is None or price < lows[key]:
            if lows.get(key) is not None:
                reasons.append(f"new all-time low (was ${lows[key]:,})")
            lows[key] = price
        if prev_price and price <= prev_price * (1 - cfg["cash_drop_pct"] / 100.0):
            reasons.append(f"down {100*(prev_price-price)/prev_price:.0f}% vs last check (${prev_price:,})")
        if target is not None and price <= target:
            reasons.append(f"at/below your ${target:,} target")
        if reasons:
            events.append({"key": key, "price": price, "prev": prev_price, "reasons": reasons, "itin": lo})
    return {"events": events, "alltime_low": lows}


# ------------------------------------------------------ points vs cash ----
def taxes_usd(opt, cfg):
    if opt.get("taxes") is None:
        return 0.0
    fx = cfg["fx_to_usd"].get((opt.get("taxes_currency") or "USD").upper(), 1.0)
    return opt["taxes"] / 100.0 * fx


def compare_points_to_cash(best_points, cash, cfg, searches):
    """best_points: {price_key: option} from monitor.lowest_by_price_key. Returns list of rows with cpp."""
    label_to_leg = {searches[0]["label"]: "outbound", searches[1]["label"]: "return"}
    rows = []
    for opt in best_points.values():
        leg = label_to_leg.get(opt["search"])
        c = cash.get(f"{leg}|{opt['cabin']}") if leg else None
        cash_lo = (c or {}).get("lowest")
        if not cash_lo or not opt.get("mileage"):
            continue
        tx = taxes_usd(opt, cfg)
        cpp = (cash_lo["price"] - tx) / opt["mileage"] * 100.0
        base = cfg["program_cpp_baseline"].get((opt["source"] or "").lower(), cfg["program_cpp_baseline"]["default"])
        rows.append({
            "leg": leg, "cabin": opt["cabin"], "source": opt["source"], "route": f"{opt['origin']}→{opt['destination']}",
            "mileage": opt["mileage"], "taxes_usd": round(tx, 0), "cash": cash_lo["price"],
            "cpp": round(cpp, 2), "baseline": base,
            "good_deal": cpp >= base * cfg["good_cpp_multiplier"],
            "key": opt["key"],
        })
    rows.sort(key=lambda r: (r["leg"], r["cabin"], -r["cpp"]))
    return rows


def diff_good_deals(old_state, rows):
    """Only flag a points good-deal the first time that option qualifies (or when its cpp jumps >= 0.25)."""
    prev = old_state.get("good_deal_cpp", {}) or {}
    new_flags, current = [], {}
    for r in rows:
        if r["good_deal"]:
            current[r["key"]] = r["cpp"]
            if r["key"] not in prev or r["cpp"] >= prev[r["key"]] + 0.25:
                new_flags.append(r)
    return new_flags, current


# -------------------------------------------------------------- report ----
def fmt_itin(i, thr):
    stops = "nonstop" if i["stops"] == 0 else f"{i['stops']} stop"
    lay = ""
    if i["layovers"]:
        lay = " via " + ", ".join(f"{l['airport']} ({l['hours']:.1f}h)" for l in i["layovers"])
    flag = f"  ⚠ LAYOVER >{thr:g}h" if any(l["hours"] > thr for l in i["layovers"]) else ""
    return (f"${i['price']:,} {i['origin']}→{i['destination']} {i['departs_at'] or '?'} → {i['arrives_at'] or '?'}, "
            f"{'/'.join(i['airlines'])}, {stops}{lay}, {i['flight_numbers']}{flag}")


def report_cash(cash, cdiff, rows, new_deals, cfg, thr, first_run):
    lines = []
    if cdiff["events"] and not first_run:
        lines.append("\n## Cash fares — worth a look")
        for e in cdiff["events"]:
            leg, cabin = e["key"].split("|")
            lines.append(f"- {leg} {cabin}: {'; '.join(e['reasons'])} → " + fmt_itin(e["itin"], thr))
    if new_deals and not first_run:
        lines.append("\n## Points vs cash — GOOD DEALS (cpp ≥ %.2fx program baseline)" % cfg["good_cpp_multiplier"])
        for r in new_deals:
            lines.append(f"- {r['leg']} {r['cabin']} {r['route']} via {r['source']}: {r['mileage']:,} mi + ${r['taxes_usd']:,.0f} "
                         f"vs ${r['cash']:,} cash → {r['cpp']:.2f}¢/pt (baseline {r['baseline']}¢)")

    lines.append("\n## Current cash floor (per person, USD, ≤1 stop)")
    for key in sorted(cash):
        c = cash[key]
        if c["lowest"] is None:
            lines.append(f"- {key}: no fares returned" + (f" ({c['error']})" if c.get("error") else ""))
            continue
        lines.append(f"- {key}: " + fmt_itin(c["lowest"], thr))
        if c["lowest_clean"] and c["lowest_clean"] is not c["lowest"]:
            lines.append(f"    cheapest without a long layover: " + fmt_itin(c["lowest_clean"], thr))

    if rows:
        lines.append("\n## Points vs cash — all programs (cents per point, higher = better use of miles)")
        for r in rows:
            star = "  ★ good deal" if r["good_deal"] else ""
            lines.append(f"- {r['leg']} {r['cabin']} {r['route']} {r['source']}: {r['mileage']:,} mi + ${r['taxes_usd']:,.0f} "
                         f"vs ${r['cash']:,} → {r['cpp']:.2f}¢/pt{star}")
    return "\n".join(lines)
