"""
Morgonkorning: rakna fram dagens och veckans signaler, oppna pappersositioner.

Forutsatter att agenten redan har:
  1. skrivit uppdaterad prisdata till data/prices/ (fetch_data.py)
  2. skrivit data/qualitative/<datum>.json (research.py)

Kor:  python3 run_morning.py [YYYY-MM-DD]
"""
import sys
import datetime as dt

import omxagent as A


def company_group(ticker):
    """
    Grupperar aktieslag som hor till samma bolag.
    ATCO-A.ST och ATCO-B.ST -> ATCO. EPI-A.ST och EPI-B.ST -> EPI.
    VOLV-B.ST -> VOLV medan VOLCAR-B.ST -> VOLCAR, alltsa skilda bolag.
    """
    return ticker.split(".")[0].split("-")[0]


def dedupe(day_sig, week_sig):
    """
    Ett bolag far bara en position, oavsett aktieslag och horisont.
    Utan detta kan modellen ta ATCO-A och ATCO-B samtidigt och tro att det
    ar tva oberoende positioner nar det i praktiken ar dubbel exponering
    mot samma bolag.

    Starkast overtygelse vinner. Returnerar (dagssignaler, veckosignaler,
    bortsorterade).
    """
    combined = [("day", s) for s in day_sig] + [("week", s) for s in week_sig]
    combined.sort(key=lambda x: -abs(x[1]["score"]))

    seen, kept, dropped = set(), [], []
    for horizon, s in combined:
        g = company_group(s["ticker"])
        if g in seen:
            dropped.append(f"{s['ticker']} ({horizon}, samma bolag som "
                           f"redan vald position)")
            continue
        seen.add(g)
        kept.append((horizon, s))

    return ([s for h, s in kept if h == "day"],
            [s for h, s in kept if h == "week"],
            dropped)


def main(today=None):
    today = today or dt.date.today().isoformat()
    strat = A.strategy()
    risk = strat["risk"]

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
    print(f"Features beraknade for {len(feats)} bolag.")

    qual_raw = A.load_json(A.DATA / "qualitative" / f"{today}.json", {})
    qual = qual_raw.get("tickers", {})
    macro = qual_raw.get("macro", {})
    if not qual:
        print("Varning: ingen kvalitativ data for idag. Kor pa enbart teknik "
              "- fundamental/sentiment/macro blir 0 och konfidensen sanks.")

    base = A.build_scores(feats, qual, macro, strat, "base")
    base.to_csv(A.DATA / "signals" / f"{today}_scores.csv")
    scored = base

    day_sig = A.make_signals(A.build_scores(feats, qual, macro, strat, "day"),
                             strat, "day")
    week_sig = A.make_signals(A.build_scores(feats, qual, macro, strat, "week"),
                              strat, "week")
    day_sig, week_sig, dropped = dedupe(day_sig, week_sig)
    if dropped:
        print(f"Bortsorterat pga dubbel bolagsexponering: {'; '.join(dropped)}")

    payload = {
        "date": today,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "weights_used": strat["weights"],
        "coverage": {"priced": len(feats), "qualitative": len(qual),
                     "missing": missing, "deduped": dropped},
        "macro": macro,
        "day_trades": day_sig,
        "week_trades": week_sig,
        "top10": scored.head(10)[["name", "score", "confidence"]]
                       .round(3).reset_index().to_dict("records"),
        "bottom10": scored.tail(10)[["name", "score", "confidence"]]
                          .round(3).reset_index().to_dict("records"),
    }
    A.save_json(A.DATA / "signals" / f"{today}.json", payload)

    # ---- oppna pappersositioner ----
    p = A.portfolio()
    equity = p["equity"] or risk["start_capital_sek"]
    open_groups = {company_group(x["ticker"]) for x in p["open_positions"]}
    gross = sum(abs(x["shares"] * x["entry"]) for x in p["open_positions"])
    max_gross = equity * risk.get("max_gross_exposure_pct", 100) / 100.0

    cap, opened, skipped = risk["max_positions"], 0, []
    for s in day_sig + week_sig:
        if len(p["open_positions"]) >= cap:
            skipped.append(f"{s['ticker']} (maxantal positioner)")
            continue
        g = company_group(s["ticker"])
        if g in open_groups:
            skipped.append(f"{s['ticker']} (redan exponering mot bolaget)")
            continue

        notional = s["shares"] * s["ref_price"]
        if gross + notional > max_gross:
            skipped.append(f"{s['ticker']} (bruttoexponeringstak)")
            continue
        if s["direction"] == "long" and notional > p["cash"]:
            skipped.append(f"{s['ticker']} (otillracklig kassa)")
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
        open_groups.add(g)
        gross += notional
        opened += 1

    A.save_portfolio(p)
    print(f"{len(day_sig)} dagssignaler, {len(week_sig)} veckosignaler, "
          f"{opened} nya pappersositioner.")
    print(f"Bruttoexponering {gross/equity*100:.0f}% av kapitalet.")
    if skipped:
        print("Ej oppnade: " + "; ".join(skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
