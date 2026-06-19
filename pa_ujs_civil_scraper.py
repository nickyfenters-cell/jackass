#!/usr/bin/env python3
"""Scrape Pennsylvania UJS civil case docket PDFs for cases filed on a date.

This CLI drives the public UJS Portal with Playwright because the portal is a
JavaScript application. It searches by Date Filed, filters to Common Pleas civil
cases when the portal exposes those filters, downloads each available PDF docket
sheet, extracts text with pypdf, and writes structured JSON/CSV outputs.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader
from playwright.sync_api import Download, Page, TimeoutError, sync_playwright

BASE_URL = "https://ujsportal.pacourts.us/casesearch"
PDF_BUTTON_RE = re.compile(r"(docket\s*sheet|pdf|print)", re.I)
DOCKET_RE = re.compile(r"\b(?:CP|MJ|MC|MD|AP)-\d{2}-[A-Z0-9-]+-\d{4}\b")
MONEY_RE = re.compile(r"\$\s?[\d,]+(?:\.\d{2})?")
DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")


@dataclass
class CivilCase:
    docket_number: str = ""
    caption: str = ""
    county: str = ""
    court: str = ""
    case_type: str = ""
    filing_date: str = ""
    status: str = ""
    plaintiffs: list[str] = field(default_factory=list)
    defendants: list[str] = field(default_factory=list)
    attorneys: list[str] = field(default_factory=list)
    docket_entries: list[dict[str, str]] = field(default_factory=list)
    amounts: list[str] = field(default_factory=list)
    pdf_path: str = ""
    source_url: str = ""
    raw_text_path: str = ""


def mmddyyyy(value: str) -> str:
    """Validate an ISO or US date and return UJS' MM/DD/YYYY format."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%m/%d/%Y")
        except ValueError:
            pass
    raise argparse.ArgumentTypeError("date must be YYYY-MM-DD or MM/DD/YYYY")


def click_if_present(page: Page, label: str, timeout: int = 1500) -> bool:
    locators = [
        page.get_by_label(label, exact=False),
        page.get_by_text(label, exact=True),
        page.locator(f"text=/{re.escape(label)}/i"),
    ]
    for locator in locators:
        try:
            if locator.count() and locator.first.is_visible(timeout=timeout):
                locator.first.click()
                return True
        except Exception:
            continue
    return False


def fill_by_label_or_near_text(page: Page, label: str, value: str) -> None:
    candidates = [
        page.get_by_label(label, exact=False),
        page.locator(f"input[aria-label*='{label}' i]"),
        page.locator(f"xpath=//*[contains(normalize-space(.), '{label}')]/following::input[1]"),
    ]
    for locator in candidates:
        try:
            if locator.count():
                locator.first.fill(value)
                return
        except Exception:
            continue
    raise RuntimeError(f"Could not find an input for {label!r}")


def choose_optional_filter(page: Page, label: str, value: str | None) -> None:
    if not value:
        return
    try:
        page.get_by_label(label, exact=False).select_option(label=value)
        return
    except Exception:
        pass
    try:
        page.get_by_text(value, exact=True).click(timeout=2000)
    except Exception:
        print(f"warning: could not set {label} filter to {value!r}", file=sys.stderr)


def submit_search(page: Page) -> None:
    for name in ("Search", "Submit"):
        try:
            with page.expect_response(lambda r: r.request.method in {"GET", "POST"}, timeout=5000):
                page.get_by_role("button", name=re.compile(name, re.I)).click(timeout=3000)
            return
        except Exception:
            continue
    page.keyboard.press("Enter")


def run_search(page: Page, filed_date: str, county: str | None, headless: bool) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=30000)
    click_if_present(page, "Date Filed")
    # Best-effort civil filters. UJS labels change occasionally; failures are non-fatal.
    click_if_present(page, "Civil")
    click_if_present(page, "Common Pleas")
    fill_by_label_or_near_text(page, "Date Filed Start Date", filed_date)
    fill_by_label_or_near_text(page, "Date Filed End Date", filed_date)
    choose_optional_filter(page, "County", county)
    submit_search(page)
    try:
        page.wait_for_selector("text=/CP-|Docket|No records|Search Results/i", timeout=30000)
    except TimeoutError:
        if headless:
            raise RuntimeError("Search did not finish. Try --headed to handle any UJS interstitial manually.")
        raise


def result_links(page: Page) -> list:
    links = page.locator("a").filter(has_text=re.compile(r"CP-\d{2}|Docket|View", re.I))
    return [links.nth(i) for i in range(links.count())]


