"""
Kvallskorning: fyll morgonens order till dagens oppningskurs, stang positioner
som natt stop, mal eller tidsexit, utvardera och justera vikterna.

Kor:  python3 run_evening.py [YYYY-MM-DD]
"""
import sys
import datetime as dt

import numpy as np
import pandas as pd

import omxagent as A


def company_group(ticker):
    return ticker.split(".")[0].split("-")[0]


def _bar(df, date):
    if df is None:
        return None
    m = df[df["date"].dt.date.astype(str) == date]
    return None if m.empty else m.iloc[-1]


# --------------------------------------------------------------------------
# 1. Fyll morgonens order till dagens faktiska oppningskurs
# --------------------------------------------------------------------------
def fill_pending(p, prices, today, strat):
    pend = A.load_json(A.DATA / "pending.json", {})
    if not pend or pend.get("date") != today:
        if pend:
            print(f"Ingen order for idag (senaste ordern ar fran "
                  f"{pend.get('date')}).")
        return [], []

    risk = strat["risk"]
    max_gap = risk.get("max_entry_gap_pct", 2.0)
    equity = p["equity"] or risk["start_capital_sek"]
    groups = {company_group(x["ticker"]) for x in p["open_positions"]}
    gross = sum(abs(x["shares"] * x["entry"]) for x in p["open_positions"])
    max_gross = equity * risk.get("max_gross_exposure_pct", 100) / 100.0

    filled, rejected = [], []
    for o in pend.get("orders", []):
        bar = _bar(prices.get(o["ticker"]), today)
        if bar is None:
            rejected.append(f"{o['ticker']} (ingen kurs for dagen)")
            continue

        px = float(bar["open"])
        sgn = 1 if o["direction"] == "long" else -1

        # Har kursen redan sprungit ivag i signalens riktning ar kanten borta.
        gap = sgn * (px / o["ref_price"] - 1) * 100
        if gap > max_gap:
            rejected.append(f"{o['ticker']} (gapade {gap:.1f}% i signalens "
                            f"riktning, over taket {max_gap}%)")
            continue
        if len(p["open_positions"]) >= risk["max_positions"]:
            rejected.append(f"{o['ticker']} (maxantal positioner)")
            continue
        g = company_group(o["ticker"])
        if g in groups:
            rejected.append(f"{o['ticker']} (redan exponering mot bolaget)")
            continue

        # Storleken raknas om mot den faktiska fyllningskursen sa att risken
        # ner till stoppen blir den avsedda.
        stop_dist = max(o["stop_dist"], 0.01)
        shares = int(max(equity * risk["risk_per_trade_pct"] / 100.0 / stop_dist, 0))
        shares = max(min(shares, int(equity * risk["max_weight_per_position"] / px)), 0)
        if shares == 0:
            rejected.append(f"{o['ticker']} (for liten position)")
            continue

        notional = shares * px
        if gross + notional > max_gross:
            rejected.append(f"{o['ticker']} (bruttoexponeringstak)")
            continue
        if o["direction"] == "long" and notional > p["cash"]:
            rejected.append(f"{o['ticker']} (otillracklig kassa)")
            continue

        c = A.cost(notional, strat)
        p["cash"] -= (notional + c) if sgn == 1 else -(notional - c)
        p["open_positions"].append({
            "signal_date": o["signal_date"], "opened": today,
            "ticker": o["ticker"], "name": o["name"],
            "direction": o["direction"], "horizon": o["horizon"],
            "shares": shares, "entry": round(px, 4),
            "ref_price": o["ref_price"], "entry_gap_pct": round(gap, 2),
            "stop": round(px - sgn * stop_dist, 2),
            "target": round(px + sgn * o["target_dist"], 2),
            "stop_dist": stop_dist, "target_dist": o["target_dist"],
            "max_hold_days": o["max_hold_days"], "score": o["score"],
            "confidence": o["confidence"], "components": o["components"],
            "rationale": o.get("rationale", ""), "entry_cost": round(c, 2),
        })
        groups.add(g)
        gross += notional
        filled.append(f"{o['ticker']} @ {px:.2f} ({shares} st, gap {gap:+.1f}%)")

    A.save_json(A.DATA / "pending.json", {"date": today, "orders": [],
                                          "filled": filled, "rejected": rejected})
    return filled, rejected


# --------------------------------------------------------------------------
# 2. Stang positioner
# --------------------------------------------------------------------------
def sessions_held(opened, today):
    """Antal handelsdagar positionen varit oppen, inklusive fyllningsdagen."""
    return int(np.busday_count(opened, today)) + 1


