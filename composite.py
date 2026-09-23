"""
EDI/GLA itineraries built from cached award segments + a cash positioning flight.

seats.aero's API only exposes its cache, and the cache has no RDU<->EDI/GLA data
(the website fills that route with live searches, which Pro API keys can't use).
So we build the priority-airport trip two ways:

  Europe-side:  RDU -> LHR/DUB/MAN on points   + cash hop LHR/DUB/MAN -> EDI/GLA
                EDI/GLA -> LHR/DUB/MAN cash hop + LHR/DUB/MAN -> RDU on points
  US-side:      RDU -> EWR/JFK/BOS/... cash hop + gateway -> EDI/GLA on points
                EDI/GLA -> gateway on points    + gateway -> RDU cash hop

Only connections with >= self_transfer_min_hours and <= self_transfer_max_hours
on the ground are kept (separate tickets: bags recheck, no misconnect protection).
Cents-per-point = (EDI/GLA cash fare - award taxes - hop fare) / miles.

Positioning fares are nonstop economy, refreshed every positioning_refresh_days to
stay inside the SerpApi free tier.
"""
import time
from datetime import datetime, timedelta, timezone

import cash as cash_mod


def naive(s):
    """Both sources report local airport times; compare them as naive datetimes."""
    if not s:
        return None
    s = s.replace("Z", "").replace("T", " ")[:16]
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _hop_search(tag, dep, arr, date, cfg, api_key):
    data = cash_mod.serp_get({"departure_id": ",".join(dep), "arrival_id": ",".join(arr),
                              "outbound_date": date, "type": 2, "travel_class": 1,
                              "adults": cfg["cash_adults"], "stops": 1}, api_key)  # stops=1 => nonstop only
    out = []
    for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        fl = it.get("flights") or []
        if not fl or it.get("price") is None:
            continue
        out.append({"tag": tag, "price": it["price"],
                    "origin": fl[0]["departure_airport"]["id"], "destination": fl[-1]["arrival_airport"]["id"],
                    "dep": fl[0]["departure_airport"].get("time"), "arr": fl[-1]["arrival_airport"].get("time"),
                    "airline": fl[0].get("airline"), "flight_numbers": ", ".join(f.get("flight_number", "") for f in fl)})
    return out


