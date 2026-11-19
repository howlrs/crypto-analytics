"""Offline input adapters for :mod:`live_signals.early_warning`.

This module deliberately does no fetching.  It accepts a small normalized JSON
format and the archived public snapshots used by the research report.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse
from zipfile import ZipFile

from .early_warning import MarketSeries, MinuteBar, ReferenceLevels


class InputError(ValueError):
    """An offline input cannot be safely interpreted."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any, field: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise InputError(f"{field} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{field} must be a number") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise InputError(f"{field} must be finite")
    return result


def _timestamp(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise InputError(f"{field} must be an integer millisecond timestamp")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{field} must be an integer millisecond timestamp") from exc
    if result < 0 or str(result) != str(value).strip() and not isinstance(value, int):
        raise InputError(f"{field} must be an integer millisecond timestamp")
    return result


def normalize_series(payload: Mapping[str, Any]) -> MarketSeries:
    """Convert the documented normalized series object to engine dataclasses."""
    if not isinstance(payload, Mapping):
        raise InputError("series must be an object")
    try:
        venue, market, symbol, rows = (payload["venue"], payload["market"],
                                       payload["symbol"], payload["bars"])
    except KeyError as exc:
        raise InputError(f"series missing {exc.args[0]}") from exc
    if not all(isinstance(value, str) and value for value in (venue, market, symbol)):
        raise InputError("series venue, market, and symbol must be non-empty strings")
    if not isinstance(rows, list) or not rows:
        raise InputError("series bars must be a non-empty list")
    bars: list[MinuteBar] = []
    previous = -1
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise InputError(f"bars[{index}] must be an object")
        try:
            ts = _timestamp(row["ts"], f"bars[{index}].ts")
            bar = MinuteBar(
                ts=ts,
                open=_number(row["open"], f"bars[{index}].open"),  # type: ignore[arg-type]
                high=_number(row["high"], f"bars[{index}].high"),  # type: ignore[arg-type]
                low=_number(row["low"], f"bars[{index}].low"),  # type: ignore[arg-type]
                close=_number(row["close"], f"bars[{index}].close"),  # type: ignore[arg-type]
                quote_volume=_number(row.get("quote_volume"), f"bars[{index}].quote_volume", optional=True),
                taker_buy_quote=_number(row.get("taker_buy_quote"), f"bars[{index}].taker_buy_quote", optional=True),
                received_ts=(None if row.get("received_ts") is None else _timestamp(row["received_ts"], f"bars[{index}].received_ts")),
            )
        except KeyError as exc:
            raise InputError(f"bars[{index}] missing {exc.args[0]}") from exc
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close) or bar.low > bar.high:
            raise InputError(f"bars[{index}] has inconsistent OHLC")
        if ts <= previous:
            raise InputError("bars must be strictly ascending by ts")
        previous = ts
        bars.append(bar)
    return MarketSeries(venue=venue, market=market, symbol=symbol, bars=bars)


def normalize_input(payload: Mapping[str, Any]) -> tuple[MarketSeries, MarketSeries | None]:
    if not isinstance(payload, Mapping) or "spot" not in payload:
        raise InputError("input must contain spot")
    spot = normalize_series(payload["spot"])
    perp = None if payload.get("perp") is None else normalize_series(payload["perp"])
    return spot, perp


def load_normalized_json(path: str | Path) -> tuple[MarketSeries, MarketSeries | None]:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read normalized JSON: {exc}") from exc
    return normalize_input(payload)


def load_reference_json(path: str | Path) -> ReferenceLevels:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read reference JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise InputError("reference must be an object")
    try:
        return ReferenceLevels(
            venue=str(payload["venue"]), symbol=str(payload["symbol"]),
            support=_number(payload["support"], "reference.support"),  # type: ignore[arg-type]
            resistance=_number(payload["resistance"], "reference.resistance"),  # type: ignore[arg-type]
            known_at=_timestamp(payload["known_at"], "reference.known_at"),
            expires_at=None if payload.get("expires_at") is None else _timestamp(payload["expires_at"], "reference.expires_at"),
        )
    except KeyError as exc:
        raise InputError(f"reference missing {exc.args[0]}") from exc


def _manifest_receipt(manifest: Mapping[str, Any], name: str, filename: str, raw: bytes,
                      *, coin: str, venue: str, market: str) -> int:
    """Return a recorded receipt time only for an exact archived request name."""
    # Exact filename/name matching prevents accidentally assigning provenance
    # from similarly named data in another report generation.
    if Path(filename).stem != name:
        raise InputError("archive filename does not exactly match its manifest request")
    requests = manifest.get("requests")
    if not isinstance(requests, list):
        raise InputError("sources.json requests must be a list")
    content_sha256 = hashlib.sha256(raw).hexdigest()
    for request in requests:
        if (isinstance(request, Mapping) and request.get("name") == name
                and request.get("status") == "ok" and request.get("sha256") == content_sha256):
            _validate_request_url(request, coin=coin, venue=venue, market=market)
            value = request.get("completed_ms", request.get("finished_ms"))
            if value is None:
                raise InputError(f"manifest {name} lacks a receipt timestamp")
            return _timestamp(value, f"manifest {name} receipt")
    raise InputError(f"manifest has no verified request for archived {name}")


