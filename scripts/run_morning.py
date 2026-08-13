"""
Morgonkorning: rakna fram dagens och veckans signaler och lagg dem som
ORDER ATT UTFORAS VID DAGENS OPPNING.

Positioner oppnas inte har. Morgonkorningen vet inte vad oppningskursen blir,
och att bokfora kopet till gardagens stangning vore att ge modellen varje natts
kursrorelse gratis. Kvallskorningen fyller ordrarna till dagens faktiska
oppningskurs.

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
    Ett bolag far bara en order, oavsett aktieslag och horisont.
    Starkast overtygelse vinner.
    """
    combined = [("day", s) for s in day_sig] + [("week", s) for s in week_sig]
    combined.sort(key=lambda x: -abs(x[1]["score"]))

    seen, kept, dropped = set(), [], []
    for horizon, s in combined:
        g = company_group(s["ticker"])
        if g in seen:
            dropped.append(f"{s['ticker']} ({horizon}, samma bolag som "
                           f"redan vald order)")
            continue
        seen.add(g)
        kept.append((horizon, s))

    return ([s for h, s in kept if h == "day"],
            [s for h, s in kept if h == "week"],
            dropped)


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
    print(f"Features beraknade for {len(feats)} bolag.")

    qual_raw = A.load_json(A.DATA / "qualitative" / f"{today}.json", {})
    qual = qual_raw.get("tickers", {})
    macro = qual_raw.get("macro", {})
    if not qual:
        print("Varning: ingen kvalitativ data for idag. Kor pa enbart teknik "
              "- fundamental/sentiment/macro blir 0 och konfidensen sanks.")

    base = A.build_scores(feats, qual, macro, strat, "base")
    base.to_csv(A.DATA / "signals" / f"{today}_scores.csv")

    day_sig = A.make_signals(A.build_scores(feats, qual, macro, strat, "day"),
                             strat, "day")
    week_sig = A.make_signals(A.build_scores(feats, qual, macro, strat, "week"),
                              strat, "week")
    day_sig, week_sig, dropped = dedupe(day_sig, week_sig)
    if dropped:
        print(f"Bortsorterat pga dubbel bolagsexponering: {'; '.join(dropped)}")

    # ---- gor om signalerna till order ----
    # Stopp- och malavstand ar ATR-baserade och foljer med som avstand, inte som
    # nivaer. Kvallskorningen lagger dem runt den faktiska oppningskursen.
    orders = []
    for s in day_sig + week_sig:
        atr = max(feats[s["ticker"]]["atr14"], 0.01)
        orders.append({
            **s,
            "signal_date": today,
            "atr14": round(atr, 4),
            "stop_dist": round(strat["risk"]["atr_stop_mult"] * atr, 4),
            "target_dist": round(strat["risk"]["atr_target_mult"] * atr, 4),
            "order_type": "marknad vid oppning",
            "indicative_shares": s["shares"],
        })
    A.save_json(A.DATA / "pending.json", {"date": today, "orders": orders})

    payload = {
        "date": today,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "weights_used": strat["weights"],
        "coverage": {"priced": len(feats), "qualitative": len(qual),
                     "missing": missing, "deduped": dropped},
        "macro": macro,
        "day_trades": day_sig,
        "week_trades": week_sig,
        "pending_orders": orders,
        "top10": base.head(10)[["name", "score", "confidence"]]
                     .round(3).reset_index().to_dict("records"),
        "bottom10": base.tail(10)[["name", "score", "confidence"]]
                        .round(3).reset_index().to_dict("records"),
    }
    A.save_json(A.DATA / "signals" / f"{today}.json", payload)

    print(f"{len(day_sig)} dagsorder, {len(week_sig)} veckoorder lagda for "
          f"utforande vid dagens oppning.")
    print("Positioner bokfors i kvallskorningen till faktisk oppningskurs.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
