# Analysis Raw Data

This folder stores canonical raw OHLCV inputs for model calculations.

It is intentionally separate from:

- `python-data-store/oanda`: legacy spot reference files from the earlier version.
- `python-data-store/yahoo`: regular pipeline working store.
- `python-data-runs`: per-run output snapshots.

Current dataset:

```text
analysis-raw/
  spot/
    oanda/
      XAU_USD/
        oanda-XAUUSD-2y-1h-ohlc-spot.csv
        oanda-XAUUSD-2y-1d-ohlc-spot.csv
      XAG_USD/
        oanda-XAGUSD-2y-1h-ohlc-spot.csv
        oanda-XAGUSD-2y-1d-ohlc-spot.csv
      gold-silver-ratio/
        oanda-XAUUSD-XAGUSD-ratio-1d.csv
        oanda-XAUUSD-XAGUSD-ratio-1h.csv
  futures/
    yahoo/
      GC=F/
        yahoo-GCF-2y-1h-ohlcv-real-volume.csv
        yahoo-GCF-2y-1d-ohlcv-real-volume.csv
      SI=F/
        yahoo-SIF-2y-1h-ohlcv-real-volume.csv
        yahoo-SIF-2y-1d-ohlcv-real-volume.csv
  manifest.json
```

Volume policy:

- Use `GC=F` and `SI=F` futures volume as `exchange_reported_contract_volume`.
- Do not use OANDA `XAU_USD` or `XAG_USD` volume for VPT/OBV or volume-based model inputs.
- For volume-price indicators, use `XAU_USD` spot price with `GC=F` futures volume as the current proxy.

Price policy:

- `OANDA XAU_USD` is the primary gold price for price-based indicators.
- `OANDA XAG_USD` is the preferred silver price for spot gold/silver ratio.
- `GC=F` and `SI=F` are COMEX futures continuous front-month proxies from Yahoo Finance. They are auxiliary references and may differ from spot quotes.
