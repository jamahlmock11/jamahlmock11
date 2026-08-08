# Kalshi BTC Edge Bot

Paper-first scanner for **genuine mispricing** on Kalshi `KXBTC15M` (15m up/down) and `KXBTCD` (strike ladder), plus a Kalshi↔Polymarket cross-venue flagger.

Profitability is the goal; this repo does **not** promise it. Edges are only as good as the IBIT vol surface, fee model, and settlement alignment. Default mode is **paper**. Live orders require dual opt-in and are intentionally not auto-routed until you wire authenticated Kalshi order placement.

## Edge model

1. Pull Kalshi book (YES mid / spread) for open `KXBTC15M` / `KXBTCD` markets.
2. Load IBIT ETF volatility smile → translate BTC strike into IBIT space:
   `ibit_strike = btc_strike * (ibit_spot / btc_spot)`
3. Black–Scholes digital `P(YES) = N(d2)` with smile IV at that moneyness (short-tenor uplift).
4. `edge_pp = (options_P_yes - kalshi_mid) * 100`  
   Example: Kalshi **22%** vs options **37.8%** → **+15.8pp** → buy YES.

Settlement reference for Kalshi is **CF Benchmarks BRTI** (60s average). Coinbase/Yahoo spots are pricing proxies only.

### Confidence tiers (BTC 50–80% IV regime)

| Tier   | Edge                         | Book                          |
|--------|------------------------------|-------------------------------|
| HIGH   | ≥ 15pp                       | spread ≤ 4¢                   |
| MEDIUM | ≥ 10pp                       | (HIGH demoted if book wide)   |
| LOW    | 5–10pp                       |                               |
| PASS   | < 5pp                        | skip                          |

### Cross-venue (alternative)

When **Kalshi UP ask + Polymarket DOWN ask < $1.00** (or the reverse), a candidate arb is flagged. This is **not** risk-free if oracles/windows differ—use `cross_venue.contract_map` for exact pairs.

## Setup

```bash
cd kalshi_btc_edge
pip install -r requirements.txt
```

Edit `config.yaml`. For production, replace `data/ibit_smile.json` with a live OPRA/IBIT surface (`smile_source: file`). Yahoo options often 401.

## Run

```bash
cd kalshi_btc_edge
PYTHONPATH=src python3 -m kalshi_btc_edge.cli        # one-shot scan
PYTHONPATH=src python3 -m kalshi_btc_edge.cli bot    # polling loop
```

Paper fills only execute for signals ≥ `execution.min_confidence` (default `HIGH`).

Live trading requires **both**:

- `execution.mode: live` in `config.yaml`
- `ENABLE_LIVE_TRADING=1` in the environment  

Even then, the bot refuses to send real orders until order routing is implemented (safety latch).

## Tests

```bash
cd kalshi_btc_edge
PYTHONPATH=src python3 -m pytest -q
```

## Layout

```
src/kalshi_btc_edge/
  pricing/          # BS digital, smile map, confidence
  clients/          # Kalshi, spot proxies, Polymarket
  strategies/       # mispricing + cross-venue
  execution/        # Kelly sizing + paper broker
  bot.py / cli.py
```
