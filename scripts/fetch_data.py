"""
Hamtar OHLCV for hela universumet. Kors i GitHub Actions, som har full
internetatkomst.

Forsta korningen hamtar 2 ars historik, darefter racker 10 dagar.
Tickers som inte gar att hamta filtreras bort och skrivs till
config/universe_active.json.

Kor:  python3 scripts/fetch_data.py [period]
"""
import sys
import time

import pandas as pd
import yfinance as yf

import omxagent as A


def normalize(df):
    """yfinance ger MultiIndex-kolumner ibland. Platta ut till gemensam form."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower).reset_index()
    date_col = "date" if "date" in df.columns else df.columns[0]
    df = df.rename(columns={date_col: "date"})
    if "adj close" in df.columns:
        df = df.rename(columns={"adj close": "adjclose"})
    keep = [c for c in ("date", "open", "high", "low", "close", "adjclose", "volume")
            if c in df.columns]
    df = df[keep].dropna(subset=["close"])
    if "adjclose" not in df.columns:
        df["adjclose"] = df["close"]
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    return df


def main(period=None):
    members = A.load_json(A.CONFIG / "universe.json")["candidates"]
    bench = A.load_json(A.CONFIG / "universe.json")["benchmark"]
    tickers = [m["ticker"] for m in members]

    # om vi redan har djup historik racker en kort hamtning
    have = list((A.DATA / "prices").glob("*.csv"))
    period = period or ("2y" if len(have) < len(tickers) * 0.8 else "10d")
    print(f"Hamtar {period} for {len(tickers)} bolag + benchmark {bench}")

    ok, failed = [], []
    batch = 15
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        try:
            raw = yf.download(chunk, period=period, interval="1d",
                              group_by="ticker", auto_adjust=False,
                              threads=True, progress=False)
        except Exception as e:
            print(f"  batch {i//batch + 1} misslyckades helt: {e}")
            failed.extend(chunk)
            continue
        for t in chunk:
            try:
                sub = raw[t] if len(chunk) > 1 else raw
                df = normalize(sub.copy())
                if df.empty:
                    failed.append(t)
                    continue
                n = A.ingest_prices(t, df.to_dict("records"))
                ok.append(t)
                print(f"  {t}: {len(df)} nya rader, {n} totalt")
            except Exception as e:
                failed.append(t)
                print(f"  {t}: FEL {e}")
        time.sleep(1.2)

    try:
        b = yf.download(bench, period=period, interval="1d",
                        auto_adjust=False, progress=False)
        A.ingest_prices(bench, normalize(b.copy()).to_dict("records"))
        print(f"  {bench}: ok")
    except Exception as e:
        print(f"  {bench}: FEL {e}")

    # rangordna pa omsattning och behall topp 60
    scored = []
    for m in members:
        if m["ticker"] not in ok:
            continue
        df = A.load_prices(m["ticker"])
        if df is None or len(df) < 60:
            continue
        turnover = float((df["volume"].iloc[-20:] * df["close"].iloc[-20:]).mean())
        scored.append((turnover, m))
    scored.sort(key=lambda x: -x[0])
    active = [m for _, m in scored[:60]]
    A.save_json(A.CONFIG / "universe_active.json",
                {"generated": pd.Timestamp.today().date().isoformat(),
                 "members": active,
                 "excluded": [t for t in tickers if t not in [m["ticker"] for m in active]]})

    print(f"\nKlart: {len(ok)} lyckades, {len(failed)} misslyckades.")
    if failed:
        print("Misslyckade: " + ", ".join(failed))
    print(f"Aktivt universum: {len(active)} bolag.")
    return 0 if len(ok) >= 20 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
