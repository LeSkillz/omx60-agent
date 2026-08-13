"""
OMX60-agenten - gemensamt bibliotek.

Ingen extern beroendekedja utover requests/pandas/numpy. Hamtar priser fran
Yahoo Finance chart-API med Stooq som reserv. All kvalitativ data (sentiment,
analytiker, omvarld) skrivs av agenten sjalv till data/qualitative/<datum>.json
enligt schemat langst ned i denna fil.

Kor alltid fran ClaudeStock-roten.
"""

from __future__ import annotations

import json
import math
import os
import time
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
MEMORY = ROOT / "memory"
REPORT = ROOT / "report"

for d in (DATA / "prices", DATA / "signals", DATA / "qualitative",
          DATA / "evaluations", MEMORY, REPORT):
    d.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# --------------------------------------------------------------------------
# IO-hjalpare
# --------------------------------------------------------------------------
def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, p)


def strategy():
    return load_json(CONFIG / "strategy.json")


def save_strategy(s):
    s["last_updated"] = dt.date.today().isoformat()
    save_json(CONFIG / "strategy.json", s)


def universe():
    """Aktiv lista om den finns, annars hela kandidatlistan."""
    active = load_json(CONFIG / "universe_active.json")
    if active:
        return active["members"]
    return load_json(CONFIG / "universe.json")["candidates"]


