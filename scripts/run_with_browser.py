#!/usr/bin/env python3
"""
scripts/run_with_browser.py
===========================

A single-file launcher that drives the full ledger-agent pipeline against a
folder of bank statements and surfaces the results either to a local web
browser (interactive mode) or to stdout as JSON (AI-agent mode).

Pipeline calls (canonical API in ledger_agent.core.api):
    1. api.import_statements(folder)
    2. api.generate_balance_sheet(fiscal_year)
    3. api.generate_form_1065(fiscal_year)
    4. api.reconcile_year(fiscal_year)

Modes
-----
1. AI-agent mode (CLI flag) — runs the pipeline immediately:

       python scripts/run_with_browser.py --statements /path/to/folder \\
           [--year 2024] [--no-browser]

   With ``--no-browser`` the results are written to stdout as JSON so an
   AI agent (or shell script) can pipe / parse them.  Without
   ``--no-browser`` the same results are rendered into an HTML page that
   is opened in the user's default browser; the embedded HTTP server
   then auto-shuts down once the page (and its handful of assets) has
   been delivered.

2. Interactive mode (bare invocation) — opens a form:

       python scripts/run_with_browser.py

   This boots a local web page on 127.0.0.1 (ephemeral port), opens the
   user's default browser to a small form, takes a statements-folder
   path + optional year, runs the pipeline, and renders the results on
   the same page.  A "Re-run" button returns to the form.  The server
   self-terminates after 5 minutes idle.

Constraints honoured
--------------------
* No new dependencies (Flask is not in requirements.txt — using stdlib
  ``http.server`` + ``webbrowser`` only).
* Bound to 127.0.0.1 with an OS-assigned ephemeral port.
* No external network calls, no CDN assets — CSS/JS is inlined.
* Pseudonymisation: this file mentions no real partner/institution
  names.  Run ``scripts/check_doc_redaction.py --strict --all-tracked``
  to verify.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# Make sure we can import the ledger_agent package no matter where this
# script is invoked from (cwd may be anywhere; the script lives in
# scripts/ at the repo root).
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_STATEMENTS_ROOT = REPO_ROOT / "data" / "statements"
IDLE_SHUTDOWN_SECONDS = 5 * 60  # auto-shutdown after 5 min idle

log = logging.getLogger("run_with_browser")


# ── Pipeline orchestration ────────────────────────────────────────────────────

def _default_year() -> Optional[int]:
    """Best-effort: pick the most recent year-shaped folder under
    data/statements/.  Returns None if no year folder is found."""
    if not DEFAULT_STATEMENTS_ROOT.is_dir():
        return None
    years = []
    for child in DEFAULT_STATEMENTS_ROOT.iterdir():
        if child.is_dir() and re.fullmatch(r"\d{4}", child.name):
            years.append(int(child.name))
    return max(years) if years else None


def _coerce(value: Any) -> Any:
    """Make pipeline results JSON-serialisable."""
    if isinstance(value, Decimal):
        # Floats lose precision; strings are safer for tax figures.
        return str(value)
    if is_dataclass(value):
        return {k: _coerce(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def run_pipeline(statements_dir: Path, fiscal_year: int) -> Dict[str, Any]:
    """Run the full pipeline and return a JSON-serialisable result dict.

    Uses the canonical public API:
      - api.import_statements(folder)
      - api.generate_balance_sheet(fiscal_year)
      - api.generate_form_1065(fiscal_year)
      - api.reconcile_year(fiscal_year)
    """
    from ledger_agent.core import api  # imported lazily so --help is fast

    statements_dir = Path(statements_dir).expanduser().resolve()
    if not statements_dir.is_dir():
        raise ValueError(f"Statements folder not found: {statements_dir}")

    import_report = api.import_statements(statements_dir)
    bs = api.generate_balance_sheet(fiscal_year)
    form = api.generate_form_1065(fiscal_year)
    recon = api.reconcile_year(fiscal_year)

    return {
        "year": fiscal_year,
        "statements_dir": str(statements_dir),
        "import": {
            "imported": import_report.imported,
            "skipped": import_report.skipped,
            "failed": import_report.failed,
            "failed_files": list(import_report.failed_files),
            "periods_added": list(import_report.periods_added),
            "ok": import_report.ok,
        },
        "balance_sheet": {
            "entity_name": bs.entity_name,
            "period": bs.period,
            "total_assets": _coerce(bs.total_assets),
            "total_liabilities": _coerce(bs.total_liabilities),
            "total_equity": _coerce(bs.total_equity),
            "net_income": _coerce(bs.net_income),
            "is_balanced": bool(bs.is_balanced),
        },
        "form_1065": _coerce(form),
        "reconcile": {
            "fiscal_year": recon.fiscal_year,
            "matched": recon.matched,
            "unmatched": recon.unmatched,
            "total_transfers": _coerce(recon.total_transfers),
            "issues": list(recon.issues),
            "clean": recon.clean,
        },
    }


# ── HTML rendering (inline CSS, no external assets) ───────────────────────────

_BASE_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 880px; margin: 2em auto; padding: 0 1em; color: #222; }
h1 { margin-bottom: 0.2em; }
.meta { color: #555; margin-bottom: 1.5em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0 2em; } /* # redaction: allow */
th, td { border: 1px solid #ddd; padding: 0.5em 0.75em; text-align: left; }
th { background: #f4f4f4; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.status-ok { color: #1a7f37; font-weight: 600; }
.status-bad { color: #c00; font-weight: 600; }
.btn { display: inline-block; padding: 0.6em 1.2em; background: #1a7f37;
       color: white; border-radius: 4px; text-decoration: none;
       border: none; cursor: pointer; font-size: 1em; }
.btn:hover { background: #155f29; }
form label { display: block; margin: 1em 0 0.25em; font-weight: 600; }
form input[type=text], form input[type=number] {
       width: 100%; padding: 0.5em; font-size: 1em; /* # redaction: allow */
       border: 1px solid #bbb; border-radius: 4px; }
.err { background: #fee; border: 1px solid #c00; padding: 1em;
       border-radius: 4px; color: #800; }
.footer { margin-top: 3em; color: #888; font-size: 0.85em; }
"""