def download_current_pdf(page: Page, output_dir: Path, fallback_name: str) -> Path | None:
    buttons = [
        page.get_by_role("link", name=PDF_BUTTON_RE),
        page.get_by_role("button", name=PDF_BUTTON_RE),
        page.locator("a[href*='Pdf'], a[href*='PDF'], a[href*='Report']"),
    ]
    for locator in buttons:
        try:
            if not locator.count():
                continue
            with page.expect_download(timeout=10000) as download_info:
                locator.first.click()
            download: Download = download_info.value
            name = download.suggested_filename or f"{fallback_name}.pdf"
            path = output_dir / re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
            download.save_as(path)
            return path
        except Exception:
            continue
    return None


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in (i.strip(" ;,") for i in items if i and i.strip()):
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def field_after(text: str, *labels: str) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*:?\s*(.+)", text, re.I)
        if match:
            return match.group(1).splitlines()[0].strip()
    return ""


def names_after_heading(text: str, heading: str) -> list[str]:
    match = re.search(rf"{heading}\s*(.*?)(?:\n\s*\n|Defendant|Plaintiff|Attorney|Docket Entries|$)", text, re.I | re.S)
    if not match:
        return []
    lines = [line.strip() for line in match.group(1).splitlines()]
    return unique(line for line in lines if 3 <= len(line) <= 120 and not DATE_RE.search(line))


def parse_docket_entries(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        match = re.match(r"(\d{1,2}/\d{1,2}/\d{4})\s+(.{8,})", line)
        if match:
            entries.append({"date": match.group(1), "description": match.group(2)})
    return entries


def parse_case(text: str, pdf_path: Path, source_url: str, raw_text_path: Path) -> CivilCase:
    docket_match = DOCKET_RE.search(text)
    docket = docket_match.group(0) if docket_match else ""
    caption = field_after(text, "Caption", "Case Caption")
    if not caption:
        caption_match = re.search(r"(.+?\s+v\.\s+.+)", text, re.I)
        caption = caption_match.group(1).strip() if caption_match else ""
    return CivilCase(
        docket_number=docket,
        caption=caption,
        county=field_after(text, "County"),
        court=field_after(text, "Court"),
        case_type=field_after(text, "Case Type", "Type"),
        filing_date=field_after(text, "Filing Date", "Date Filed"),
        status=field_after(text, "Case Status", "Status"),
        plaintiffs=names_after_heading(text, "Plaintiff"),
        defendants=names_after_heading(text, "Defendant"),
        attorneys=unique(re.findall(r"Attorney(?:\s+for[^\n]*)?\s*:?\s*([^\n]+)", text, re.I)),
        docket_entries=parse_docket_entries(text),
        amounts=unique(MONEY_RE.findall(text)),
        pdf_path=str(pdf_path),
        source_url=source_url,
        raw_text_path=str(raw_text_path),
    )


def scrape(args: argparse.Namespace) -> list[CivilCase]:
    output_dir = Path(args.output_dir)
    pdf_dir = output_dir / "pdfs"
    text_dir = output_dir / "text"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)
    cases: list[CivilCase] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        run_search(page, args.date, args.county, not args.headed)
        links = result_links(page)
        for idx, link in enumerate(links[: args.limit or len(links)], start=1):
            try:
                with context.expect_page(timeout=3000) as new_page_info:
                    link.click()
                detail = new_page_info.value
            except Exception:
                link.click()
                detail = page
            detail.wait_for_load_state("networkidle", timeout=20000)
            pdf_path = download_current_pdf(detail, pdf_dir, f"ujs_case_{idx}")
            if not pdf_path:
                print(f"warning: no PDF found for result {idx}", file=sys.stderr)
                continue
            text = extract_pdf_text(pdf_path)
            raw_path = text_dir / f"{pdf_path.stem}.txt"
            raw_path.write_text(text, encoding="utf-8")
            cases.append(parse_case(text, pdf_path, detail.url, raw_path))
            if detail is not page:
                detail.close()
        browser.close()
    return cases


def write_outputs(cases: list[CivilCase], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    data = [asdict(case) for case in cases]
    (output_dir / "cases.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    with (output_dir / "cases.csv").open("w", newline="", encoding="utf-8") as fh:
        fields = ["docket_number", "caption", "county", "court", "case_type", "filing_date", "status", "plaintiffs", "defendants", "attorneys", "amounts", "pdf_path", "source_url", "raw_text_path"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for case in data:
            writer.writerow({key: "; ".join(case[key]) if isinstance(case.get(key), list) else case.get(key, "") for key in fields})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape PA UJS civil case docket PDFs filed on a specific day.")
    parser.add_argument("date", type=mmddyyyy, help="Filing date to scrape, as YYYY-MM-DD or MM/DD/YYYY.")
    parser.add_argument("--county", help="Optional county filter, e.g. Allegheny or Philadelphia.")
    parser.add_argument("--output-dir", default="ujs_output", help="Directory for PDFs, text, JSON, and CSV outputs.")
    parser.add_argument("--limit", type=int, default=0, help="Optional maximum number of results to process.")
    parser.add_argument("--headed", action="store_true", help="Show Chromium so you can handle UJS prompts manually.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = scrape(args)
    write_outputs(cases, Path(args.output_dir))
    print(f"saved {len(cases)} cases to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
