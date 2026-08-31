#!/usr/bin/env python3
"""Extend the temporary recovery artifact with the full public Compass cluster.

Imports the already-reviewed network and normalization helpers from
recover_public_data.py. The script adds older source tweets, longer NQ/MNQ
histories, and a SHA-256 manifest for every recovered file.
"""

from __future__ import annotations

import csv
import hashlib
import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recover_public_data import OUT, best_video_url, fetch_bytes, save_json, utc_epoch

EXTRA_TWEETS = {
    "2085047923738280214": "awl5q8m",
    "2090818486477971839": "13q6r24f",
    "2090820614386765952": "6lxibnk",
    "2090867534882820421": "g0wbwhx",
    "2090872745831592169": "l1yy2uh",
    "2090898006585274753": "30wpaip",
    "2091223202756276674": "19l0koy",
    "2091257512209113574": "5lwzobq",
    "2091900003656647117": "1oh2mz1",
}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, value)


def fetch_tweet(tweet_id: str, token: str, out_dir: Path) -> dict[str, Any]:
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
        raw_path = out_dir / f"{tweet_id}.json"
        raw_path.write_bytes(body)
        data = json.loads(body)
        author = data.get("user", {}).get("screen_name")
        if author and str(author).lower() != "stoicta":
            raise RuntimeError(f"unexpected author: {author}")
        record.update(
            {
                "status": "FETCHED",
                "raw_file": str(raw_path),
                "content_type": headers.get("content-type"),
                "author_handle": author,
                "created_at": data.get("created_at"),
                "text": data.get("text"),
                "media_count": len(data.get("mediaDetails") or []),
            }
        )
        for position, media in enumerate(data.get("mediaDetails") or [], start=1):
            media_type = str(media.get("type") or "unknown")
            if media_type == "photo":
                media_url = media.get("media_url_https")
                if media_url:
                    media_url = f"{media_url}{'&' if '?' in media_url else '?'}name=orig"
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
                path = out_dir / f"{tweet_id}_{position}{suffix}"
                try:
                    content, media_headers = fetch_bytes(media_url, referer="https://x.com/")
                    path.write_bytes(content)
                    media_record.update(
                        {
                            "status": "FETCHED",
                            "file": str(path),
                            "bytes": len(content),
                            "content_type": media_headers.get("content-type"),
                        }
                    )
                except Exception as exc:
                    media_record.update({"status": "ERROR", "error": str(exc)})
            record["media"].append(media_record)
    except Exception as exc:
        record.update({"status": "ERROR", "error": str(exc)})
    return record


def recover_extra_tweets() -> list[dict[str, Any]]:
    out_dir = OUT / "extra_tweets"
    out_dir.mkdir(parents=True, exist_ok=True)
    records = [fetch_tweet(tweet_id, token, out_dir) for tweet_id, token in EXTRA_TWEETS.items()]
    write_json(out_dir / "index.json", records)
    return records


def normalize_chart(payload: dict[str, Any], symbol: str, interval: str, out_dir: Path) -> dict[str, Any]:
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if chart.get("error") or not results:
        raise RuntimeError(f"Yahoo chart error: {chart.get('error')}")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for i, ts in enumerate(timestamps):
        values = {
            "timestamp_utc": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "epoch": ts,
            "open": (quote.get("open") or [None] * len(timestamps))[i],
            "high": (quote.get("high") or [None] * len(timestamps))[i],
            "low": (quote.get("low") or [None] * len(timestamps))[i],
            "close": (quote.get("close") or [None] * len(timestamps))[i],
            "volume": (quote.get("volume") or [None] * len(timestamps))[i],
        }
        if any(values[key] is not None for key in ("open", "high", "low", "close")):
            rows.append(values)

    safe_symbol = symbol.replace("=", "_")
    csv_path = out_dir / f"{safe_symbol}_{interval}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp_utc", "epoch", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)
    meta = result.get("meta") or {}
    return {
        "symbol": symbol,
        "interval": interval,
        "rows": len(rows),
        "csv_file": str(csv_path),
        "exchange_timezone": meta.get("exchangeTimezoneName"),
        "instrument_type": meta.get("instrumentType"),
        "first_timestamp_utc": rows[0]["timestamp_utc"] if rows else None,
        "last_timestamp_utc": rows[-1]["timestamp_utc"] if rows else None,
    }


def recover_extended_market() -> list[dict[str, Any]]:
    out_dir = OUT / "extended_market"
    out_dir.mkdir(parents=True, exist_ok=True)
    start = utc_epoch("2026-08-23T18:00:00Z")
    end = utc_epoch("2026-09-01T08:00:00Z")
    records: list[dict[str, Any]] = []
    for symbol in ("MNQ=F", "NQ=F"):
        for interval in ("5m", "15m", "30m", "60m"):
            params = urllib.parse.urlencode(
                {
                    "period1": start,
                    "period2": end,
                    "interval": interval,
                    "includePrePost": "true",
                    "events": "div,splits,capitalGains",
                }
            )
            encoded = urllib.parse.quote(symbol, safe="")
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?{params}"
            record: dict[str, Any] = {"symbol": symbol, "interval": interval, "url": url, "status": "PENDING"}
            try:
                body, headers = fetch_bytes(url, referer="https://finance.yahoo.com/")
                raw_path = out_dir / f"{symbol.replace('=', '_')}_{interval}.json"
                raw_path.write_bytes(body)
                record.update(
                    {
                        "status": "FETCHED",
                        "raw_file": str(raw_path),
                        "content_type": headers.get("content-type"),
                        **normalize_chart(json.loads(body), symbol, interval, out_dir),
                    }
                )
            except Exception as exc:
                record.update({"status": "ERROR", "error": str(exc)})
            records.append(record)
    write_json(out_dir / "index.json", records)
    return records


def build_file_manifest() -> list[dict[str, Any]]:
    records = []
    for path in sorted(OUT.rglob("*")):
        if not path.is_file() or path.name == "sha256-manifest.json":
            continue
        content = path.read_bytes()
        records.append(
            {
                "path": str(path),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    write_json(OUT / "sha256-manifest.json", records)
    return records


def main() -> int:
    report = {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "extra_tweets": recover_extra_tweets(),
        "extended_market": recover_extended_market(),
    }
    write_json(OUT / "extended-report.json", report)
    manifest = build_file_manifest()
    print(
        json.dumps(
            {
                "extra_tweets": len(report["extra_tweets"]),
                "extended_market": len(report["extended_market"]),
                "files": len(manifest),
                "errors": [
                    r
                    for group in (report["extra_tweets"], report["extended_market"])
                    for r in group
                    if r["status"] == "ERROR"
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
