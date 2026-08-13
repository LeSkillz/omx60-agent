"""
Morgonkorning: rakna fram dagens och veckans signaler, oppna pappersositioner.

Forutsatter att agenten redan har:
  1. skrivit uppdaterad prisdata till data/prices/ (via ingest_prices)
  2. skrivit data/qualitative/<datum>.json med sentiment/fundamenta/omvarld

Kor:  python3 scripts/run_morning.py [YYYY-MM-DD]
"""
import sys
import datetime as dt

import omxagent as A


def main(today=None):
    today = today or dt.date.today().isoformat()
    strat = A.strategy()

    prices, missing = A.load_all_prices()
    if len(prices) < 20:
        print(f"AVBRYTER: bara {len(prices)} bolag har prisdata. "
              f"Kor datainhamtningen forst.")
        return 1
    if missing:
        print(f"Varning: saknar/for kort historik for {len(missing)} bolag: "
              f"{', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")

    feats = {}
    for t, df in prices.items():
        f = A.features(df)
        if f:
            feats[t] = f
    print(f"Features berdknade for {len(feats)} bolag.")

    qual_file = A.DATA / "qualitative" / f"{today}.json"
    qual_raw = A.load_json(qual_file, {})
    qual = qual_raw.get("tickers", {})
    macro = qual_raw.get("macro", {})
    if not qual:
        print("Varning: ingen kvalitativ data for idag. Kor pa enbart teknik "
              "- fundamental/sentiment/macro blir 0 och konfidensen sanks.")

    base = A.build_scores(feats, qual, macro, strat, "base")
    base.to_csv(A.DATA / "signals" / f"{today}_scores.csv")

    scored_day = A.build_scores(feats, qual, macro, strat, "day")
    scored_week = A.build_scores(feats, qual, macro, strat, "week")
    scored = base

    day_sig = A.make_signals(scored_day, strat, "day")
    week_sig = A.make_signals(scored_week, strat, "week")

    # veckosignal som dubblerar en dagssignal pa samma bolag+riktning tas bort
    seen = {(s["ticker"], s["direction"]) for s in day_sig}
    week_sig = [s for s in week_sig if (s["ticker"], s["direction"]) not in seen]

    payload = {
        "date": today,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "weights_used": strat["weights"],
        "coverage": {"priced": len(feats), "qualitative": len(qual),
                     "missing": missing},
        "macro": macro,
        "day_trades": day_sig,
        "week_trades": week_sig,
        "top10": scored.head(10)[["name", "score", "confidence"]]
                       .round(3).reset_index().to_dict("records"),
        "bottom10": scored.tail(10)[["name", "score", "confidence"]]
                          .round(3).reset_index().to_dict("records"),
    }
    A.save_json(A.DATA / "signals" / f"{today}.json", payload)

    # oppna pappersositioner
    p = A.portfolio()
    open_keys = {(x["ticker"], x["horizon"]) for x in p["open_positions"]}
    cap = strat["risk"]["max_positions"]
    opened = 0
    for s in day_sig + week_sig:
        if len(p["open_positions"]) >= cap:
            break
        if (s["ticker"], s["horizon"]) in open_keys:
            continue
        notional = s["shares"] * s["ref_price"]
        if notional > p["cash"] and s["direction"] == "long":
            continue
        c = A.cost(notional, strat)
        p["cash"] -= (notional + c) if s["direction"] == "long" else -(notional - c)
        p["open_positions"].append({
            "opened": today, "ticker": s["ticker"], "name": s["name"],
            "direction": s["direction"], "horizon": s["horizon"],
            "shares": s["shares"], "entry": s["ref_price"],
            "stop": s["stop"], "target": s["target"],
            "max_hold_days": s["max_hold_days"], "score": s["score"],
            "confidence": s["confidence"], "components": s["components"],
            "entry_cost": round(c, 2),
        })
        opened += 1

    A.save_portfolio(p)
    print(f"{len(day_sig)} dagssignaler, {len(week_sig)} veckosignaler, "
          f"{opened} nya pappersositioner.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