def close_positions(p, prices, today, strat):
    still_open, closed = [], []
    for pos in p["open_positions"]:
        bar = _bar(prices.get(pos["ticker"]), today)
        if bar is None:
            still_open.append(pos)
            continue

        sgn = 1 if pos["direction"] == "long" else -1
        held = sessions_held(pos["opened"], today)
        exit_px, reason = None, None

        # Konservativt: trafffas bade stop och mal samma dag antas stoppen.
        if (sgn == 1 and bar["low"] <= pos["stop"]) or \
           (sgn == -1 and bar["high"] >= pos["stop"]):
            exit_px, reason = pos["stop"], "stop"
        elif (sgn == 1 and bar["high"] >= pos["target"]) or \
             (sgn == -1 and bar["low"] <= pos["target"]):
            exit_px, reason = pos["target"], "mal"
        elif held >= pos["max_hold_days"]:
            exit_px, reason = float(bar["close"]), "tid"

        if exit_px is None:
            still_open.append(pos)
            continue

        notional = exit_px * pos["shares"]
        c = A.cost(notional, strat)
        gross = sgn * (exit_px - pos["entry"]) * pos["shares"]
        net = gross - c - pos["entry_cost"]
        p["cash"] += (notional - c) if sgn == 1 else -(notional + c)
        closed.append({**{k: pos[k] for k in
                          ("signal_date", "opened", "ticker", "name", "direction",
                           "horizon", "shares", "entry", "score", "confidence",
                           "components")},
                       "closed": today, "exit": round(exit_px, 2), "reason": reason,
                       "pnl_sek": round(net, 2),
                       "pnl_pct": round(sgn * (exit_px / pos["entry"] - 1) * 100, 3),
                       "held_days": held})
    p["open_positions"] = still_open
    p["closed_trades"].extend(closed)
    return closed


# --------------------------------------------------------------------------
# 3. Bevakningsvarden for de positioner som ligger kvar
# --------------------------------------------------------------------------
def annotate_open(p, prices, today):
    for pos in p["open_positions"]:
        bar = _bar(prices.get(pos["ticker"]), today)
        px = float(bar["close"]) if bar is not None else pos["entry"]
        sgn = 1 if pos["direction"] == "long" else -1
        held = sessions_held(pos["opened"], today)

        pos["last_close"] = round(px, 2)
        pos["as_of"] = today
        pos["open_pnl"] = round(sgn * (px - pos["entry"]) * pos["shares"], 2)
        pos["open_pnl_pct"] = round(sgn * (px / pos["entry"] - 1) * 100, 2)
        pos["to_stop_pct"] = round(sgn * (pos["stop"] / px - 1) * 100, 2)
        pos["to_target_pct"] = round(sgn * (pos["target"] / px - 1) * 100, 2)
        pos["days_left"] = max(pos["max_hold_days"] - held, 0)

        span = abs(pos["target"] - pos["stop"]) or 1
        pos["progress"] = round(min(max(abs(px - pos["stop"]) / span, 0), 1), 3)

        if pos["days_left"] == 0:
            pos["action"] = "Stangs vid nasta stangning"
        elif abs(pos["to_stop_pct"]) <= 1.0:
            pos["action"] = "Nara stop"
        elif abs(pos["to_target_pct"]) <= 1.0:
            pos["action"] = "Nara mal"
        else:
            pos["action"] = "Behall"


def mark_equity(p, prices, today):
    mv = 0.0
    for pos in p["open_positions"]:
        bar = _bar(prices.get(pos["ticker"]), today)
        px = float(bar["close"]) if bar is not None else pos["entry"]
        mv += (1 if pos["direction"] == "long" else -1) * px * pos["shares"]
    p["equity"] = round(p["cash"] + mv, 2)
    p["equity_curve"] = [e for e in p["equity_curve"] if e["date"] != today]
    p["equity_curve"].append({"date": today, "equity": p["equity"],
                              "cash": round(p["cash"], 2),
                              "open_positions": len(p["open_positions"])})
    p["equity_curve"].sort(key=lambda e: e["date"])


# --------------------------------------------------------------------------
# 4. Larandet
# --------------------------------------------------------------------------
def day_returns(prices, today):
    """Dagsavkastning per bolag, stangning mot stangning."""
    out = {}
    for t, df in prices.items():
        i = df.index[df["date"].dt.date.astype(str) == today]
        if len(i) == 0 or i[0] == 0:
            continue
        out[t] = float(df.loc[i[0], "adjclose"] / df.loc[i[0] - 1, "adjclose"] - 1)
    return out


