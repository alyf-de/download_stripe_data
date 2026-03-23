from __future__ import annotations

import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from getpass import getpass
from io import StringIO
from pathlib import Path
from typing import Annotated, Sequence

import requests
import stripe
import typer
from dotenv import dotenv_values
from pytz import timezone
from stripe import StripeClient

from . import __version__

try:
    StripeError = stripe.StripeError
except AttributeError:  # pragma: no cover - compatibility with older SDK releases.
    from stripe.error import StripeError  # type: ignore[attr-defined]


ENV_FILE = Path("~/.download-stripe-invoices/.env").expanduser()
SUMMARY_REPORT_TYPE = "balance.summary.1"
SUMMARY_REPORT_TITLE = "Saldenübersicht"
SUMMARY_REPORT_PARAMETERS: dict[str, str | list[str]] = {}
PAYMENT_REPORT_TYPE = "balance_change_from_activity.itemized.7"
PAYMENT_REPORT_TITLE = "Zahlungsabgleich"
PAYMENT_REPORT_CATEGORIES = ("charge", "refund")
PAYMENT_REPORT_PARAMETERS = {
    "columns": [
        "available_on",
        "customer_name",
        "invoice_number",
        "payment_method_type",
        "currency",
        "gross",
    ],
}
INVOICE_DOWNLOAD_WORKERS = 4
REQUEST_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Settings:
    timezone_name: str
    api_key: str


@dataclass(frozen=True)
class InvoiceDownloadTask:
    invoice_number: str
    pdf_url: str
    target_path: Path


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode="markdown",
    help=(
        "Download Stripe invoice PDFs and monthly reconciliation CSV reports.\n\n"
        "Use `download` to export a month of invoices, a Stripe balance summary, and a payment reconciliation report."
    ),
)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"stripe-helper {__version__}")
        raise typer.Exit()


def exit_from_error(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1)


@app.callback()
def cli(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = None,
) -> None:
    """CLI entrypoint for stripe-helper."""


@app.command("download")
def download_command(
    year_month: Annotated[
        str,
        typer.Argument(
            metavar="MM/YYYY",
            help="Month to export, for example 01/2025.",
        ),
    ],
    target_folder: Annotated[
        Path | None,
        typer.Argument(
            help="Directory where invoices and reports will be saved. Defaults to the current directory.",
        ),
    ] = None,
) -> None:
    try:
        run(year_month, output_dir=target_folder)
    except (RuntimeError, StripeError, ValueError, requests.RequestException) as exc:
        exit_from_error(exc)


@app.command()
def setup() -> None:
    """Create or update the local Stripe configuration file."""

    try:
        run_setup()
    except (RuntimeError, StripeError, ValueError, requests.RequestException) as exc:
        exit_from_error(exc)


def main(argv: Sequence[str] | None = None) -> None:
    argv = list(argv) if argv is not None else sys.argv[1:]
    app(args=argv, prog_name="stripe-helper")


def run(year_month: str, output_dir: Path | None = None) -> None:
    settings = load_settings()
    interval_start, interval_end = get_month_bounds(year_month, settings.timezone_name)
    output_dir = output_dir.expanduser() if output_dir else Path.cwd()
    output_dir.mkdir(parents=True, exist_ok=True)

    # The monthly invoice export and both report exports are independent, so run them together.
    with ThreadPoolExecutor(max_workers=3) as executor:
        invoice_future = executor.submit(
            download_invoices,
            interval_start=interval_start,
            interval_end=interval_end,
            settings=settings,
            output_dir=output_dir,
        )
        summary_report_future = executor.submit(
            download_report,
            report_type=SUMMARY_REPORT_TYPE,
            report_title=SUMMARY_REPORT_TITLE,
            report_parameters=SUMMARY_REPORT_PARAMETERS,
            interval_start=interval_start,
            interval_end=interval_end,
            settings=settings,
            output_dir=output_dir,
        )
        payment_report_future = executor.submit(
            download_payment_report,
            report_type=PAYMENT_REPORT_TYPE,
            report_title=PAYMENT_REPORT_TITLE,
            report_parameters=PAYMENT_REPORT_PARAMETERS,
            interval_start=interval_start,
            interval_end=interval_end,
            settings=settings,
            output_dir=output_dir,
        )

        invoice_count = invoice_future.result()
        summary_report_path = summary_report_future.result()
        payment_report_path = payment_report_future.result()

    print(f"Downloaded {invoice_count} invoice(s) to {output_dir}")
    print(f"Saved balance summary report to {summary_report_path}")
    print(f"Saved payment reconciliation report to {payment_report_path}")


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


