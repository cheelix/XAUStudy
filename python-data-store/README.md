# XAU Python Data Store

This folder is the Python pipeline's long-term local market data store.

It is intentionally separate from AI-generated report pages and earlier generic
data folders:

- Python data and calculations: `python-data-store`, `python-data-runs`.
- XAU analysis archive: `index.html`.
- Earlier generic folders: `data-store`, `data-runs`.

Update rules:

- Append only newly fetched rows when possible.
- De-duplicate by `timestamp_utc`.
- Sort ascending by time after each update.
- Keep instrument roles explicit. XAU analysis uses `OANDA XAU_USD` as the primary gold price. Gold/silver ratio uses `OANDA XAU_USD / XAG_USD` when both are available. Futures/ETF series are auxiliary volume and cross-market references.
- Do not create date-stamped copies of full history here. This folder is the canonical Python local history.

Expected layout:

```text
python-data-store/
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
  yahoo/
    GC=F/
      yahoo-GCF-1d.csv
      yahoo-GCF-1h.csv
    SI=F/
      yahoo-SIF-1d.csv
      yahoo-SIF-1h.csv
    GLD/
      yahoo-GLD-1d.csv
    DX-Y.NYB/
      yahoo-DXYNYB-1d.csv
    ^GVZ/
      yahoo-GVZ-1d.csv
    gold-silver-ratio/
      yahoo-GCF-SIF-ratio-1d.csv
      yahoo-GCF-SIF-ratio-1h.csv
  cftc/
    cftc-gold-commercial-net.csv
  fred/
    DFII10/
      fred-DFII10.csv
    T10YIE/
      fred-T10YIE.csv
    T5YIE/
      fred-T5YIE.csv
  ssga/
    GLD/
      US_GLD_Archive_EN.xlsx
      ssga-GLD-etf-flow.csv
  wgc/
    gold-etf-flows/
      wgc-global-gold-etf-flow.csv
      wgc-global-gold-etf-flow-latest.json
  options/
    GLD/
      yahoo-GLD-options-put-call-latest.json
```

Data role policy:

- `analysis-raw/spot/oanda`: primary XAUUSD/XAGUSD spot price inputs for model calculations. OANDA volume is stored only as broker/platform tick volume and should not be treated as true market volume.
- `analysis-raw/futures/yahoo`: futures OHLCV with exchange-reported contract volume, used as auxiliary volume/cross-market input.
- `oanda`: legacy working folder from earlier spot-reference tests.
- `yahoo`: regular pipeline working store.
- `cftc` and `fred`: externally published macro/positioning inputs.
- `ssga/GLD`: official SPDR GLD holdings archive and normalized ETF flow history.
- `wgc/gold-etf-flows`: World Gold Council global gold ETF holdings and flow summary.
- `options/GLD`: latest successful Yahoo GLD options put/call proxy cache.
