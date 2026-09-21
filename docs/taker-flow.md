# Taker-flow hourly features

`backtests/taker_flow.py` creates a descriptive hourly feature dataset from Binance spot and perpetual one-minute candles. It supports a direct script invocation and module invocation:

```bash
python3 backtests/taker_flow.py --symbol BTCUSDT --start 2026-07-01 --end 2026-08-01
python3 -m backtests.taker_flow --output-dir /tmp/taker-flow
```

The output directory contains `features.csv`, `state_summary.csv`, and `run_manifest.json`. Existing output files are never overwritten. The manifest records settings, state counts, coverage, cutoff, and caveats. This MVP is deliberately limited to Binance `BTCUSDT` and `ETHUSDT`.

Each emitted market/hour has all 60 one-minute candles. `ts` and `decision_ts` refer to the **end** of that completed hour, so price, OI, and funding joins are available at the decision time. `taker_net_quote` is `2 * taker_buy_quote - quote_volume`; `taker_imbalance` is that amount divided by quote volume. Missing or zero-volume hours have an `unclassified_*` state and no signed-flow or imbalance signal. The spot/perpetual divergence is only present when both markets are classified. `spot_price_change_1h`, `perp_price_change_1h`, and `basis` (`perp_close / spot_close - 1`) provide the associated price context.

The three z-score columns use only earlier completed hourly observations: the current hour never contributes to its own mean or standard deviation. Their default reference is 168 prior hours with at least 72 usable prior values. A missing hourly decision breaks this history, so a later row never silently treats a multi-hour gap as one-hour continuity.

`flow_oi_state` is the diagnostic classification and `state_summary.csv` gives its count and contiguous duration. It is computed after rejecting unavailable flow/OI inputs:

- `unclassified`: either market has missing/zero/invalid flow, causal history is insufficient, OI quantity is unavailable, or its one-hour change cannot be measured.
- `oi_contracting`: available OI contract quantity change is at or below `--oi-contracting-threshold` (default -1%).
- `spot_buy_dominant_consistent`: spot imbalance and its causal z-score exceed `--imbalance-threshold` (5%) and `--flow-z-threshold` (1), respectively, and spot imbalance exceeds perpetual imbalance by `--spot-dominance-gap` (2%).
- `perp_buy_oi_increase_consistent`: perpetual imbalance/z-score exceed those flow thresholds and OI contract quantity rises by at least `--oi-increase-threshold` (1%).
- `mixed`: all required inputs are available but no preceding rule applies.

Rules are evaluated in this precedence after `unclassified`: contraction, perpetual-buy-with-OI-increase, then spot-buy dominance. Thus simultaneous strong flows with increasing OI are assigned to the perpetual/OI state; an insufficient spot dominance gap remains `mixed`. All thresholds are recorded in the manifest settings.

Open interest is supplied both as contract quantity (`oi_open_interest`) and dollar value (`oi_oi_value`), with independent one-hour changes. They are deliberately separate: neither is a proxy for the other, and neither measures directional capital flow. OI and price alone cannot identify new longs, new shorts, or short covering. OI and funding use a backward-as-of join at the hour end, expose the source timestamp and age in hours, and become `stale` after the configured maximum age (12 hours by default). A stale value remains visible for diagnosis but is labelled as unavailable for signal use.

The analysis end is exclusive and is hard-capped at `2026-08-01T00:00:00Z`. This prevents accidental inclusion of data after the project’s specified research boundary. The dataset is descriptive, uses candle-level taker aggregates rather than trade-level order flow, and does not establish causality or a tradable edge.
