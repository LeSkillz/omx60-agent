"""
Kvallskorning: stang positioner, utvardera dagens rad, justera vikterna.

Forutsatter att agenten har uppdaterat data/prices/ med dagens stangningskurser.

Kor:  python3 scripts/run_evening.py [YYYY-MM-DD]
"""
import sys
import datetime as dt

import numpy as np
import pandas as pd

import omxagent as A


def _bar(df, date):
    m = df[df["date"].dt.date.astype(str) == date]
    return None if m.empty else m.iloc[-1]


def close_positions(p, prices, today, strat):
    """Stanger positioner pa stop, target eller max innehavstid. Returnerar avslut."""
    still_open, closed = [], []
    for pos in p["open_positions"]:
        df = prices.get(pos["ticker"])
        bar = _bar(df, today) if df is not None else None
        if bar is None:
            still_open.append(pos)
            continue

        sgn = 1 if pos["direction"] == "long" else -1
        held = np.busday_count(pos["opened"], today)
        exit_px, reason = None, None
        # konservativt: om bade stop och target trafffas samma dag, anta stop
        if (sgn == 1 and bar["low"] <= pos["stop"]) or \
           (sgn == -1 and bar["high"] >= pos["stop"]):
            exit_px, reason = pos["stop"], "stop"
        elif (sgn == 1 and bar["high"] >= pos["target"]) or \
             (sgn == -1 and bar["low"] <= pos["target"]):
            exit_px, reason = pos["target"], "target"
        elif held >= pos["max_hold_days"]:
            exit_px, reason = float(bar["close"]), "tid"

        if exit_px is None:
            pos["last_close"] = float(bar["close"])
            pos["open_pnl"] = round(sgn * (bar["close"] - pos["entry"]) * pos["shares"], 2)
            still_open.append(pos)
            continue

        notional = exit_px * pos["shares"]
        c = A.cost(notional, strat)
        gross = sgn * (exit_px - pos["entry"]) * pos["shares"]
        net = gross - c - pos["entry_cost"]
        p["cash"] += (notional - c) if sgn == 1 else -(notional + c)
        closed.append({**{k: pos[k] for k in
                          ("opened", "ticker", "name", "direction", "horizon",
                           "shares", "entry", "score", "confidence", "components")},
                       "closed": today, "exit": round(exit_px, 2), "reason": reason,
                       "pnl_sek": round(net, 2),
                       "pnl_pct": round(sgn * (exit_px / pos["entry"] - 1) * 100, 3),
                       "held_days": int(held)})
    p["open_positions"] = still_open
    p["closed_trades"].extend(closed)
    return closed


def mark_equity(p, prices, today):
    mv = 0.0
    for pos in p["open_positions"]:
        df = prices.get(pos["ticker"])
        bar = _bar(df, today) if df is not None else None
        px = float(bar["close"]) if bar is not None else pos["entry"]
        sgn = 1 if pos["direction"] == "long" else -1
        mv += sgn * px * pos["shares"]
    p["equity"] = round(p["cash"] + mv, 2)
    p["equity_curve"] = [e for e in p["equity_curve"] if e["date"] != today]
    p["equity_curve"].append({"date": today, "equity": p["equity"],
                              "cash": round(p["cash"], 2),
                              "open_positions": len(p["open_positions"])})
    p["equity_curve"].sort(key=lambda e: e["date"])


def next_day_returns(prices, today):
    """Faktisk dagsavkastning per bolag idag - facit for gardagens signaler."""
    out = {}
    for t, df in prices.items():
        bar = _bar(df, today)
        if bar is None:
            continue
        i = df.index[df["date"].dt.date.astype(str) == today]
        if len(i) == 0 or i[0] == 0:
            continue
        prev = df.loc[i[0] - 1, "adjclose"]
        out[t] = float(bar["adjclose"] / prev - 1)
    return out


