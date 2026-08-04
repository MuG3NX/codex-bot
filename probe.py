from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RawWriter:
    def __init__(self, root: Path, symbol: str) -> None:
        self.root = root
        self.symbol = symbol
        self.counts: Counter[str] = Counter()
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, source: str, payload: Any, **meta: Any) -> None:
        envelope = {
            "received_at": now_iso(),
            "source": source,
            "symbol": self.symbol,
            **meta,
            "payload": payload,
        }
        with (self.root / f"{source}.ndjson").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        self.counts[source] += 1


async def get_json(session: aiohttp.ClientSession, base: str, path: str, params: dict[str, Any] | None = None) -> tuple[int, Any, str]:
    async with session.get(f"{base}{path}", params=params, timeout=aiohttp.ClientTimeout(total=25)) as response:
        text = await response.text()
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError:
            payload = {"raw_text": text}
        return response.status, payload, str(response.url)


def discover_symbol(payload: Any, requested: str) -> tuple[str, list[str]]:
    candidates: list[str] = []
    if isinstance(payload, dict):
        data = payload.get("data")
        symbols = data.get("symbols", []) if isinstance(data, dict) else []
        for item in symbols if isinstance(symbols, list) else []:
            if isinstance(item, dict):
                symbol = item.get("symbol")
                if isinstance(symbol, str) and "SKHX" in symbol.upper():
                    candidates.append(symbol.upper())
    candidates = sorted(set(candidates))
    requested_u = requested.upper()
    if requested_u in candidates:
        return requested_u, candidates
    if candidates:
        perp = [item for item in candidates if item.endswith("_PERP")]
        return (perp or candidates)[0], candidates
    return requested_u, candidates


async def rest_probe(session: aiohttp.ClientSession, base: str, writer: RawWriter) -> None:
    endpoints: list[tuple[str, str, dict[str, Any]]] = [
        ("symbols", "/api/v1/common/symbols", {"type": "PERP"}),
        ("risk_table", "/api/v1/common/riskTable", {}),
        ("trades", "/api/v1/market/trades", {"symbol": writer.symbol, "limit": 500}),
        ("depth", "/api/v1/market/depth", {"symbol": writer.symbol, "limit": 100}),
        ("ticker", "/api/v1/market/tickers", {"symbol": writer.symbol}),
        ("book_ticker", "/api/v1/market/bookTicker", {"symbol": writer.symbol}),
        ("indexes", "/api/v1/market/indexes", {"symbol": writer.symbol}),
        ("klines", "/api/v1/market/klines", {"symbol": writer.symbol, "interval": "1M", "limit": 500}),
        ("mark_klines", "/api/v1/market/markKlines", {"symbol": writer.symbol, "interval": "1M", "limit": 500}),
        ("index_klines", "/api/v1/market/indexKlines", {"symbol": writer.symbol, "interval": "1M", "limit": 500}),
        ("funding_rates", "/api/v1/market/fundingRates", {"symbol": writer.symbol, "limit": 500}),
        ("open_interests", "/api/v1/market/openInterests", {}),
    ]
    for name, path, params in endpoints:
        try:
            status, payload, url = await get_json(session, base, path, params)
            writer.write(f"rest_{name}", payload, http_status=status, request_url=url)
        except Exception as exc:
            writer.write("errors", {"stage": "rest", "endpoint": name, "error": repr(exc)})


async def ws_probe(session: aiohttp.ClientSession, ws_url: str, writer: RawWriter, duration: float, depth_limit: int) -> None:
    subscriptions = [
        {"op": "SUBSCRIBE", "topic": "TRADE", "symbol": writer.symbol},
        {"op": "SUBSCRIBE", "topic": "DEPTH", "symbol": writer.symbol, "limit": depth_limit},
        {"op": "SUBSCRIBE", "topic": "INDEX", "symbol": writer.symbol},
    ]
    deadline = asyncio.get_running_loop().time() + duration
    try:
        async with session.ws_connect(ws_url, heartbeat=20, receive_timeout=15, autoping=True) as ws:
            writer.write("status", {"event": "ws_connected", "url": ws_url})
            for message in subscriptions:
                await ws.send_json(message)
                writer.write("status", {"event": "subscribed", "message": message})

            while asyncio.get_running_loop().time() < deadline:
                timeout = max(0.1, min(5.0, deadline - asyncio.get_running_loop().time()))
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue

                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload: Any = json.loads(msg.data)
                    except json.JSONDecodeError:
                        payload = {"raw_text": msg.data}
                    if isinstance(payload, dict) and "ping" in payload:
                        await ws.send_json({"pong": payload["ping"]})
                        writer.write("status", {"event": "pong", "value": payload["ping"]})
                        continue
                    topic = str(payload.get("topic", "OTHER")).lower() if isinstance(payload, dict) else "other"
                    writer.write(f"ws_{topic}", payload)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    writer.write("ws_binary", {"hex": msg.data.hex()})
                elif msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                    writer.write("errors", {"stage": "websocket", "message_type": str(msg.type), "exception": repr(ws.exception())})
                    break
    except Exception as exc:
        writer.write("errors", {"stage": "websocket_connect", "error": repr(exc)})


async def run(config_path: Path) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    base = str(config.get("rest_base", "https://api.pionex.com")).rstrip("/")
    ws_url = str(config.get("ws_url", "wss://ws.pionex.com/wsPub"))
    requested = str(config.get("symbol", "SKHX_USDT_PERP"))
    output = Path(config.get("output_dir", "probe_data"))
    duration = float(config.get("duration_seconds", 35))
    depth_limit = int(config.get("depth_limit", 100))

    headers = {"User-Agent": "zero-capital-grid-research/1.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        status, symbols_payload, symbols_url = await get_json(session, base, "/api/v1/common/symbols", {"type": "PERP"})
        symbol, candidates = discover_symbol(symbols_payload, requested)
        writer = RawWriter(output, symbol)
        writer.write("rest_symbols", symbols_payload, http_status=status, request_url=symbols_url, candidates=candidates)
        writer.write("status", {
            "event": "probe_started",
            "requested_symbol": requested,
            "resolved_symbol": symbol,
            "duration_seconds": duration,
            "capital_used": 0,
            "api_key_used": False,
        })
        await asyncio.gather(
            rest_probe(session, base, writer),
            ws_probe(session, ws_url, writer, duration, depth_limit),
        )
        writer.write("status", {"event": "probe_finished", "counts": dict(writer.counts)})

    summary = {
        "finished_at": now_iso(),
        "requested_symbol": requested,
        "resolved_symbol": symbol,
        "candidate_symbols": candidates,
        "event_counts": dict(sorted(writer.counts.items())),
        "capital_used": 0,
        "api_key_used": False,
    }
    (output / "probe_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Public Pionex futures probe: no key, no capital, no order endpoints.")
    parser.add_argument("--config", default="config.example.json")
    args = parser.parse_args()
    return asyncio.run(run(Path(args.config)))


if __name__ == "__main__":
    raise SystemExit(main())
