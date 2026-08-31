#!/usr/bin/env python3
"""Recover public Stoic tweet media and MNQ/NQ intraday bars.

This script is intentionally self-contained and writes only an Actions artifact.
It uses public endpoints and does not require credentials.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUT = Path("recovered")
OUT.mkdir(parents=True, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TWEETS = {
    "2093003673450185138": "xqqcdki",
    "2093004666984341511": "ursqwp3",
    "2093346549883392254": "mwza7su",
    "2091254210129969497": "4nj4ehl",
    "2093350098826637394": "19eettg2",
}


def fetch_bytes(url: str, *, referer: str | None = None, retries: int = 3) -> tuple[bytes, dict[str, str]]:
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
    }
    if referer:
        headers["Referer"] = referer
    last_error: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return response.read(), {k.lower(): v for k, v in response.headers.items()}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def best_video_url(media: dict[str, Any]) -> str | None:
    variants = media.get("video_info", {}).get("variants", []) or []
    mp4 = [v for v in variants if "mp4" in str(v.get("content_type", "")) and v.get("url")]
    if not mp4:
        return None
    mp4.sort(key=lambda v: int(v.get("bitrate") or 0), reverse=True)
    return str(mp4[0]["url"])


def recover_tweets() -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    tweet_dir = OUT / "tweets"
    tweet_dir.mkdir(exist_ok=True)

    for tweet_id, token in TWEETS.items():
        endpoint = (
            "https://cdn.syndication.twimg.com/tweet-result?"
            + urllib.parse.urlencode({"id": tweet_id, "lang": "en", "token": token})
        )
        record: dict[str, Any] = {
            "tweet_id": tweet_id,
            "source_url": f"https://x.com/StoicTA/status/{tweet_id}",
            "syndication_url": endpoint,
            "status": "PENDING",
            "media": [],
        }
        try:
            body, headers = fetch_bytes(endpoint, referer="https://platform.twitter.com/")
            raw_path = tweet_dir / f"{tweet_id}.json"
            raw_path.write_bytes(body)
            data = json.loads(body)
            record.update(
                {
                    "status": "FETCHED",
                    "raw_file": str(raw_path),
                    "content_type": headers.get("content-type"),
                    "author_handle": data.get("user", {}).get("screen_name"),
                    "created_at": data.get("created_at"),
                    "text": data.get("text"),
                    "media_count": len(data.get("mediaDetails") or []),
                }
            )
            if record["author_handle"] and str(record["author_handle"]).lower() != "stoicta":
                raise RuntimeError(f"unexpected author: {record['author_handle']}")

            for position, media in enumerate(data.get("mediaDetails") or [], start=1):
                media_type = str(media.get("type") or "unknown")
                media_url: str | None
                suffix: str
                if media_type == "photo":
                    media_url = media.get("media_url_https")
                    if media_url:
                        separator = "&" if "?" in media_url else "?"
                        media_url = f"{media_url}{separator}name=orig"
                    suffix = Path(urllib.parse.urlparse(str(media_url or "")).path).suffix or ".jpg"
                else:
                    media_url = best_video_url(media)
                    suffix = ".mp4"

                media_record: dict[str, Any] = {
                    "position": position,
                    "type": media_type,
                    "source_url": media_url,
                    "width": (media.get("original_info") or {}).get("width"),
                    "height": (media.get("original_info") or {}).get("height"),
                    "status": "NO_URL" if not media_url else "PENDING",
                }
                if media_url:
                    media_path = tweet_dir / f"{tweet_id}_{position}{suffix}"
                    try:
                        media_bytes, media_headers = fetch_bytes(media_url, referer="https://x.com/")
                        media_path.write_bytes(media_bytes)
                        media_record.update(
                            {
                                "status": "FETCHED",
                                "file": str(media_path),
                                "bytes": len(media_bytes),
                                "content_type": media_headers.get("content-type"),
                            }
                        )
                    except Exception as exc:  # preserve tweet JSON even when CDN download fails
                        media_record.update({"status": "ERROR", "error": str(exc)})
                record["media"].append(media_record)
        except Exception as exc:
            record.update({"status": "ERROR", "error": str(exc)})
        index.append(record)

    save_json(tweet_dir / "index.json", index)
    return index


def utc_epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def normalize_yahoo_chart(payload: dict[str, Any], symbol: str, interval: str, out_dir: Path) -> dict[str, Any]:
    chart = payload.get("chart") or {}
    errors = chart.get("error")
    result_list = chart.get("result") or []
    if errors or not result_list:
        raise RuntimeError(f"Yahoo chart error for {symbol} {interval}: {errors}")
    result = result_list[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for i, ts in enumerate(timestamps):
        row = {
            "timestamp_utc": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "epoch": ts,
            "open": (quote.get("open") or [None] * len(timestamps))[i],
            "high": (quote.get("high") or [None] * len(timestamps))[i],
            "low": (quote.get("low") or [None] * len(timestamps))[i],
            "close": (quote.get("close") or [None] * len(timestamps))[i],
            "volume": (quote.get("volume") or [None] * len(timestamps))[i],
        }
        if any(row[k] is not None for k in ("open", "high", "low", "close")):
            rows.append(row)

    safe_symbol = symbol.replace("=", "_").replace("^", "IDX_")
    csv_path = out_dir / f"{safe_symbol}_{interval}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp_utc", "epoch", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)

    return {
        "symbol": symbol,
        "interval": interval,
        "rows": len(rows),
        "csv_file": str(csv_path),
        "exchange_timezone": (result.get("meta") or {}).get("exchangeTimezoneName"),
        "instrument_type": (result.get("meta") or {}).get("instrumentType"),
        "first_timestamp_utc": rows[0]["timestamp_utc"] if rows else None,
        "last_timestamp_utc": rows[-1]["timestamp_utc"] if rows else None,
    }


def recover_market_data() -> list[dict[str, Any]]:
    market_dir = OUT / "market"
    market_dir.mkdir(exist_ok=True)
    start = utc_epoch("2026-08-30T18:00:00Z")
    end = utc_epoch("2026-09-01T08:00:00Z")
    index: list[dict[str, Any]] = []

    for symbol in ("MNQ=F", "NQ=F"):
        for interval in ("1m", "5m"):
            params = urllib.parse.urlencode(
                {
                    "period1": start,
                    "period2": end,
                    "interval": interval,
                    "includePrePost": "true",
                    "events": "div,splits,capitalGains",
                }
            )
            encoded_symbol = urllib.parse.quote(symbol, safe="")
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}?{params}"
            record: dict[str, Any] = {"symbol": symbol, "interval": interval, "url": url, "status": "PENDING"}
            try:
                body, headers = fetch_bytes(url, referer="https://finance.yahoo.com/")
                safe_symbol = symbol.replace("=", "_")
                raw_path = market_dir / f"{safe_symbol}_{interval}.json"
                raw_path.write_bytes(body)
                normalized = normalize_yahoo_chart(json.loads(body), symbol, interval, market_dir)
                record.update(
                    {
                        "status": "FETCHED",
                        "raw_file": str(raw_path),
                        "content_type": headers.get("content-type"),
                        **normalized,
                    }
                )
            except Exception as exc:
                record.update({"status": "ERROR", "error": str(exc)})
            index.append(record)

    save_json(market_dir / "index.json", index)
    return index


def main() -> int:
    report = {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "tweets": recover_tweets(),
        "market": recover_market_data(),
    }
    save_json(OUT / "report.json", report)
    failures = [r for group in (report["tweets"], report["market"]) for r in group if r["status"] == "ERROR"]
    print(json.dumps({"tweet_records": len(report["tweets"]), "market_records": len(report["market"]), "failures": failures}, indent=2))
    # Upload partial evidence even if one endpoint fails; the report preserves every error.
    return 0


if __name__ == "__main__":
    sys.exit(main())
