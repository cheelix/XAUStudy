# XAU Python Data Runs

This folder is for Python pipeline run outputs only.

It is intentionally separate from AI-generated report pages and earlier generic
data folders:

- Python data and calculations: `python-data-store`, `python-data-runs`.
- Codex report HTML pages: `reports`.
- Gemini report HTML pages: `gemini/reports`.
- Earlier generic folders: `data-store`, `data-runs`.

Each Python run creates one Calgary-time folder:

```text
python-data-runs/YYYY-MM-DD/HHMMSS-calgary-data-pipeline/
```

Each run folder contains:

```text
summary.json
data-result.html
raw/
  yahoo-increment-*.csv
  cftc-deacmxsf.html
computed/
  indicators.json
  model-conclusion.json
  data-lineage.json
```

`python-data-runs/latest-data-result.html` is refreshed after each run and points
to the latest data-only HTML result.

This folder should not copy the full historical market database. Full normalized
history belongs in `python-data-store`.