def get_month_bounds(year_month: str, tz_name: str) -> tuple[int, int]:
    try:
        month, year = map(int, year_month.split("/", maxsplit=1))
    except ValueError as exc:
        raise ValueError("Expected month in MM/YYYY format, for example 01/2025.") from exc

    if month < 1 or month > 12:
        raise ValueError("Month must be between 01 and 12.")

    from_date = date(year, month, 1)
    until_date = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    tzinfo = get_timezone(tz_name)
    from_datetime = tzinfo.localize(datetime.combine(from_date, datetime.min.time()))
    until_datetime = tzinfo.localize(datetime.combine(until_date, datetime.min.time()))

    return int(from_datetime.timestamp()), int(until_datetime.timestamp())


def get_timezone(tz_name: str):
    try:
        return timezone(tz_name)
    except Exception as exc:  # pragma: no cover - delegated library validation.
        raise ValueError(f"Unknown timezone {tz_name!r}.") from exc


def download_invoices(
    interval_start: int,
    interval_end: int,
    settings: Settings,
    output_dir: Path,
) -> int:
    client = StripeClient(settings.api_key)
    tzinfo = get_timezone(settings.timezone_name)
    futures = []

    with ThreadPoolExecutor(max_workers=INVOICE_DOWNLOAD_WORKERS) as executor:
        for invoice in client.v1.invoices.list(
            params={
                "created": {
                    "gte": interval_start,
                    "lt": interval_end,
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

            company_name = invoice.get("account_name") or "Unknown company"
            customer_name = sanitize_filename(invoice.get("customer_name") or "Unknown customer")
            invoice_number = sanitize_filename(invoice.get("number") or invoice["id"])
            invoice_isodate = datetime.fromtimestamp(invoice_timestamp, tzinfo).date().isoformat()
            file_name = (
                f"{invoice_isodate} {company_name} - {customer_name} - Rechnung {invoice_number}.pdf"
            )
            task = InvoiceDownloadTask(
                invoice_number=invoice_number,
                pdf_url=pdf_url,
                target_path=output_dir / file_name,
            )
            futures.append(executor.submit(download_invoice_file, task))

        return sum(future.result() for future in as_completed(futures))


def download_invoice_file(task: InvoiceDownloadTask) -> int:
    print(f"Downloading invoice {task.invoice_number}")
    response = requests.get(task.pdf_url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    task.target_path.write_bytes(response.content)
    return 1


def download_report(
    report_type: str,
    report_title: str,
    report_parameters: dict[str, str | list[str]],
    interval_start: int,
    interval_end: int,
    settings: Settings,
    output_dir: Path,
) -> Path:
    report_content = fetch_report_content(
        report_type=report_type,
        report_parameters=report_parameters,
        interval_start=interval_start,
        interval_end=interval_end,
        settings=settings,
    )
    target_path = build_report_path(
        report_title=report_title,
        interval_start=interval_start,
        settings=settings,
        output_dir=output_dir,
    )
    target_path.write_bytes(report_content)
    return target_path


def download_payment_report(
    report_type: str,
    report_title: str,
    report_parameters: dict[str, str | list[str]],
    interval_start: int,
    interval_end: int,
    settings: Settings,
    output_dir: Path,
) -> Path:
    headers: list[str] | None = None
    rows: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=len(PAYMENT_REPORT_CATEGORIES)) as executor:
        category_futures = {
            category: executor.submit(
                fetch_report_content,
                report_type=report_type,
                report_parameters={**report_parameters, "reporting_category": category},
                interval_start=interval_start,
                interval_end=interval_end,
                settings=settings,
                report_label=category,
            )
            for category in PAYMENT_REPORT_CATEGORIES
        }

    for category in PAYMENT_REPORT_CATEGORIES:
        category_report_content = category_futures[category].result()
        category_headers, category_rows = parse_csv_report(category_report_content)
        if headers is None:
            headers = category_headers
        elif headers != category_headers:
            raise RuntimeError("Payment report columns did not match between charges and refunds.")

        rows.extend(category_rows)

    if headers is None:
        raise RuntimeError("Payment report did not return any columns.")

    rows.sort(key=lambda row: tuple((row.get(column) or "") for column in headers))

    target_path = build_report_path(
        report_title=report_title,
        interval_start=interval_start,
        settings=settings,
        output_dir=output_dir,
    )
    write_csv_report(target_path, headers, rows)
    return target_path


def fetch_report_content(
    report_type: str,
    report_parameters: dict[str, str | list[str]],
    interval_start: int,
    interval_end: int,
    settings: Settings,
    report_label: str | None = None,
) -> bytes:
    report_name = f"{report_type} [{report_label}]" if report_label else report_type
    print(f"Creating report {report_name}")
    client = StripeClient(settings.api_key)
    report_run = client.v1.reporting.report_runs.create(
        {
            "report_type": report_type,
            "parameters": build_report_parameters(
                report_parameters=report_parameters,
                interval_start=interval_start,
                interval_end=interval_end,
                timezone_name=settings.timezone_name,
            ),
        }
    )

    while report_run.status != "succeeded":
        if report_run.status == "failed":
            raise RuntimeError(f"Report {report_name} failed")

        print(f"Report {report_name} is {report_run.status}")
        time.sleep(5)
        report_run = client.v1.reporting.report_runs.retrieve(report_run.id)

    print(f"Downloading report {report_name}")
    result_url = report_run.result.url if report_run.result else None
    if not result_url:
        raise RuntimeError(f"Report {report_name} did not provide a download URL.")

    response = requests.get(
        result_url,
        auth=(settings.api_key, ""),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.content


def build_report_parameters(
    report_parameters: dict[str, str | list[str]],
    interval_start: int,
    interval_end: int,
    timezone_name: str,
) -> dict[str, int | str | list[str]]:
    parameters: dict[str, int | str | list[str]] = {
        "interval_start": interval_start,
        "interval_end": interval_end,
        "timezone": timezone_name,
    }
    parameters.update(report_parameters)
    return parameters


def build_report_path(
    report_title: str,
    interval_start: int,
    settings: Settings,
    output_dir: Path,
) -> Path:
    tzinfo = get_timezone(settings.timezone_name)
    month_year = datetime.fromtimestamp(interval_start, tzinfo).strftime("%B %Y")
    nowdate = datetime.now(tzinfo).strftime("%Y-%m-%d")
    file_name = f"{nowdate} Stripe - {report_title} {month_year}.csv"
    return output_dir / file_name


def parse_csv_report(report_content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(StringIO(report_content.decode("utf-8-sig")))
    if reader.fieldnames is None:
        raise RuntimeError("Downloaded report was empty.")

    return reader.fieldnames, list(reader)


def write_csv_report(
    target_path: Path,
    headers: list[str],
    rows: list[dict[str, str]],
) -> None:
    with target_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def sanitize_filename(value: str) -> str:
    cleaned = value.replace("/", " ").replace("\\", " ").replace(":", " ").strip()
    return " ".join(cleaned.split()) or "download"