def update_weights(strat, today):
    """
    Larandet. For varje delkomponent mats information coefficient (IC) =
    rangkorrelationen mellan komponentens varde och nasta dags faktiska
    avkastning, over de senaste N dagarna. Vikter flyttas sma steg mot de
    komponenter som faktiskt har forutsagt nagot.
    """
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
        delta = max(-step, min(step, target[c] - w[c]))
        w[c] = min(hi, max(lo, w[c] + delta))
    s = sum(w.values())
    strat["weights"] = {c: round(w[c] / s, 4) for c in A.COMPONENTS}
    return mean_ic, "Vikter justerade mot uppmatt IC."


def main(today=None):
    today = today or dt.date.today().isoformat()
    strat = A.strategy()
    prices, _ = A.load_all_prices()
    if not prices:
        print("AVBRYTER: ingen prisdata.")
        return 1

    # --- 1. IC pa gardagens (eller senaste) signaldag mot dagens utfall ---
    scored_files = sorted((A.DATA / "signals").glob("*_scores.csv"))
    ic = {}
    if scored_files:
        sc = pd.read_csv(scored_files[-1], index_col=0)
        rets = next_day_returns(prices, today)
        common = [t for t in sc.index if t in rets]
        if len(common) >= 15:
            r = pd.Series({t: rets[t] for t in common})
            rr = r.rank()
            for c in A.COMPONENTS + ["score"]:
                # Spearman = Pearson pa rangerna (undviker scipy-beroende)
                ic[c] = float(sc.loc[common, c].rank().corr(rr))
        A.save_json(A.DATA / "evaluations" / f"{today}_ic.json",
                    {"date": today, "signal_file": scored_files[-1].name,
                     "n": len(common), "ic": ic})

    # --- 2. stang positioner och markera till marknad ---
    p = A.portfolio()
    closed = close_positions(p, prices, today, strat)
    mark_equity(p, prices, today)
    A.save_portfolio(p)

    # --- 3. utvardera dagens rad ---
    sig = A.load_json(A.DATA / "signals" / f"{today}.json", {})
    rets = next_day_returns(prices, today)
    rows = []
    for s in sig.get("day_trades", []) + sig.get("week_trades", []):
        r = rets.get(s["ticker"])
        if r is None:
            continue
        sgn = 1 if s["direction"] == "long" else -1
        rows.append({"ticker": s["ticker"], "name": s["name"],
                     "direction": s["direction"], "horizon": s["horizon"],
                     "score": s["score"], "confidence": s["confidence"],
                     "day_return_pct": round(r * 100, 3),
                     "signed_return_pct": round(sgn * r * 100, 3),
                     "hit": bool(sgn * r > 0)})
    bench = None
    bdf = A.load_prices(A.load_json(A.CONFIG / "universe.json")["benchmark"])
    if bdf is not None:
        br = next_day_returns({"bench": bdf}, today).get("bench")
        bench = round(br * 100, 3) if br is not None else None

    daily = {
        "date": today,
        "n_signals": len(rows),
        "hit_rate": round(float(np.mean([r["hit"] for r in rows])), 3) if rows else None,
        "avg_signed_return_pct": round(float(np.mean([r["signed_return_pct"] for r in rows])), 3) if rows else None,
        "benchmark_return_pct": bench,
        "equity": p["equity"],
        "closed_trades": closed,
        "detail": rows,
        "ic": ic,
    }
    A.save_json(A.DATA / "evaluations" / f"{today}.json", daily)

    # --- 4. larandet ---
    mean_ic, note = update_weights(strat, today)
    A.save_strategy(strat)
    A.save_json(A.DATA / "evaluations" / f"{today}_weights.json",
                {"date": today, "mean_ic": mean_ic, "note": note,
                 "weights_after": strat["weights"]})

    print(f"Traffsakerhet {daily['hit_rate']}, snitt {daily['avg_signed_return_pct']}%, "
          f"index {bench}%, equity {p['equity']:,.0f} SEK")
    print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
