# Kalshi BTC Trading Bot

Profitability-first bot for Kalshi **KXBTC15M** (15-minute) and **KXBTCD** (hourly directional / threshold) Bitcoin markets.

## Edge model

Mispricing is detected by comparing:

1. **Kalshi implied probability** — executable YES/NO ask on the book  
2. **Options-implied probability** — Black-Scholes \(N(d_2)\) using the **IBIT ETF volatility smile**, translated into BTC spot space via the live `IBIT/BTC` price ratio

Example: Kalshi YES @ 22% while options imply 37.8% → **15.8pp edge** → take the trade.

Settlement reference for Kalshi is **CF Benchmarks BRTI** (60s average). Without a CF license the bot proxies spot with BTC-USD; relative moves dominate short-horizon probability.

### Confidence tiers (BTC 50–80% IV regime)

| Tier   | Rule                                      |
|--------|-------------------------------------------|
| HIGH   | ≥15pp edge **and** tight book             |
| MEDIUM | ≥10pp edge                                |
| LOW    | 5–10pp edge                               |
| PASS   | &lt;5pp                                     |

Default execution only takes **HIGH** and **MEDIUM**.

## Cross-venue arb

Secondary strategy: same 15-minute BTC window on Kalshi and Polymarket.

- Buy **Kalshi UP + Polymarket DOWN** when asks sum &lt; `$1.00` (config default `0.99`)
- Or **Kalshi DOWN + Polymarket UP** under the same rule  

That locked pair pays `$1.00` at settlement → risk-free residual is the edge (basis risk: BRTI vs Chainlink TWAP).

## Dashboard

Trade blotter UI that reads the SQLite journal (`data/journal.db`):

```bash
python -m kalshi_bot --once          # run a cycle (logs signals + fills)
python -m kalshi_bot --dashboard     # Edge Desk on http://0.0.0.0:8787
```

Shows fills, signal tape, scan history, and notional/edge stats. Auto-refreshes every 3s.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# edit .env — leave DRY_RUN=true until keys are set

python -m kalshi_bot --once          # one scan cycle (dry-run)
python -m kalshi_bot --scan-only     # print signals, never size orders
python -m kalshi_bot                 # continuous loop
python -m kalshi_bot --live          # real Kalshi orders (requires API key)
```

### Kalshi credentials

1. [kalshi.com/account/profile](https://kalshi.com/account/profile) → API Keys → Create  
2. Save the private key to `secrets/kalshi_private.key`  
3. Set `KALSHI_API_KEY_ID` in `.env`  
4. Set `DRY_RUN=false` or pass `--live`

### Config

See `config/default.yaml` for series, tier thresholds, arb pair-cost, and risk caps.

## Architecture

```
src/kalshi_bot/
  models/          Black-Scholes, vol smile, edge/tiers
  data/            IBIT options (yfinance) + BRTI proxy
  venues/          Kalshi RSA-PSS client, Polymarket Gamma/CLOB
  strategies/      Mispricing scanner, cross-venue arb
  execution/       Risk sizing (fractional Kelly), order engine
  bot.py / cli.py  Loop + rich tables
```

## Tests

```bash
pytest -q
```

Live read-only venue tests hit public Kalshi/Polymarket APIs (no keys).

## Disclaimer

Prediction-market trading is risky. This software defaults to **dry-run**. Options-implied probabilities are risk-neutral model estimates, not guarantees. Cross-venue arb has settlement-index basis risk (BRTI vs Chainlink). You are responsible for compliance with Kalshi / Polymarket terms and applicable law.
