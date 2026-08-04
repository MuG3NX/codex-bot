# Zero-Capital Grid Research Lab

Clean-slate research repository for validating a Pionex futures-grid hypothesis **without trading capital or private API keys**.

Current phase: **Phase 2 — public market-data probe**.

The probe records public SKHX futures trades, order-book updates, index/mark/funding messages, and REST snapshots. It contains no order-placement code.

## Locked safety boundary

- Capital used: **0**
- API keys used: **0**
- Order endpoints: **none**
- Private account data: **none**

## Run the 30-second probe

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python collector.py --config config.json --probe
python validate_data.py data
```

A GitHub Actions run starts automatically after probe files are pushed. Its artifact contains raw NDJSON and a validation report.

Official references:

- https://www.pionex.com/docs/api-docs/futures-websocket/public-stream
- https://www.pionex.com/docs/api-docs/futures-websocket/general-info/connection-endpoints
- https://www.pionex.com/docs/api-docs/futures-api/common
- https://www.pionex.com/docs/api-docs/futures-api/market
