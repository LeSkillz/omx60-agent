"""
Sjalvtest. Kopierar projektet till en temporar katalog, fyller det med
syntetisk prisdata och kor hela kedjan morgon -> kvall -> rapport.
Ror aldrig din riktiga data.

Kor:  python3 scripts/selftest.py
"""
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parent.parent


def synth(seed, n=400, start=100.0):
    rng = np.random.default_rng(seed)
    drift = rng.normal(0.0004, 0.0006)
    vol = rng.uniform(0.010, 0.025)
    r = rng.normal(drift, vol, n)
    close = start * np.exp(np.cumsum(r))
    dates = pd.bdate_range(end=dt.date.today(), periods=n)
    high = close * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.006, n)))
    op = low + (high - low) * rng.uniform(0.2, 0.8, n)
    return pd.DataFrame({"date": dates, "open": op.round(2), "high": high.round(2),
                         "low": low.round(2), "close": close.round(2),
                         "volume": rng.integers(1e5, 5e6, n), "adjclose": close.round(2)})


def main(days=1):
    tmp = Path(tempfile.mkdtemp(prefix="omxtest_"))
    root = tmp / "ClaudeStock"
    shutil.copytree(SRC / "config", root / "config")
    shutil.copytree(SRC / "scripts", root / "scripts")
    (root / "data" / "prices").mkdir(parents=True)
    (root / "data" / "qualitative").mkdir(parents=True)

    uni = json.loads((root / "config" / "universe.json").read_text(encoding="utf-8"))
    members = uni["candidates"]
    full = {}
    for i, m in enumerate(members):
        full[m["ticker"]] = synth(i)
    full["^OMX"] = synth(999)

    cal = list(pd.bdate_range(end=dt.date.today(), periods=400))
    sim_days = [d.date().isoformat() for d in cal[-days:]]
    rng = np.random.default_rng(7)
    ok = True

    for n, day in enumerate(sim_days, 1):
        # klipp historiken sa att bara data t.o.m. denna dag finns
        for tkr, df in full.items():
            cut = df[df["date"].dt.date.astype(str) <= day]
            cut.to_csv(root / "data" / "prices" / f"{tkr.replace('.', '_')}.csv",
                       index=False)
        qual = {"date": day,
                "macro": {"market": int(rng.integers(-2, 3)),
                          "sector_scores": {"Industri": 1, "Finans": -1},
                          "notes": "Syntetiskt testlage."},
                "tickers": {m["ticker"]: {"fundamental": int(rng.integers(-2, 3)),
                                          "sentiment": int(rng.integers(-2, 3)),
                                          "notes": "syntetisk testdata", "sources": []}
                            for m in members[:40]}}
        (root / "data" / "qualitative" / f"{day}.json").write_text(
            json.dumps(qual, ensure_ascii=False), encoding="utf-8")

        for step in ("run_morning.py", "run_evening.py"):
            r = subprocess.run([sys.executable, str(root / "scripts" / step), day],
                               cwd=root / "scripts", capture_output=True, text=True)
            if days <= 3 or n == len(sim_days):
                print(f"--- dag {n}/{len(sim_days)} {day} :: {step} ---")
                print(r.stdout.strip() or "(ingen utdata)")
            if r.returncode != 0:
                ok = False
                print("FEL:\n" + r.stderr[-2500:])

    print("\n===== build_report.py =====")
    r = subprocess.run([sys.executable, str(root / "scripts" / "build_report.py")],
                       cwd=root / "scripts", capture_output=True, text=True)
    print(r.stdout.strip() or "(ingen utdata)")
    if r.returncode != 0:
        ok = False
        print("FEL:\n" + r.stderr[-2500:])

    w = json.loads((root / "config" / "strategy.json").read_text(encoding="utf-8"))["weights"]
    print("Vikter efter simuleringen:", {k: round(v, 3) for k, v in w.items()})

    rep = root / "report" / "index.html"
    if rep.exists():
        size = rep.stat().st_size
        print(f"\nRapport: {size:,} bytes")
        ok = ok and size > 5000
    else:
        ok = False
        print("\nIngen rapport genererad.")

    port = json.loads((root / "data" / "portfolio.json").read_text(encoding="utf-8"))
    print(f"Equity: {port['equity']:,.0f} | oppna: {len(port['open_positions'])} "
          f"| avslut: {len(port['closed_trades'])}")
    print("\n" + ("SJALVTEST OK" if ok else "SJALVTEST MISSLYCKADES"))
    print(f"Testkatalog: {root}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1))
