"""Offline, event-conditioned diagnostic demonstration; not a backtest.

Run from any directory with a NEW output path argument. Never overwrites reports.
"""
from dataclasses import replace
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from live_signals.early_warning import MINUTE_MS, ReferenceLevels, evaluate
from live_signals.io import load_archive, sha256_file

REPORT = Path(__file__).resolve().parents[1]
ARCHIVE = REPORT / "verification-1747/public_snapshots.zip"
LEVELS = {"BTC": (81228.15, 81878.98), "ETH": (2653.43, 2678.70), "HYPE": (92.92, 94.42)}


def timestamp(clock):
    return int(datetime.fromisoformat(f"2026-09-21T{clock}:00+09:00").timestamp() * 1000)


def future_mutation(series, cutoff):
    return replace(series, bars=[replace(bar, open=bar.open * 10, high=bar.high * 10,
                                        low=bar.low * 10, close=bar.close * 10)
                                if bar.ts >= cutoff else bar for bar in series.bars])


def main(output_path):
    generated = int(datetime.now(timezone.utc).timestamp() * 1000)
    manifest = json.loads(ARCHIVE.with_name("sources.json").read_text())
    payload = {
        "purpose": "Event-conditioned market-time prefix replay, not predictive validation.",
        "generated_at_ms": generated,
        "archive_sha256": sha256_file(ARCHIVE),
        "sources_sha256": sha256_file(ARCHIVE.with_name("sources.json")),
        "original_report_sha256": sha256_file(REPORT / "report.md"),
        "engine_sha256": sha256_file(ROOT / "live_signals/early_warning.py"),
        "io_sha256": sha256_file(ROOT / "live_signals/io.py"),
        "script_sha256": sha256_file(__file__),
        "limits": ["Fixed high4/low4 levels copied from original report, available market context at 16:00.",
                   "known_at=16:00 is a market-time assumption, not proof levels were computed then.",
                   "Rules were authored after observing the event; no out-of-sample or false-alarm estimate.",
                   "Later data were actually received around 17:47:53–17:47:55 JST, not at replay timestamps.",
                   "Original minute inputs stop at 16:00 JST; HYPE original spot minute data absent.",
                   "No 4–48h return or holding suitability assessed."],
        "coins": {},
    }
    for coin, (support, resistance) in LEVELS.items():
        spot, perp, _ = load_archive(ARCHIVE, coin)
        reference = ReferenceLevels(spot.venue, spot.symbol, support, resistance, timestamp("16:00"))
        minute_states = []
        selected = {}
        for decision in range(timestamp("16:00"), timestamp("17:47") + MINUTE_MS, MINUTE_MS):
            result = evaluate(spot, perp, as_of_ms=decision, generated_at_ms=generated,
                              reference=reference, mode="market_time_replay")
            # Entire future price path can change without changing this decision.
            mutated = evaluate(future_mutation(spot, decision), future_mutation(perp, decision),
                               as_of_ms=decision, generated_at_ms=generated,
                               reference=reference, mode="market_time_replay")
            if result != mutated:
                raise AssertionError(f"future leakage at {coin} {decision}")
            label = datetime.fromtimestamp(decision / 1000, timezone.utc).astimezone(
                datetime.fromisoformat("2026-09-21T00:00:00+09:00").tzinfo).strftime("%H:%M")
            minute_states.append({"time_jst": label, "state": result["state"],
                                  "flags": result["flags"], "price": result.get("price"),
                                  "volume_confirmed": result.get("confirmation", {}).get("volume_confirmed")})
            if label in {"16:00", "16:30", "16:50", "16:55", "16:58", "17:10", "17:15", "17:30", "17:35", "17:47"}:
                selected[label] = result
        original_observed = None
        original_replay = None
        if coin != "HYPE":
            old_spot, old_perp, _ = load_archive(REPORT / "public_snapshots.zip", coin)
            original_observed = evaluate(old_spot, old_perp,
                as_of_ms=manifest["old_report_last_modified_ms"], generated_at_ms=manifest["old_report_last_modified_ms"],
                reference=reference, mode="observed")
            original_replay = evaluate(old_spot, old_perp, as_of_ms=timestamp("16:00"),
                generated_at_ms=generated, reference=reference, mode="market_time_replay")
        payload["coins"][coin] = {"reference": selected["16:00"]["reference"],
            "minute_states": minute_states, "selected_prefixes": selected,
            "future_mutation_invariance_checks": len(minute_states),
            "original_archive_observed_at_report_mtime": original_observed,
            "original_archive_market_time_replay_1600": original_replay}
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with Path(output_path).open("x", encoding="utf-8") as handle:
        handle.write(serialized)
    for coin, data in payload["coins"].items():
        print(coin)
        for time, result in data["selected_prefixes"].items():
            print(time, result["state"], [item["name"] + ":" + item.get("market", "")
                  for item in result["flags"]], result["confirmation"]["volume_confirmed"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="new output JSON path (never overwritten)")
    main(parser.parse_args().output)