def fetch_positioning(cfg, old_state, api_key, log):
    cached = old_state.get("positioning") or {}
    ts = cached.get("fetched_at")
    if ts:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
        if age < timedelta(days=cfg["positioning_refresh_days"]):
            log(f"[positioning] using cached fares from {ts[:10]}")
            return cached
    home, pri, hubs, gws = cfg["home"], cfg["priority_airports"], cfg["euro_hubs"], cfg["us_gateways"]
    out_d = cfg["searches"][0]["date"]
    ret_d = cfg["searches"][1]["date"]
    next_d = (datetime.strptime(out_d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    plan = [("euro_out", hubs, pri, out_d), ("euro_out", hubs, pri, next_d),   # overnight arrivals land next day
            ("euro_ret", pri, hubs, ret_d),
            ("us_out", [home], gws, out_d),
            ("us_ret", gws, [home], ret_d)]
    fares = []
    for tag, dep, arr, d in plan:
        try:
            got = _hop_search(tag, dep, arr, d, cfg, api_key)
            fares += got
            log(f"[positioning {tag} {d}] {len(got)} nonstop fares")
        except Exception as e:
            log(f"[positioning {tag} {d}] FAILED: {e}")
        time.sleep(1.0)
    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "fares": fares}


def build(options, positioning, cash, cfg):
    home, pri = cfg["home"], set(cfg["priority_airports"])
    hubs, gws = set(cfg["euro_hubs"]), set(cfg["us_gateways"])
    lo = timedelta(hours=cfg["self_transfer_min_hours"])
    hi = timedelta(hours=cfg["self_transfer_max_hours"])
    fares = positioning.get("fares") or []
    rows = []
    for o in options.values():
        leg = o.get("leg")
        dep, arr = naive(o["departs_at"]), naive(o["arrives_at"])
        if not leg or not dep or not arr:
            continue
        if leg == "outbound" and o["origin"] == home and o["destination"] in hubs:
            side, pool = "after", [f for f in fares if f["tag"] == "euro_out" and f["origin"] == o["destination"]]
            gap = lambda f: naive(f["dep"]) - arr
        elif leg == "outbound" and o["origin"] in gws and o["destination"] in pri:
            side, pool = "before", [f for f in fares if f["tag"] == "us_out" and f["destination"] == o["origin"]]
            gap = lambda f: dep - naive(f["arr"])
        elif leg == "return" and o["origin"] in hubs and o["destination"] == home:
            side, pool = "before", [f for f in fares if f["tag"] == "euro_ret" and f["destination"] == o["origin"]]
            gap = lambda f: dep - naive(f["arr"])
        elif leg == "return" and o["origin"] in pri and o["destination"] in gws:
            side, pool = "after", [f for f in fares if f["tag"] == "us_ret" and f["origin"] == o["destination"]]
            gap = lambda f: naive(f["dep"]) - arr
        else:
            continue
        ok = [f for f in pool if naive(f["dep"]) and naive(f["arr"]) and lo <= gap(f) <= hi]
        if not ok:
            continue
        hop = min(ok, key=lambda f: (f["price"], gap(f)))
        first, last = (hop, o) if side == "before" else (o, hop)
        start = first["origin"]
        end = last["destination"]
        via = o["destination"] if side == "after" else o["origin"]
        cash_lo = ((cash.get(f"{leg}|{o['cabin']}") or {}).get("lowest_clean")
                   or (cash.get(f"{leg}|{o['cabin']}") or {}).get("lowest"))
        tx = cash_mod.taxes_usd(o, cfg)
        cpp = None
        if cash_lo and o["mileage"]:
            cpp = round((cash_lo["price"] - tx - hop["price"]) / o["mileage"] * 100.0, 2)
        base = cfg["program_cpp_baseline"].get((o["source"] or "").lower(), cfg["program_cpp_baseline"]["default"])
        rows.append({
            "key": "composite|" + o["key"], "leg": leg, "cabin": o["cabin"], "source": o["source"],
            "route": f"{start}→{end} via {via}", "mileage": o["mileage"], "taxes_usd": round(tx, 0),
            "cash": cash_lo["price"] if cash_lo else None, "cpp": cpp, "baseline": base,
            "good_deal": cpp is not None and cpp >= base * cfg["good_cpp_multiplier"],
            "award": o, "hop": hop, "hop_side": side, "ground_hours": round(gap(hop).total_seconds() / 3600, 1),
        })
    rows.sort(key=lambda r: (r["leg"] != "outbound", r["cabin"], -(r["cpp"] or 0)))
    return rows


def _t(s):
    d = naive(s)
    return d.strftime("%a %H:%M") if d else "?"


def report(rows, direct_count, cfg):
    lines = ["\n## Points into / out of EDI & GLA"]
    if direct_count == 0:
        lines.append("Single-ticket RDU↔EDI/GLA awards: none in seats.aero's cache (the API can't see live-only "
                     "results — your seats.aero web alert covers those). Built from award + separate cash hop instead:")
    if not rows:
        lines.append("- (no award segment currently pairs with a workable positioning flight)")
        return "\n".join(lines)
    per = cfg["composite_show_per_cabin"]
    shown = {}
    for r in rows:
        k = (r["leg"], r["cabin"])
        shown[k] = shown.get(k, 0) + 1
        if shown[k] > per:
            continue
        a, h = r["award"], r["hop"]
        award_txt = (f"{a['source']} {a['mileage']:,} mi + ${r['taxes_usd']:,.0f} "
                     f"({a['origin']}→{a['destination']} {_t(a['departs_at'])}→{_t(a['arrives_at'])}, {a['flight_numbers']})")
        hop_txt = (f"${h['price']:,} cash {h['origin']}→{h['destination']} "
                   f"({_t(h['dep'])}→{_t(h['arr'])}, {h['flight_numbers']})")
        parts = [hop_txt, award_txt] if r["hop_side"] == "before" else [award_txt, hop_txt]
        val = f" → {r['cpp']:.2f}¢/pt vs ${r['cash']:,} cash" if r["cpp"] is not None else ""
        star = "  ★ good deal" if r["good_deal"] else ""
        lines.append(f"- {r['leg']} {r['cabin']} {r['route']}: {parts[0]} + {parts[1]}, "
                     f"{r['ground_hours']}h self-transfer{val}{star}")
    lines.append("  Separate tickets: bags are rechecked and a late first flight doesn't protect the second. "
                 "Heathrow means an AA (T3) → BA (T5) terminal change.")
    return "\n".join(lines)
