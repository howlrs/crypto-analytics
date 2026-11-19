from __future__ import annotations

from contextlib import redirect_stderr
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from live_signals import cli
from live_signals.io import InputError, load_archive, normalize_input


def _normalized(received_ts: int | None = 1_700_000_060_000) -> dict:
    bar = {"ts": 1_700_000_000_000, "open": "10", "high": "12", "low": "9", "close": "11",
           "quote_volume": "100", "taker_buy_quote": "60"}
    if received_ts is not None:
        bar["received_ts"] = received_ts
    return {"spot": {"venue": "binance", "market": "spot", "symbol": "BTCUSDT", "bars": [bar]}}


def _complete_series(count: int = 85) -> tuple[dict, int]:
    as_of = (int(datetime.now(timezone.utc).timestamp() * 1000) // 60_000) * 60_000
    start = as_of - count * 60_000
    bars = []
    for index in range(count):
        ts = start + index * 60_000
        price = 100 + index * 0.1
        bars.append({"ts": ts, "open": price, "high": price + 0.3, "low": price - 0.2,
                     "close": price + 0.1, "quote_volume": 1000, "taker_buy_quote": 550,
                     "received_ts": ts + 60_000})
    return {"spot": {"venue": "binance", "market": "spot", "symbol": "BTCUSDT", "bars": bars}}, as_of


def _iso8601(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


class EarlyWarningIoTests(unittest.TestCase):
    def test_normalizer_preserves_missing_receipt_and_rejects_bad_order(self) -> None:
        spot, perp = normalize_input(_normalized(None))
        self.assertIsNone(perp)
        self.assertIsNone(spot.bars[0].received_ts)
        data = _normalized()
        data["spot"]["bars"].append(dict(data["spot"]["bars"][0]))
        with self.assertRaisesRegex(InputError, "strictly ascending"):
            normalize_input(data)

    def test_cli_rejects_naive_timestamp(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            cli.main(["data.json", "--as-of", "2026-01-01T00:00:00", "--output", "out.json"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_exclusive_output_and_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "input.json", Path(directory) / "out.json"
            source.write_text(json.dumps(_normalized()), encoding="utf-8")
            with patch.object(cli.early_warning, "evaluate", return_value={"state": "diagnostic"}):
                self.assertEqual(cli.main([str(source), "--as-of", "2026-01-01T00:00:00Z", "--output", str(destination)]), 0)
                result = json.loads(destination.read_text(encoding="utf-8"))
                self.assertEqual(result["diagnostic"], {"state": "diagnostic"})
                self.assertEqual(len(result["source_sha256"]), 64)
                self.assertIsNone(result["manifest_sha256"])
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main([str(source), "--as-of", "2026-01-01T00:00:00Z", "--output", str(destination)])
            self.assertEqual(error.exception.code, 2)

    def test_archive_rejects_nonmatching_member_sha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "public_snapshots.zip"
            (root / "sources.json").write_text(json.dumps({"requests": [
                {"name": "HYPE_spot_1m", "status": "ok", "finished_ms": 50, "sha256": "not-a-match"},
                {"name": "HYPE_perp_1m", "status": "ok", "finished_ms": 60, "sha256": "also-not-a-match"},
            ]}), encoding="utf-8")
            bybit = {"result": {"list": [["120000", "2", "3", "1", "2", "9", "18"], ["60000", "1", "2", "0.5", "2", "4", "8"]]}}
            binance = [[60000, "1", "2", "0.5", "2", "4", 0, "8", 0, 0, "3"]]
            with ZipFile(archive, "w") as zipped:
                zipped.writestr("HYPE_spot_1m.json", json.dumps(bybit))
                zipped.writestr("HYPE_perp_1m.json", json.dumps(binance))
            with self.assertRaisesRegex(InputError, "no verified request"):
                load_archive(archive, "HYPE")

    def test_archive_uses_later_matching_sha_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "public_snapshots.zip"
            spot_raw = json.dumps([[60000, "1", "2", "0.5", "2", "4", 0, "8", 0, 0, "3"]]).encode()
            perp_raw = json.dumps([[60000, "1", "2", "0.5", "2", "4", 0, "8", 0, 0, "3"]]).encode()
            with ZipFile(archive, "w") as zipped:
                zipped.writestr("BTC_spot_1m.json", spot_raw)
                zipped.writestr("BTC_perp_1m.json", perp_raw)
            spot_hash, perp_hash = hashlib.sha256(spot_raw).hexdigest(), hashlib.sha256(perp_raw).hexdigest()
            (root / "sources.json").write_text(json.dumps({"requests": [
                {"name": "BTC_spot_1m", "status": "ok", "finished_ms": 10, "sha256": "wrong"},
                {"name": "BTC_spot_1m", "status": "ok", "finished_ms": 20, "sha256": spot_hash,
                 "url": "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m"},
                {"name": "BTC_perp_1m", "status": "ok", "finished_ms": 30, "sha256": perp_hash,
                 "url": "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1m"},
            ]}), encoding="utf-8")
            spot, perp, _ = load_archive(archive, "BTC")
            self.assertEqual(spot.bars[0].received_ts, 20)
            self.assertEqual(perp.bars[0].received_ts, 30)

    def test_cli_real_engine_observed_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload, as_of = _complete_series()
            source = root / "input.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            observed, replay = root / "observed.json", root / "replay.json"
            common = [str(source), "--as-of", _iso8601(as_of)]
            self.assertEqual(cli.main([*common, "--mode", "observed", "--output", str(observed)]), 0)
            self.assertEqual(cli.main([*common, "--generated-at", _iso8601(as_of), "--mode", "market_time_replay", "--output", str(replay)]), 0)
            observed_result = json.loads(observed.read_text(encoding="utf-8"))["diagnostic"]
            replay_result = json.loads(replay.read_text(encoding="utf-8"))["diagnostic"]
            self.assertTrue(observed_result["usable_for_live"])
            self.assertFalse(replay_result["usable_for_live"])
            self.assertEqual(replay_result["publishable_state"], "historical_replay")

    def test_cli_nan_result_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "input.json", root / "out.json"
            source.write_text(json.dumps(_normalized()), encoding="utf-8")
            with patch.object(cli.early_warning, "evaluate", return_value={"value": float("nan")}):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    cli.main([str(source), "--as-of", "2026-01-01T00:00:00Z", "--output", str(destination)])
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(destination.exists())

    def test_archive_rejects_missing_request_list_and_wrong_verified_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "public_snapshots.zip"
            raw = json.dumps([[60000, "1", "2", "0.5", "2", "4", 0, "8", 0, 0, "3"]]).encode()
            with ZipFile(archive, "w") as zipped:
                zipped.writestr("BTC_spot_1m.json", raw)
                zipped.writestr("BTC_perp_1m.json", raw)
            digest = hashlib.sha256(raw).hexdigest()
            manifest = root / "sources.json"
            manifest.write_text(json.dumps({"requests": None}), encoding="utf-8")
            with self.assertRaisesRegex(InputError, "requests must be a list"):
                load_archive(archive, "BTC")
            manifest.write_text(json.dumps({"requests": [
                {"name": "BTC_spot_1m", "status": "ok", "finished_ms": 10, "sha256": digest,
                 "url": "https://api.binance.com/api/v3/klines?symbol=ETHUSDT&interval=1m"},
                {"name": "BTC_perp_1m", "status": "ok", "finished_ms": 10, "sha256": digest,
                 "url": "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1m"},
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(InputError, "does not verify"):
                load_archive(archive, "BTC")

    def test_cli_rejects_explicit_generation_time_in_observed_mode(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            cli.main(["data.json", "--as-of", "2026-01-01T00:00:00Z",
                      "--generated-at", "2026-01-01T00:00:01Z", "--output", "out.json"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_bad_normalized_input_exits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad.json"
            destination = root / "out.json"
            source.write_text(json.dumps({"spot": {"bars": []}}), encoding="utf-8")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                cli.main([str(source), "--as-of", "2026-01-01T00:00:00Z", "--output", str(destination)])
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