def update_weights(strat, today):
    lb = strat["learning"]["ic_lookback_days"]
    files = sorted((A.DATA / "evaluations").glob("*_ic.json"))[-lb:]
    if len(files) < strat["learning"]["min_observations"]:
        return None, f"For fa observationer ({len(files)}) - vikterna ororda."

    ics = {c: [] for c in A.COMPONENTS}
    for f in files:
        d = A.load_json(f, {})
        for c in A.COMPONENTS:
            v = d.get("ic", {}).get(c)
            if v is not None and not pd.isna(v):
                ics[c].append(v)

    mean_ic = {c: (float(np.mean(v)) if v else 0.0) for c, v in ics.items()}
    pos = {c: max(m, 0.0) for c, m in mean_ic.items()}
    tot = sum(pos.values())
    if tot <= 0:
        return mean_ic, "Ingen komponent har positiv IC - vikterna ororda."

    target = {c: pos[c] / tot for c in A.COMPONENTS}
    step = strat["learning"]["max_step_per_day"]
    lo, hi = strat["weight_bounds"]["min"], strat["weight_bounds"]["max"]
    w = dict(strat["weights"])
    for c in A.COMPONENTS:
        w[c] = min(hi, max(lo, w[c] + max(-step, min(step, target[c] - w[c]))))
    s = sum(w.values())
    strat["weights"] = {c: round(w[c] / s, 4) for c in A.COMPONENTS}
    return mean_ic, "Vikter justerade mot uppmatt IC."


# --------------------------------------------------------------------------
def main(today=None):
    today = today or dt.date.today().isoformat()
    strat = A.strategy()
    prices, _ = A.load_all_prices()
    if not prices:
        print("AVBRYTER: ingen prisdata.")
        return 1

    p = A.portfolio()

    filled, rejected = fill_pending(p, prices, today, strat)
    if filled:
        print("Fyllda order: " + "; ".join(filled))
    if rejected:
        print("Ej fyllda: " + "; ".join(rejected))

    closed = close_positions(p, prices, today, strat)
    annotate_open(p, prices, today)
    mark_equity(p, prices, today)
    A.save_portfolio(p)

    # --- utvardering av dagens faktiska affarer ---
    rows = []
    for t in closed:
        sgn = 1 if t["direction"] == "long" else -1
        rows.append({"ticker": t["ticker"], "name": t["name"],
                     "direction": t["direction"], "horizon": t["horizon"],
                     "status": f"stangd ({t['reason']})",
                     "signed_return_pct": t["pnl_pct"], "hit": t["pnl_pct"] > 0})
    for o in p["open_positions"]:
        rows.append({"ticker": o["ticker"], "name": o["name"],
                     "direction": o["direction"], "horizon": o["horizon"],
                     "status": "oppen", "signed_return_pct": o["open_pnl_pct"],
                     "hit": o["open_pnl_pct"] > 0})

    rets = day_returns(prices, today)
    # Benchmarken ligger utanfor universumet och maste laddas separat.
    bench_t = A.load_json(A.CONFIG / "universe.json")["benchmark"]
    bdf = A.load_prices(bench_t)
    br = day_returns({bench_t: bdf}, today).get(bench_t) if bdf is not None else None
    bench = round(br * 100, 3) if br is not None else None

    # --- IC: forutsade morgonens poang dagens avkastning? ---
    ic = {}
    sf = sorted((A.DATA / "signals").glob("*_scores.csv"))
    if sf:
        sc = pd.read_csv(sf[-1], index_col=0)
        common = [t for t in sc.index if t in rets]
        if len(common) >= 15:
            rr = pd.Series({t: rets[t] for t in common}).rank()
            for c in A.COMPONENTS + ["score"]:
                ic[c] = float(sc.loc[common, c].rank().corr(rr))
        A.save_json(A.DATA / "evaluations" / f"{today}_ic.json",
                    {"date": today, "signal_file": sf[-1].name,
                     "n": len(common), "ic": ic})

    daily = {
        "date": today, "n_positions": len(rows),
        "hit_rate": round(float(np.mean([r["hit"] for r in rows])), 3) if rows else None,
        "avg_signed_return_pct": round(float(np.mean([r["signed_return_pct"] for r in rows])), 3) if rows else None,
        "benchmark_return_pct": bench, "equity": p["equity"],
        "filled": filled, "rejected": rejected,
        "closed_trades": closed, "detail": rows, "ic": ic,
    }
    A.save_json(A.DATA / "evaluations" / f"{today}.json", daily)

    mean_ic, note = update_weights(strat, today)
    A.save_strategy(strat)
    A.save_json(A.DATA / "evaluations" / f"{today}_weights.json",
                {"date": today, "mean_ic": mean_ic, "note": note,
                 "weights_after": strat["weights"]})

    print(f"Traffsakerhet {daily['hit_rate']}, snitt "
          f"{daily['avg_signed_return_pct']}%, index {bench}%, "
          f"equity {p['equity']:,.0f} SEK")
    print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
