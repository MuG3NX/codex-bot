from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def validate(root: Path) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    malformed = 0
    files = sorted(root.glob("*.ndjson"))
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                counts[str(payload.get("source", "unknown"))] += 1
            except Exception:
                malformed += 1

    required_rest = {
        "symbols": counts["rest_symbols"] > 0,
        "trades": counts["rest_trades"] > 0,
        "depth": counts["rest_depth"] > 0,
        "indexes": counts["rest_indexes"] > 0,
        "klines": counts["rest_klines"] > 0,
        "mark_klines": counts["rest_mark_klines"] > 0,
        "funding_rates": counts["rest_funding_rates"] > 0,
    }
    required_ws = {
        "trade": counts["ws_trade"] > 0,
        "depth": counts["ws_depth"] > 0,
        "index": counts["ws_index"] > 0,
    }
    report = {
        "root": str(root.resolve()),
        "files": [path.name for path in files],
        "events": sum(counts.values()),
        "events_by_source": dict(sorted(counts.items())),
        "malformed_lines": malformed,
        "required_rest": required_rest,
        "required_ws": required_ws,
        "errors_logged": counts["errors"],
        "probe_pass": malformed == 0 and all(required_rest.values()) and required_ws["trade"] and required_ws["depth"],
        "strict_pass": malformed == 0 and all(required_rest.values()) and all(required_ws.values()) and counts["errors"] == 0,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir", nargs="?", default="probe_data")
    parser.add_argument("--report", default="validation_report.json")
    args = parser.parse_args()
    report = validate(Path(args.data_dir))
    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["probe_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