def _validate_request_url(request: Mapping[str, Any], *, coin: str, venue: str, market: str) -> None:
    url = request.get("url")
    if not isinstance(url, str):
        raise InputError("manifest request lacks a URL")
    parsed = urlparse(url)
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    symbol = f"{coin}USDT"
    if venue == "binance" and market == "spot":
        expected_host, expected_path, required = "api.binance.com", "/api/v3/klines", {"symbol": symbol, "interval": "1m"}
    elif venue == "binance" and market == "perp":
        expected_host, expected_path, required = "fapi.binance.com", "/fapi/v1/klines", {"symbol": symbol, "interval": "1m"}
    elif venue == "bybit" and market == "spot":
        expected_host, expected_path, required = "api.bybit.com", "/v5/market/kline", {"category": "spot", "symbol": symbol, "interval": "1"}
    else:  # Kept defensive if additional archive conventions are added later.
        raise InputError("unsupported archive market provenance")
    if (parsed.scheme != "https" or parsed.hostname != expected_host or parsed.path != expected_path
            or any(parameters.get(key) != [value] for key, value in required.items())):
        raise InputError(f"manifest request URL does not verify {venue} {market} {symbol} 1m data")


def _binance_bars(rows: Any, receipt: int | None) -> list[MinuteBar]:
    if not isinstance(rows, list):
        raise InputError("Binance snapshot must be a list")
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 11:
            raise InputError("invalid Binance kline row")
        result.append(MinuteBar(ts=_timestamp(row[0], "kline ts"), open=_number(row[1], "open"), high=_number(row[2], "high"), low=_number(row[3], "low"), close=_number(row[4], "close"), quote_volume=_number(row[7], "quote_volume"), taker_buy_quote=_number(row[10], "taker_buy_quote"), received_ts=receipt))  # type: ignore[arg-type]
    return sorted(result, key=lambda bar: bar.ts)


def _bybit_bars(payload: Any, receipt: int | None) -> list[MinuteBar]:
    try:
        rows = payload["result"]["list"]
    except (TypeError, KeyError) as exc:
        raise InputError("invalid Bybit kline payload") from exc
    if not isinstance(rows, list):
        raise InputError("invalid Bybit kline list")
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise InputError("invalid Bybit kline row")
        # Bybit returns newest-first; quote turnover is row[6].
        result.append(MinuteBar(ts=_timestamp(row[0], "kline ts"), open=_number(row[1], "open"), high=_number(row[2], "high"), low=_number(row[3], "low"), close=_number(row[4], "close"), quote_volume=_number(row[6], "quote_volume"), received_ts=receipt))  # type: ignore[arg-type]
    return sorted(result, key=lambda bar: bar.ts)


def load_archive(path: str | Path, coin: str) -> tuple[MarketSeries, MarketSeries, str]:
    """Load one historical archive; no cross-archive merging is performed."""
    archive = Path(path)
    symbol = coin.upper()
    if symbol not in {"BTC", "ETH", "HYPE"}:
        raise InputError("coin must be BTC, ETH, or HYPE")
    manifest_path = archive.with_name("sources.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with ZipFile(archive) as zipped:
            names = set(zipped.namelist())
            # The verification archive uses generic names.  The original report
            # keeps exchange-qualified names.  Both are accepted only verbatim.
            spot_name = next((n for n in (f"{symbol}_spot_1m.json", f"{symbol}_binance_spot_1m.json") if n in names), None)
            perp_name = next((n for n in (f"{symbol}_perp_1m.json", f"{symbol}_binance_perp_1m.json") if n in names), None)
            if not spot_name or not perp_name:
                raise InputError("archive lacks the required spot and perp 1m snapshots")
            spot_bytes = zipped.read(spot_name)
            perp_bytes = zipped.read(perp_name)
            spot_raw = json.loads(spot_bytes)
            perp_raw = json.loads(perp_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read archive or companion sources.json: {exc}") from exc
    except Exception as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError(f"cannot read archive: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise InputError("sources.json must be an object")
    spot_request = Path(spot_name).stem
    perp_request = Path(perp_name).stem
    spot_venue = "bybit" if symbol == "HYPE" and spot_name == f"{symbol}_spot_1m.json" else "binance"
    spot_receipt = _manifest_receipt(manifest, spot_request, spot_name, spot_bytes,
                                     coin=symbol, venue=spot_venue, market="spot")
    perp_receipt = _manifest_receipt(manifest, perp_request, perp_name, perp_bytes,
                                     coin=symbol, venue="binance", market="perp")
    spot_bars = _bybit_bars(spot_raw, spot_receipt) if spot_venue == "bybit" else _binance_bars(spot_raw, spot_receipt)
    perp_bars = _binance_bars(perp_raw, perp_receipt)
    if not spot_bars or not perp_bars:
        raise InputError("archive contains no bars")
    return (MarketSeries(spot_venue, "spot", f"{symbol}USDT", spot_bars),
            MarketSeries("binance", "perp", f"{symbol}USDT", perp_bars), sha256_file(archive))


def serializable(value: Any) -> Any:
    """Turn engine dataclasses/tuples into JSON-friendly values."""
    if hasattr(value, "__dataclass_fields__"):
        return {key: serializable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value