# --------------------------------------------------------------------------
# Prisdata
# --------------------------------------------------------------------------
def _yahoo_chart(ticker, range_="2y", interval="1d"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    r = requests.get(url, params={"range": range_, "interval": interval},
                     headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"]
    if not res:
        raise ValueError("tomt svar")
    res = res[0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "date": pd.to_datetime(res["timestamp"], unit="s").normalize(),
        "open": q["open"], "high": q["high"], "low": q["low"],
        "close": q["close"], "volume": q["volume"],
    })
    adj = res["indicators"].get("adjclose")
    df["adjclose"] = adj[0]["adjclose"] if adj else df["close"]
    return df.dropna(subset=["close"]).reset_index(drop=True)


def _stooq_chart(ticker):
    sym = ticker.lower().replace(".st", ".st")
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    if "Date" not in r.text[:50]:
        raise ValueError("stooq gav ingen data")
    from io import StringIO
    df = pd.read_csv(StringIO(r.text))
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    df["adjclose"] = df["close"]
    return df.reset_index(drop=True)


def fetch_prices(ticker, force=False):
    """Hamtar och cachar dagsdata. Returnerar DataFrame eller None."""
    cache = DATA / "prices" / f"{ticker.replace('.', '_')}.csv"
    if cache.exists() and not force:
        df = pd.read_csv(cache, parse_dates=["date"])
        last = df["date"].max().date()
        if last >= _prev_business_day():
            return df
    for fn in (lambda: _yahoo_chart(ticker), lambda: _stooq_chart(ticker)):
        try:
            df = fn()
            if len(df) > 120:
                df.to_csv(cache, index=False)
                return df
        except Exception:
            time.sleep(0.6)
    if cache.exists():
        return pd.read_csv(cache, parse_dates=["date"])
    return None


def ingest_prices(ticker, rows):
    """
    Primar vag in i systemet. Agenten hamtar data via en MCP-koppling
    (Alpha Vantage / FMP / liknande) och skriver hit.
    rows: lista av dictar med nycklarna date, open, high, low, close, volume
          (adjclose valfri). Slas ihop med befintlig cache, dubletter tas bort.
    """
    cache = DATA / "prices" / f"{ticker.replace('.', '_')}.csv"
    new = pd.DataFrame(rows)
    new["date"] = pd.to_datetime(new["date"])
    if "adjclose" not in new:
        new["adjclose"] = new["close"]
    if cache.exists():
        old = pd.read_csv(cache, parse_dates=["date"])
        new = pd.concat([old, new], ignore_index=True)
    new = (new.drop_duplicates(subset=["date"], keep="last")
              .sort_values("date").reset_index(drop=True))
    new.to_csv(cache, index=False)
    return len(new)


def load_prices(ticker):
    cache = DATA / "prices" / f"{ticker.replace('.', '_')}.csv"
    if not cache.exists():
        return None
    return pd.read_csv(cache, parse_dates=["date"])


def load_all_prices(min_rows=130):
    out, missing = {}, []
    for m in universe():
        df = load_prices(m["ticker"])
        if df is None or len(df) < min_rows:
            missing.append(m["ticker"])
        else:
            out[m["ticker"]] = df
    return out, missing


def _prev_business_day(d=None):
    d = d or dt.date.today()
    d -= dt.timedelta(days=1)
    while d.weekday() > 4:
        d -= dt.timedelta(days=1)
    return d


def refresh_all(force=False, sleep=0.35):
    out, failed = {}, []
    for m in universe():
        df = fetch_prices(m["ticker"], force=force)
        if df is None or len(df) < 120:
            failed.append(m["ticker"])
        else:
            out[m["ticker"]] = df
        time.sleep(sleep)
    return out, failed


# --------------------------------------------------------------------------
# Tekniska features
# --------------------------------------------------------------------------
def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def features(df):
    """Rafeatures for en aktie, senaste raden."""
    c = df["adjclose"].astype(float)
    if len(c) < 130:
        return None
    f = {}
    f["close"] = float(df["close"].iloc[-1])
    f["ret_1d"] = float(c.pct_change().iloc[-1])
    f["ret_5d"] = float(c.iloc[-1] / c.iloc[-6] - 1)
    f["ret_21d"] = float(c.iloc[-1] / c.iloc[-22] - 1)
    f["ret_63d"] = float(c.iloc[-1] / c.iloc[-64] - 1)
    if len(c) >= 253:
        f["mom_12_1"] = float(c.iloc[-22] / c.iloc[-253] - 1)
        f["dist_52w_high"] = float(c.iloc[-1] / c.iloc[-253:].max() - 1)
    else:
        f["mom_12_1"] = float(c.iloc[-22] / c.iloc[0] - 1)
        f["dist_52w_high"] = float(c.iloc[-1] / c.max() - 1)
    ma20, ma50, ma200 = c.rolling(20).mean(), c.rolling(50).mean(), c.rolling(200).mean()
    f["ma_state"] = (
        (1 if c.iloc[-1] > ma20.iloc[-1] else -1)
        + (1 if c.iloc[-1] > ma50.iloc[-1] else -1)
        + (1 if len(c) >= 200 and c.iloc[-1] > ma200.iloc[-1] else -1)
    ) / 3.0
    f["rsi14"] = float(_rsi(c).iloc[-1])
    f["vol20d_ann"] = float(c.pct_change().rolling(20).std().iloc[-1] * math.sqrt(252))
    atr = _atr(df)
    f["atr14"] = float(atr.iloc[-1])
    f["atr_pct"] = float(atr.iloc[-1] / df["close"].iloc[-1])
    v = df["volume"].astype(float)
    f["vol_surge"] = float(v.iloc[-5:].mean() / max(v.iloc[-60:].mean(), 1e-9))
    f["turnover_sek"] = float((v.iloc[-20:] * df["close"].iloc[-20:]).mean())
    return f


def zscore(series, clip=2.5):
    s = pd.Series(series, dtype=float)
    mu, sd = s.mean(), s.std(ddof=0)
    if not sd or math.isnan(sd):
        return pd.Series(np.zeros(len(s)), index=s.index)
    return ((s - mu) / sd).clip(-clip, clip) / clip


# --------------------------------------------------------------------------
# Poangsattning
# --------------------------------------------------------------------------
COMPONENTS = ["momentum", "trend", "reversal", "volume",
              "fundamental", "sentiment", "macro"]


# Horisontspecifika multiplikatorer. Dagshandel lutar mot kortsiktig
# mean reversion, volym och sentiment. Veckohandel mot momentum, trend
# och fundamenta. Basvikterna (som larandet justerar) skalas med dessa.
HORIZON_TILT = {
    "day":  {"momentum": 0.6, "trend": 0.8, "reversal": 1.7, "volume": 1.6,
             "fundamental": 0.5, "sentiment": 1.3, "macro": 0.8},
    "week": {"momentum": 1.4, "trend": 1.3, "reversal": 0.3, "volume": 0.7,
             "fundamental": 1.5, "sentiment": 0.9, "macro": 1.2},
    "base": {c: 1.0 for c in COMPONENTS},
}


def horizon_weights(strat, horizon="base"):
    tilt = HORIZON_TILT.get(horizon, HORIZON_TILT["base"])
    w = {c: strat["weights"][c] * tilt[c] for c in COMPONENTS}
    s = sum(w.values())
    return {c: w[c] / s for c in COMPONENTS}


def build_scores(feats: dict, qual: dict, macro_tilt: dict, strat: dict,
                 horizon: str = "base"):
    """
    feats: {ticker: featuredict}
    qual:  {ticker: {"fundamental": -2..2, "sentiment": -2..2, "notes": str, "sources": [...]}}
    macro_tilt: {"sector_scores": {sektor: -2..2}, "market": -2..2, "notes": str}
    Returnerar DataFrame med delpoang + totalscore.
    """
    tickers = list(feats.keys())
    df = pd.DataFrame(index=tickers)
    sec = {m["ticker"]: m.get("sector", "Ovrigt") for m in universe()}
    name = {m["ticker"]: m.get("name", m["ticker"]) for m in universe()}

    df["name"] = [name.get(t, t) for t in tickers]
    df["sector"] = [sec.get(t, "Ovrigt") for t in tickers]
    for k in ("close", "atr14", "atr_pct", "vol20d_ann", "rsi14",
              "turnover_sek", "ret_1d", "ret_5d", "ret_21d"):
        df[k] = [feats[t][k] for t in tickers]

    df["momentum"] = (0.6 * zscore([feats[t]["mom_12_1"] for t in tickers])
                      + 0.4 * zscore([feats[t]["ret_63d"] for t in tickers])).values
    df["trend"] = (0.6 * pd.Series([feats[t]["ma_state"] for t in tickers]).values
                   + 0.4 * zscore([feats[t]["dist_52w_high"] for t in tickers]).values)
    # kortsiktig kontrarian: svag vecka + ej overkopt => positivt
    df["reversal"] = (-zscore([feats[t]["ret_5d"] for t in tickers]).values * 0.6
                      - zscore([feats[t]["rsi14"] - 50 for t in tickers]).values * 0.4)
    df["volume"] = zscore([math.log(max(feats[t]["vol_surge"], 1e-6)) for t in tickers]).values

    df["fundamental"] = [float(qual.get(t, {}).get("fundamental", 0)) / 2.0 for t in tickers]
    df["sentiment"] = [float(qual.get(t, {}).get("sentiment", 0)) / 2.0 for t in tickers]
    ss = (macro_tilt or {}).get("sector_scores", {})
    mkt = float((macro_tilt or {}).get("market", 0)) / 2.0
    df["macro"] = [0.7 * float(ss.get(sec.get(t, "Ovrigt"), 0)) / 2.0 + 0.3 * mkt
                   for t in tickers]

    w = horizon_weights(strat, horizon)
    df["score"] = sum(df[c] * w[c] for c in COMPONENTS)
    # konfidens: hur eniga delkomponenterna ar + hur mycket kvalitativ tackning
    comp = df[COMPONENTS]
    agree = (np.sign(comp) == np.sign(df["score"]).values[:, None]).mean(axis=1)
    cov = [1.0 if t in qual else 0.55 for t in tickers]
    df["confidence"] = (0.65 * agree + 0.35 * np.array(cov)).round(3)
    df["qual_notes"] = [qual.get(t, {}).get("notes", "") for t in tickers]
    return df.sort_values("score", ascending=False)


# --------------------------------------------------------------------------
# Signaler + pappersportfolj
# --------------------------------------------------------------------------
def make_signals(scored: pd.DataFrame, strat: dict, horizon: str):
    th, risk = strat["signal_thresholds"], strat["risk"]
    equity = portfolio()["equity"]
    out = []
    cand = scored[scored["confidence"] >= th["min_confidence"]]
    longs = cand[cand["score"] >= th["long_min_score"]].head(risk["max_positions"])
    shorts = (cand[cand["score"] <= th["short_max_score"]]
              .sort_values("score").head(risk["max_positions"] // 2)
              if risk["allow_shorts"] else cand.iloc[0:0])

    for direction, rows in (("long", longs), ("short", shorts)):
        for t, r in rows.iterrows():
            atr = max(r["atr14"], 0.01)
            stop_dist = risk["atr_stop_mult"] * atr
            risk_sek = equity * risk["risk_per_trade_pct"] / 100.0
            shares = int(max(risk_sek / stop_dist, 0))
            cap = int(equity * risk["max_weight_per_position"] / r["close"])
            shares = max(min(shares, cap), 0)
            if shares == 0:
                continue
            sgn = 1 if direction == "long" else -1
            out.append({
                "ticker": t, "name": r["name"], "sector": r["sector"],
                "direction": direction, "horizon": horizon,
                "score": round(float(r["score"]), 3),
                "confidence": float(r["confidence"]),
                "components": {c: round(float(r[c]), 3) for c in COMPONENTS},
                "ref_price": round(float(r["close"]), 2),
                "shares": shares,
                "stop": round(float(r["close"] - sgn * stop_dist), 2),
                "target": round(float(r["close"] + sgn * risk["atr_target_mult"] * atr), 2),
                "max_hold_days": (risk["max_hold_days_day_trade"] if horizon == "day"
                                  else risk["max_hold_days_week_trade"]),
                "rationale": r["qual_notes"],
            })
    return out


def portfolio():
    return load_json(DATA / "portfolio.json", {
        "equity": strategy()["risk"]["start_capital_sek"],
        "cash": strategy()["risk"]["start_capital_sek"],
        "open_positions": [], "closed_trades": [], "equity_curve": [],
    })


def save_portfolio(p):
    save_json(DATA / "portfolio.json", p)


def cost(notional, strat):
    return notional * strat["risk"]["cost_bps_per_side"] / 10000.0


# --------------------------------------------------------------------------
# Schema for den kvalitativa filen agenten skriver varje morgon
# --------------------------------------------------------------------------
QUALITATIVE_SCHEMA = {
    "date": "YYYY-MM-DD",
    "macro": {
        "market": "-2..2 (helhetssyn Stockholmsborsen idag)",
        "sector_scores": {"Industri": "-2..2", "Finans": "-2..2"},
        "notes": "kort text",
        "sources": ["url"],
    },
    "tickers": {
        "ERIC-B.ST": {
            "fundamental": "-2..2 (vardering, vinsttrend, estimatrevideringar, analytikerriktkurser)",
            "sentiment": "-2..2 (forum, sociala medier, nyhetsflode)",
            "notes": "1-3 meningar om varfor",
            "sources": ["url"],
            "events": ["rapport 2026-08-20", "kapitalmarknadsdag"],
        }
    },
}

if __name__ == "__main__":
    print("Universum:", len(universe()), "bolag")
    print("Vikter:", strategy()["weights"])
