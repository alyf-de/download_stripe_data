from __future__ import annotations

import argparse
import calendar
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from getpass import getpass
from pathlib import Path
from typing import Sequence

import requests
import stripe
from dotenv import dotenv_values
from pytz import timezone
from stripe import StripeClient
from stripe.reporting import ReportRun

from . import __version__

try:
    StripeError = stripe.StripeError
except AttributeError:  # pragma: no cover - compatibility with older SDK releases.
    from stripe.error import StripeError  # type: ignore[attr-defined]


ENV_FILE = Path("~/.download-stripe-invoices/.env").expanduser()
DEFAULT_OUTPUT_DIR = Path("~/Downloads").expanduser()
DEFAULT_REPORT_TYPE = "balance.summary.1"
DEFAULT_REPORT_TITLE = "Saldenübersicht"
REQUEST_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Settings:
    timezone_name: str
    api_key: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="download-stripe-invoices",
        description="Download Stripe invoice PDFs and monthly reports.",
        epilog=f"Other commands:\n  setup    Create or update {ENV_FILE}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "year_month",
        metavar="MM/YYYY",
        help="Month to export, for example 01/2025.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where invoices and reports will be saved.",
    )
    return parser


def build_setup_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="download-stripe-invoices setup",
        description=f"Create or update {ENV_FILE} by prompting for TIMEZONE and STRIPE_API_KEY.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]

    try:
        if argv and argv[0] == "setup":
            build_setup_parser().parse_args(argv[1:])
            run_setup()
        else:
            args = build_parser().parse_args(argv)
            run(args.year_month, output_dir=args.output_dir)
    except (RuntimeError, StripeError, ValueError, requests.RequestException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def run(year_month: str, output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    settings = load_settings()
    from_timestamp, to_timestamp = get_timestamps(year_month, settings.timezone_name)
    output_dir = output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    invoice_count = download_invoices(
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        settings=settings,
        output_dir=output_dir,
    )
    report_path = download_report(
        report_type=DEFAULT_REPORT_TYPE,
        report_title=DEFAULT_REPORT_TITLE,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        settings=settings,
        output_dir=output_dir,
    )

    print(f"Downloaded {invoice_count} invoice(s) to {output_dir}")
    print(f"Saved report to {report_path}")


def load_settings() -> Settings:
    config = dotenv_values(ENV_FILE) if ENV_FILE.is_file() else {}
    timezone_name = os.environ.get("TIMEZONE") or config.get("TIMEZONE")
    api_key = os.environ.get("STRIPE_API_KEY") or config.get("STRIPE_API_KEY")

    missing = []
    if not timezone_name:
        missing.append("TIMEZONE")
    if not api_key:
        missing.append("STRIPE_API_KEY")

    if missing:
        missing_vars = ", ".join(missing)
        raise ValueError(
            f"Missing {missing_vars}. Set them in the environment or in {ENV_FILE}."
        )

    return Settings(timezone_name=timezone_name, api_key=api_key)


def run_setup() -> None:
    config = dotenv_values(ENV_FILE) if ENV_FILE.is_file() else {}

    try:
        timezone_name = prompt_timezone(config.get("TIMEZONE") or "Europe/Berlin")
        api_key = prompt_api_key(config.get("STRIPE_API_KEY"))
    except (EOFError, KeyboardInterrupt) as exc:
        raise ValueError("Setup cancelled.") from exc

    save_settings(Settings(timezone_name=timezone_name, api_key=api_key))
    print(f"Saved configuration to {ENV_FILE}")


def prompt_timezone(default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""

    while True:
        value = input(f"Timezone{suffix}: ").strip() or default
        if not value:
            print("Timezone is required.")
            continue

        try:
            get_timezone(value)
        except ValueError as exc:
            print(exc)
            continue

        return value


def prompt_api_key(default: str | None = None) -> str:
    prompt = "Stripe API key"
    if default:
        prompt += " [leave blank to keep existing]"
    prompt += ": "

    while True:
        value = getpass(prompt).strip()
        if value:
            return value
        if default:
            return default

        print("Stripe API key is required.")


def save_settings(settings: Settings) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(
        f"TIMEZONE={settings.timezone_name}\nSTRIPE_API_KEY={settings.api_key}\n",
        encoding="utf-8",
    )


def get_timestamps(year_month: str, tz_name: str) -> tuple[int, int]:
    try:
        month, year = map(int, year_month.split("/", maxsplit=1))
    except ValueError as exc:
        raise ValueError("Expected month in MM/YYYY format, for example 01/2025.") from exc

    if month < 1 or month > 12:
        raise ValueError("Month must be between 01 and 12.")

    from_date = date(year, month, 1)
    to_date = date(year, month, calendar.monthrange(year, month)[1])
    tzinfo = get_timezone(tz_name)
    from_datetime = tzinfo.localize(datetime.combine(from_date, datetime.min.time()))
    to_datetime = tzinfo.localize(datetime.combine(to_date, datetime.max.time()))

    return int(from_datetime.timestamp()), int(to_datetime.timestamp())


def get_timezone(tz_name: str):
    try:
        return timezone(tz_name)
    except Exception as exc:  # pragma: no cover - delegated library validation.
        raise ValueError(f"Unknown timezone {tz_name!r}.") from exc


def download_invoices(
    from_timestamp: int,
    to_timestamp: int,
    settings: Settings,
    output_dir: Path,
) -> int:
    client = StripeClient(settings.api_key)
    tzinfo = get_timezone(settings.timezone_name)
    downloaded = 0

    for invoice in client.invoices.list(
        params={
            "created": {
                "gte": from_timestamp,
                "lte": to_timestamp,
            },
            "limit": 100,
        }
    ):
        pdf_url = invoice.get("invoice_pdf")
        if not pdf_url:
            continue

        invoice_timestamp = invoice.get("effective_at") or invoice.get("created")
        if not invoice_timestamp:
            continue

        customer_name = sanitize_filename(invoice.get("customer_name") or "Unknown customer")
        invoice_number = sanitize_filename(invoice.get("number") or invoice["id"])
        invoice_isodate = datetime.fromtimestamp(invoice_timestamp, tzinfo).date().isoformat()
        file_name = (
            f"{invoice_isodate} ALYF GmbH - {customer_name} - Rechnung {invoice_number}.pdf"
        )

        response = requests.get(pdf_url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()

        target_path = output_dir / file_name
        target_path.write_bytes(response.content)
        downloaded += 1

    return downloaded


def download_report(
    report_type: str,
    report_title: str,
    from_timestamp: int,
    to_timestamp: int,
    settings: Settings,
    output_dir: Path,
) -> Path:
    print(f"Creating report {report_type}")
    stripe.api_key = settings.api_key
    report_run = ReportRun.create(
        report_type=report_type,
        parameters={
            "interval_start": from_timestamp,
            "interval_end": to_timestamp,
            "timezone": settings.timezone_name,
        },
    )

    while report_run.status != "succeeded":
        if report_run.status == "failed":
            raise RuntimeError(f"Report {report_type} failed")

        print(f"Report {report_type} is {report_run.status}")
        time.sleep(5)
        report_run = ReportRun.retrieve(report_run.id)

    print(f"Downloading report {report_type}")
    file_link = stripe.FileLink.create(file=report_run.result.id)
    response = requests.get(file_link.url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    tzinfo = get_timezone(settings.timezone_name)
    month_year = datetime.fromtimestamp(to_timestamp, tzinfo).strftime("%B %Y")
    nowdate = datetime.now(tzinfo).strftime("%Y-%m-%d")
    file_name = f"{nowdate} Stripe Payments Europe Ltd - {report_title} - {month_year}.csv"

    target_path = output_dir / file_name
    target_path.write_bytes(response.content)
    return target_path


def sanitize_filename(value: str) -> str:
    cleaned = value.replace("/", " ").replace("\\", " ").replace(":", " ").strip()
    return " ".join(cleaned.split()) or "download"