def _form_page(error: Optional[str] = None,
               prefill_dir: str = "",
               prefill_year: str = "") -> str:
    default_year = _default_year()
    if not prefill_year and default_year:
        prefill_year = str(default_year)
    if not prefill_dir:
        prefill_dir = str(DEFAULT_STATEMENTS_ROOT)

    err_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>ledger-agent runner</title>
<style>{_BASE_CSS}</style></head><body>
<h1>ledger-agent runner</h1>
<p class="meta">Local pipeline launcher. Enter a folder of bank statements
and (optionally) a fiscal year, then click Run.</p>
{err_html}
<form method="POST" action="/run">
  <label for="dir">Statements folder (absolute path)</label>
  <input type="text" id="dir" name="dir" value="{html.escape(prefill_dir)}" required>
  <label for="year">Fiscal year</label>
  <input type="number" id="year" name="year" value="{html.escape(prefill_year)}"
         min="1900" max="2999" required>
  <p><button type="submit" class="btn">Run pipeline</button></p>
</form>
<p class="footer">Server is bound to 127.0.0.1 and will shut down automatically
after {IDLE_SHUTDOWN_SECONDS // 60} min idle.</p>
</body></html>"""


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    return html.escape(str(v))


def _results_page(result: Dict[str, Any]) -> str:
    bs = result["balance_sheet"]
    f = result["form_1065"]
    r = result["reconcile"]
    imp = result["import"]

    recon_status = ('<span class="status-ok">CLEAN</span>'
                    if r["clean"] else
                    '<span class="status-bad">ISSUES</span>')
    bal_status = ('<span class="status-ok">BALANCED</span>'
                  if bs["is_balanced"] else
                  '<span class="status-bad">UNBALANCED</span>')

    issues_html = ""
    if r["issues"]:
        items = "".join(f"<li>{_fmt(i)}</li>" for i in r["issues"])
        issues_html = f"<h3>Reconciliation issues</h3><ul>{items}</ul>"

    failed_html = ""
    if imp["failed_files"]:
        items = "".join(f"<li>{_fmt(i)}</li>" for i in imp["failed_files"])
        failed_html = f"<h3>Import failures</h3><ul>{items}</ul>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>ledger-agent results</title>
<style>{_BASE_CSS}</style></head><body>
<h1>Results — fiscal year {_fmt(result['year'])}</h1>
<p class="meta">Statements folder: <code>{_fmt(result['statements_dir'])}</code></p>

<h2>Import summary</h2>
<table>
  <tr><th>Imported</th><td class="num">{_fmt(imp['imported'])}</td></tr>
  <tr><th>Skipped (already ingested)</th><td class="num">{_fmt(imp['skipped'])}</td></tr>
  <tr><th>Failed</th><td class="num">{_fmt(imp['failed'])}</td></tr>
  <tr><th>Periods added</th><td>{_fmt(', '.join(imp['periods_added']) or '—')}</td></tr>
</table>
{failed_html}

<h2>Balance sheet — {_fmt(bs['period'])} ({bal_status})</h2>
<table>
  <tr><th>Total assets</th><td class="num">{_fmt(bs['total_assets'])}</td></tr>
  <tr><th>Total liabilities</th><td class="num">{_fmt(bs['total_liabilities'])}</td></tr>
  <tr><th>Total equity</th><td class="num">{_fmt(bs['total_equity'])}</td></tr>
  <tr><th>Net income (period)</th><td class="num">{_fmt(bs['net_income'])}</td></tr>
</table>

<h2>Form 1065 line totals</h2>
<table>
  <tr><th>Total income</th><td class="num">{_fmt(f['total_income'])}</td></tr>
  <tr><th>Total deductions</th><td class="num">{_fmt(f['total_deductions'])}</td></tr>
  <tr><th>Ordinary business income</th><td class="num">{_fmt(f['ordinary_business_income'])}</td></tr>
  <tr><th>Net short-term capital gain</th><td class="num">{_fmt(f['net_short_term_capital_gain'])}</td></tr>
  <tr><th>Net long-term capital gain</th><td class="num">{_fmt(f['net_long_term_capital_gain'])}</td></tr>
  <tr><th>Dividend income</th><td class="num">{_fmt(f['dividend_income'])}</td></tr>
  <tr><th>Interest income</th><td class="num">{_fmt(f['interest_income'])}</td></tr>
</table>

<h2>Reconciliation — {recon_status}</h2>
<table>
  <tr><th>Matched transfers</th><td class="num">{_fmt(r['matched'])}</td></tr>
  <tr><th>Unmatched transfers</th><td class="num">{_fmt(r['unmatched'])}</td></tr>
  <tr><th>Total transfer volume</th><td class="num">{_fmt(r['total_transfers'])}</td></tr>
</table>
{issues_html}

<p><a href="/" class="btn">Re-run</a></p>
<p class="footer">Server will shut down automatically after
{IDLE_SHUTDOWN_SECONDS // 60} min idle.</p>
</body></html>"""


def _error_page(message: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>ledger-agent error</title>
<style>{_BASE_CSS}</style></head><body>
<h1>Pipeline error</h1>
<div class="err"><pre>{html.escape(message)}</pre></div>
<p><a href="/" class="btn">Back</a></p>
</body></html>"""


# ── HTTP server ───────────────────────────────────────────────────────────────

class _RunnerHandler(BaseHTTPRequestHandler):
    """Tiny request handler — interactive form + one-shot results renderer."""

    # Suppress noisy default access logging; route through ``log`` instead.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("http: " + fmt, *args)

    # The server instance carries shared state (last activity, optional
    # pre-baked result for AI-agent browser mode).
    server: "_RunnerServer"  # type: ignore[assignment]

    def _send_html(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API name)
        self.server.touch()
        parsed = urlparse(self.path)

        # AI-agent browser mode: a single pre-baked result is served once.
        if self.server.preloaded_result is not None and parsed.path in ("/", "/results"):
            self._send_html(HTTPStatus.OK, _results_page(self.server.preloaded_result))
            # Schedule shutdown shortly after delivery so any favicon
            # fetch can still land first.
            self.server.schedule_shutdown(2.0)
            return

        if parsed.path == "/" or parsed.path == "/form":
            self._send_html(HTTPStatus.OK, _form_page())
            return

        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        self.server.touch()
        parsed = urlparse(self.path)
        if parsed.path != "/run":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        fields = parse_qs(raw)
        folder = (fields.get("dir") or [""])[0].strip()
        year_raw = (fields.get("year") or [""])[0].strip()

        try:
            year = int(year_raw)
        except ValueError:
            self._send_html(
                HTTPStatus.BAD_REQUEST,
                _form_page(error=f"Invalid year: {year_raw!r}",
                           prefill_dir=folder, prefill_year=year_raw),
            )
            return

        try:
            result = run_pipeline(Path(folder), year)
        except Exception as exc:  # noqa: BLE001 — surface anything to the UI
            log.exception("Pipeline failed")
            self._send_html(
                HTTPStatus.OK,
                _error_page(f"{type(exc).__name__}: {exc}"),
            )
            return

        self._send_html(HTTPStatus.OK, _results_page(result))


class _RunnerServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with idle-shutdown support."""

    def __init__(self, addr: Tuple[str, int], handler):
        super().__init__(addr, handler)
        self.preloaded_result: Optional[Dict[str, Any]] = None
        self._last_activity = time.monotonic()
        self._shutdown_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def touch(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def schedule_shutdown(self, delay_seconds: float) -> None:
        """Fire shutdown after a short delay from a background thread."""
        def _stop():
            log.info("Shutting down local server")
            self.shutdown()
        with self._lock:
            if self._shutdown_timer is not None:
                self._shutdown_timer.cancel()
            self._shutdown_timer = threading.Timer(delay_seconds, _stop)
            self._shutdown_timer.daemon = True
            self._shutdown_timer.start()

    def start_idle_watcher(self, max_idle: float) -> None:
        """Background poller that shuts the server down after `max_idle`
        seconds with no recorded activity."""
        def _watch():
            while True:
                time.sleep(15)
                with self._lock:
                    idle = time.monotonic() - self._last_activity
                if idle >= max_idle:
                    log.info("Idle timeout reached (%.0fs); shutting down", idle)
                    self.shutdown()
                    return
        t = threading.Thread(target=_watch, daemon=True)
        t.start()


def _make_server(preloaded: Optional[Dict[str, Any]] = None) -> _RunnerServer:
    server = _RunnerServer(("127.0.0.1", 0), _RunnerHandler)
    server.preloaded_result = preloaded
    return server


def _server_url(server: _RunnerServer) -> str:
    # `server_address` after bind contains the OS-assigned port.
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/"


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_with_browser.py",
        description=(
            "Run the ledger-agent pipeline on a folder of bank statements "
            "and view the results in a local browser (or as JSON on stdout)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Interactive: open a form in the browser\n"
            "  python scripts/run_with_browser.py\n\n"
            "  # AI-agent / scripted: print JSON to stdout\n"
            "  python scripts/run_with_browser.py \\\n"
            "      --statements data/statements/2024 --year 2024 --no-browser\n\n"
            "  # Headed: run + open browser to results\n"
            "  python scripts/run_with_browser.py --statements data/statements/2024\n"
        ),
    )
    p.add_argument(
        "--statements",
        type=Path,
        default=None,
        help="Folder of bank-statement PDFs. If omitted, launches interactive form.",
    )
    p.add_argument(
        "--year",
        type=int,
        default=None,
        help="Fiscal year. Defaults to the latest 4-digit year folder under data/statements/.",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="Print JSON results to stdout instead of opening a browser.",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    return p


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # ── Mode 1: AI-agent / one-shot ──────────────────────────────────────────
    if args.statements is not None:
        year = args.year if args.year is not None else _default_year()
        if year is None:
            print(
                "error: --year is required (no year folder found under data/statements/)",
                file=sys.stderr,
            )
            return 2
        try:
            result = run_pipeline(args.statements, year)
        except Exception as exc:  # noqa: BLE001
            log.exception("Pipeline failed")
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        if args.no_browser:
            json.dump(result, sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
            return 0

        # Render the result into a one-shot HTML page and open browser.
        server = _make_server(preloaded=result)
        url = _server_url(server)
        log.info("Serving results at %s", url)
        server.start_idle_watcher(IDLE_SHUTDOWN_SECONDS)
        # Run server in foreground; shutdown is scheduled after page delivery.
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0

    # ── Mode 2: Interactive form ─────────────────────────────────────────────
    server = _make_server(preloaded=None)
    url = _server_url(server)
    log.info("Interactive runner at %s (Ctrl-C to stop)", url)
    server.start_idle_watcher(IDLE_SHUTDOWN_SECONDS)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
