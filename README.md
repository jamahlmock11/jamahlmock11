# Kalshi BTC 15-Minute Forecasting & Trading System

Safety-first Python system for Kalshi **KXBTC15M** markets. It forecasts the
terminal BRTI outcome, compares calibrated probability with executable order-book
cost, and defaults to **NO TRADE**.

## Decision model

Each decision combines:

1. official CME CF Bitcoin Real Time Index (BRTI) settlement data
2. causal 5s–5m velocity, acceleration, trajectory, and reversal features
3. volatility-normalized strike distance and time remaining
4. regime-dependent trend, mean-reversion, order-book, and venue-confirmation signals
5. optional IBIT volatility and historical/calibrated priors
6. executable Kalshi depth, fees, and slippage

`P(DOWN) = 1 - P(UP)`. Current direction and predicted expiration direction
remain separate.

### Immutable entry rule

An entry is impossible unless:

```text
model probability - all-in executable price >= 0.20
```

The configured target is 0.25. Configuration can tighten the 0.20 floor but
cannot lower it. Stale/missing BRTI, malformed contracts, poor liquidity, wide
spreads, low confidence, signal conflict, open orders, and risk locks all produce
NO TRADE. The final 60 seconds require at least the target edge and stronger
confidence.

## Settlement data

Set `CF_BENCHMARK_URL` to an official/licensed JSON endpoint returning an
explicit BRTI source, price, and timestamp. Optional authorization fields are in
`.env.example`.

When licensed data is unavailable, `benchmark_mode: constituent_proxy` provides
an explicitly unofficial **PAPER-only** estimate from the median top-of-book
midpoint on publicly accessible CME CF constituent venues: Coinbase, Kraken,
Bitstamp, Gemini, and Crypto.com. At least three fresh venues must agree within
the configured dispersion limit. Proxy probabilities and confidence are
shrunk, require at least a 25-point edge, and cannot open positions in the final
two minutes.

This approximation is not BRTI. It does not include Bullish or LMAX Digital and
cannot reproduce CF Benchmarks' capped, uncrossed consolidated order book and
price-volume curves. LIVE entries remain locked unless official primary BRTI is
configured.

Kalshi's contract uses the simple average of the final 60 BRTI observations. The
market strike/reference is read from the live Kalshi contract; current spot is
never used as an invented strike.

## Dashboard

The real-time dashboard reads the SQLite decision journal:

```bash
python -m kalshi_bot --once
python -m kalshi_bot --dashboard     # http://0.0.0.0:8787
```

It shows BRTI, strike, time, probabilities, executable prices/edges, momentum,
acceleration, volatility, regime, signal agreement, position, data health, risk
state, and structured reasons for BUY, EXIT, HOLD, and NO TRADE.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# edit .env — leave DRY_RUN=true until keys are set

python -m kalshi_bot --once          # one PAPER cycle
python -m kalshi_bot --scan-only     # decide and journal; no simulated order
python -m kalshi_bot                 # continuous PAPER mode
python -m kalshi_bot --live          # explicit LIVE switch; requires credentials
```

### Kalshi credentials

1. [kalshi.com/account/profile](https://kalshi.com/account/profile) → API Keys → Create  
2. Save the private key to `secrets/kalshi_private.key`  
3. Set `KALSHI_API_KEY_ID` in `.env`  
4. Set `DRY_RUN=false` or pass `--live`

### Configuration

See `config/default.yaml` for data freshness, hard/target edge, late-contract
gates, execution assumptions, and risk limits. PAPER is the default.

## Architecture

```
src/kalshi_bot/
  data/            strict BRTI + supporting venues + optional IBIT volatility
  market/          active-contract validation + order-book execution estimates
  features/        causal trajectory/volatility/strike-distance features
  models/          regime classifier + ensemble terminal probability
  strategies/      hard-gated structured decision pipeline
  execution/       positions, exits, duplicate protection, risk locks
  calibration/     reliability bins and causal probability calibration
  backtest/        chronological depth/fee/slippage replay
  learning/        observational segment analysis
  dashboard/       decision, position, and explanation UI
  journal.py       SQLite journal for every decision
```

## Tests

```bash
pytest -q
```

The suite covers the hard edge boundary, BUY UP/DOWN, stale/malformed data,
market discovery, trajectory/reversal logic, execution/positions, risk locks,
exits/flips, calibration, and no-lookahead replay. Public venue tests are
read-only.

## Disclaimer

Prediction-market trading is risky. This software defaults to **PAPER**.
Probabilities are model estimates, not guarantees. Calibration requires
sufficient resolved out-of-sample decisions before live use. You are responsible
for data licensing, Kalshi eligibility, compliance, and losses.
