# Browser runner — `scripts/run_with_browser.py`

A single-file launcher that drives the full pipeline (import → balance sheet
→ Form 1065 → reconcile) and surfaces results to a local browser or to
stdout as JSON.  No new dependencies — uses Python's stdlib HTTP server.

## Interactive mode (humans)

```bash
python scripts/run_with_browser.py
```

Opens a small form on `127.0.0.1` (ephemeral port).  Enter a statements
folder and a year, click Run, and the same page renders the results.  The
server self-terminates after 5 min idle.

## AI-agent / scripted mode

```bash
# Open results in a browser:
python scripts/run_with_browser.py --statements data/statements/2024 --year 2024

# Print JSON to stdout (pipe-friendly):
python scripts/run_with_browser.py --statements data/statements/2024 --no-browser
```

`--year` defaults to the latest 4-digit year folder under `data/statements/`.
