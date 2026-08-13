# OMX60-agenten

Autonom analysagent för Stockholmsbörsens 60 största bolag. Producerar dagliga
och veckovisa handelsuppslag, utvärderar dem mot faktiskt utfall och justerar sin
egen modell efter varje handelsdag.

**Detta är pappershandel.** Inga riktiga order läggs. Systemet är byggt för att
mäta om modellen håller innan några pengar är inblandade.

## Mappstruktur

```
ClaudeStock/
├── config/
│   ├── universe.json        Kandidatuniversum (70 namn) + benchmark
│   ├── universe_active.json  Skapas när universumet validerats mot datakällan
│   └── strategy.json        Vikter, risknivåer, trösklar — agentens lärbara del
├── data/
│   ├── prices/              OHLCV per bolag, CSV. Byggs upp över tid.
│   ├── qualitative/         <datum>.json — sentiment, fundamenta, omvärld
│   ├── signals/             <datum>.json + <datum>_scores.csv
│   ├── evaluations/         <datum>.json, _ic.json, _weights.json
│   └── portfolio.json       Pappersportföljen
├── memory/
│   └── lessons.md           Agentens kvalitativa lärdomslogg
├── report/
│   └── index.html           Den levande rapporten — öppna i webbläsare
└── scripts/
    ├── omxagent.py          Bibliotek: data, features, poäng, portfölj
    ├── run_morning.py       Signaler + öppnar positioner
    ├── run_evening.py       Stänger, utvärderar, justerar vikter
    ├── build_report.py      Bygger rapporten
    └── selftest.py          Testar hela kedjan på syntetisk data
```

## Så fungerar modellen

Varje bolag får sju delpoäng, alla normaliserade till ungefär −1…+1 och
tvärsnittsjämförda mot resten av universumet:

| Komponent | Källa | Mäter |
|---|---|---|
| `momentum` | pris | 12−1-månaders och 3-månaders avkastning |
| `trend` | pris | Läge mot MA20/50/200, avstånd till 52-veckorshögsta |
| `reversal` | pris | Kortsiktig rekyl: svag vecka + RSI under 50 |
| `volume` | pris | Volymuppgång senaste veckan mot 60-dagarssnitt |
| `fundamental` | agentens research | Värdering, vinsttrend, estimatrevideringar, riktkurser |
| `sentiment` | agentens research | Forum, sociala medier, nyhetsflöde |
| `macro` | agentens research | Sektorläge och marknadsläge |

Totalpoängen är den viktade summan. Vikterna skalas dessutom om per horisont:
dagshandel lutar mot rekyl, volym och sentiment; veckohandel mot momentum, trend
och fundamenta.

Positionsstorlek sätts av risk, inte av övertygelse: varje affär riskerar
1,5 % av kapitalet ner till stoppen, som ligger 1,5 × ATR(14) bort. Målkursen
ligger 2,5 × ATR bort. Courtage och spread dras med 10 punkter per sida.

## Lärandet

Efter varje handelsdag mäts *information coefficient* per komponent —
rangkorrelationen mellan komponentens värde och dagens faktiska avkastning över
hela universumet. Ett rullande 60-dagarssnitt av dessa värden avgör vart vikterna
flyttas, med högst 0,02 i steg per dag och golv/tak på 0,03 respektive 0,45.

Det betyder att modellen inte kan svänga vilt på en enskild bra dag, och att en
komponent som konsekvent inte förutsäger något krymper bort av sig själv. Innan
15 observationer finns rörs vikterna inte alls.

## Köra manuellt

```bash
cd scripts
python3 run_morning.py     # efter att prisdata och qualitative/<datum>.json finns
python3 run_evening.py     # efter börsstängning
python3 build_report.py
python3 selftest.py 12     # simulerar 12 dagar på syntetisk data
```

## Vad du behöver veta om begränsningarna

- **Historiken byggs upp över tid.** Modellen behöver minst 130 handelsdagar per
  bolag för att räkna features. Vid start hämtas två års historik en gång.
- **Utvärderingen antar konservativt utfall.** Om både stop och målkurs träffas
  samma dag räknas stoppen.
- **Ingen slippage-modell utöver de 10 punkterna.** Verklig handel i mindre
  likvida namn blir dyrare.
- **Träffsäkerhet på få dagar säger ingenting.** Räkna med minst 40–60
  handelsdagar innan siffrorna är värda att tolka.
