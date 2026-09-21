# Event path audit

`backtests/event_paths.py` adds intratrade path diagnostics to the existing
liquidation-reversion and crowding event CSVs. It is a post-processing audit:
the source CSV's `entry_ts`, `exit_ts`, and `net_bp`/`net_ret_bp` are preserved
as the endpoint record; it does not generate a new strategy return.

Run it with an explicit, reproducible input selection and a new output directory:

```bash
python3 backtests/event_paths.py \
  --db /mnt/e/Datas/market/market.db \
  --output-dir /tmp/event-paths \
  --event-file results/liq_reversion/events_btc_long_ret-2_oi-1_entry0_exit4h.csv \
  --event-glob 'results/crowding/A2_contrarian_events_BTCUSDT_24h.csv'
```

The database is opened read-only. Only Binance perpetual BTCUSDT/ETHUSDT 1-minute
OHLCV is used. Event rows require `entry_ts < exit_ts`, minute alignment, a
matching entry bar and exact exit-bar **open**, complete one-minute coverage for
`entry_ts <= bar.ts < exit_ts`, and `exit_ts < 2026-08-01T00:00:00Z`. Invalid,
late, or incomplete rows are marked incomplete and recorded with a reason.
When a source supplies `entry_px` and/or `exit_px`, those values must match the
corresponding database opens. For a finite source net return, the reconstructed
linear gross return must equal source net plus the source's 14bp round-trip cost.
Every event must supply one finite numeric `net_bp` or `net_ret_bp`; missing,
null, non-finite, or nonnumeric endpoint returns (and malformed optional endpoint
prices) reject only that row, without preventing other rows in the same CSV from
being audited.

For both directions, excursion uses the source strategy's linear original-notional
return: `sign * (price / entry - 1)`, where sign is +1 for long and -1 for short.
For a long MAE/MFE therefore use low/high; for a short they use high/low. The
entry baseline is included, so MAE cannot be positive and MFE cannot be negative.
`underwater_minutes` counts only path-bar closes below entry for longs and above
entry for shorts. The terminal `exit_ts` **open** participates in MAE, MFE, and
barrier detection; its high, low, and close do not.

`--stop-bp` and `--take-bp` (both default to 100) are descriptive barriers.
The metrics file records their first observable contact. A bar open crossing is marked
`*_open`, covering gap opens. If a later OHLC bar spans both barriers, its within
bar ordering is unknowable and `first_barrier_hit` is `ambiguous` with an
`ambiguous_bar_ts`; no first-hit
claim is made.

Outputs are `event_path_metrics.csv`, `event_path_rejected.csv`,
`event_path_summary.csv`, and `event_path_manifest.json`. The summary has
per-strategy total, complete, and missing/invalid-path counts, MAE/MFE and
underwater-time quantiles for complete paths, plus stop/take/ambiguous barrier
rates. `event_path_metrics.csv` retains incomplete rows with `path_complete=false`;
the rejected CSV is the reason-indexed subset. The manifest pins selected input
file hashes and parameters, the database path/size/modification time, the script
hash, and SHA-256 hashes of the generated CSV outputs. The output directory must
be new or empty.

B2 deleverage source files place 24h and 72h observations in one CSV. Their
row-level `horizon` is appended to the audit strategy key, so its MAE/MFE,
underwater, and barrier summaries remain separate by holding horizon.
