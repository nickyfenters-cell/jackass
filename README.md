# PA UJS Civil Case PDF Scraper

This repository includes a Playwright-based scraper for the Pennsylvania Unified Judicial System (UJS) Portal. It searches for civil/Common Pleas cases filed on one specific day, downloads each available docket-sheet PDF, extracts text, and writes structured JSON and CSV output.

## Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python pa_ujs_civil_scraper.py 2026-06-12 --county Cumberland --output-dir ujs_output
```

Useful options:

- `--county`: narrows the UJS search to one Pennsylvania county.
- `--limit`: processes only the first N results while testing.
- `--headed`: opens a visible browser. Use this if UJS displays a prompt or if its page labels change.

## Output

The scraper creates:

- `ujs_output/pdfs/`: downloaded docket-sheet PDFs.
- `ujs_output/text/`: raw extracted PDF text.
- `ujs_output/cases.json`: detailed structured records, including docket entries.
- `ujs_output/cases.csv`: spreadsheet-friendly summary fields.

Each parsed record includes docket number, caption, county, court, case type, filing date, status, parties, attorneys, docket entries, monetary amounts found in the PDF, and local paths to the PDF/text artifacts.

## Notes

The UJS Portal is a JavaScript application and may change labels or require a human-visible browser session. If a headless run cannot find the search controls or PDF buttons, rerun with `--headed` and use the browser window to clear any prompt before the script continues.
