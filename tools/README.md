# XAU Tools

## Data pipeline

Run the local XAU data pipeline:

```bash
python3 tools/xau_data_pipeline.py
```

Fast/stable mode, useful when optional sources are slow:

```bash
python3 tools/xau_data_pipeline.py --skip-fred
```

Generate and open the data-only HTML page:

```bash
python3 tools/xau_data_pipeline.py --skip-fred --open-html
```

Strict mode, useful for debugging automation:

```bash
python3 tools/xau_data_pipeline.py --strict
```

The pipeline does four things:

1. Updates normalized long-term data in `python-data-store`.
2. Writes a lightweight run package in `python-data-runs/YYYY-MM-DD/HHMMSS-calgary-data-pipeline`.
3. Computes `indicators.json`, `model-conclusion.json`, and `data-lineage.json`.
4. Writes a data-only HTML page at `data-result.html` inside the run folder and refreshes `python-data-runs/latest-data-result.html`.

Bootstrap the two-year analysis raw dataset:

```bash
python3 tools/bootstrap_analysis_raw_data.py
```

This writes the model raw-input layer under:

```text
python-data-store/analysis-raw/
```

It stores two raw layers:

- `spot/oanda`: two years of `XAU_USD` and `XAG_USD` spot-mid OHLC data for `1h` and `1d`. This is the primary price layer.
- `futures/yahoo`: two years of `GC=F` and `SI=F` futures OHLCV for `1h` and `1d`, with volume labeled as exchange-reported futures contract volume. This is the auxiliary volume layer.

Python output is intentionally isolated from AI-written report pages:

- Python data and calculations: `python-data-store`, `python-data-runs`.
- Codex report HTML pages: `reports`.
- Gemini report HTML pages: `gemini/reports`.

Current automatic sources:

- OANDA `XAU_USD` and `XAG_USD`: primary spot price source after local token setup.
- Yahoo chart data: `GC=F`, `SI=F`, `GLD`, `DX-Y.NYB`, `^GVZ`.
- CFTC legacy metals report: gold commercial positioning.
- FRED: `DFII10`, `T10YIE`, `T5YIE` when the endpoint is reachable.
- Yahoo GLD options chain: GLD options put/call proxy when the endpoint is reachable. A local cache is used when a fresh fetch fails.
- SPDR GLD Historical Archive: official GLD holdings history, used to calculate ETF flow from changes in tonnes held.
- World Gold Council gold ETF flows page: global/regional physically-backed gold ETF holdings and flows.

OANDA setup:

```bash
zsh tools/setup_oanda_token.command
python3 tools/oanda_xauusd_fetch.py
```

The token is stored locally in `secrets/oanda.env`, which is ignored by Git.
If a token was pasted into chat or screenshots, revoke it and create a new one.

Indicator source types shown in the HTML page:

- Calculated from market data: RSI, TD9/CD13, Bollinger %B, VPT/OBV, gold/silver ratio.
  Price-based calculations use `XAU_USD` spot. VPT/OBV uses `XAU_USD` spot price with `GC=F` futures volume as the current true-volume proxy.
- Direct market fetch: DXY, GVZ.
- External published data: 10Y real yield, COT, GLD options P/C proxy, BEI, WGC global ETF flow, GLD ETF Flow.
- Not automated in this first version: WGC central-bank gold.

Notes:

- Yahoo options P/C is a `GLD` options proxy, not COMEX GC futures options.
- GLD ETF Flow is calculated from official SPDR GLD holdings: current tonnes minus prior trading-day tonnes.
- WGC global ETF flow is pulled from the World Gold Council chart JSON endpoint behind its gold ETF holdings and flows page.
- WGC central-bank gold data is not automated in this first version because direct XLSX downloads may be blocked by the source site.
- The mechanical `model-conclusion.json` is a scoring hint only. The final report interpretation should still be reviewed before publishing.
