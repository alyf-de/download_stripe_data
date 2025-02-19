import calendar
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
import stripe
from dotenv import dotenv_values
from pytz import timezone
from stripe import StripeClient
from stripe.reporting import ReportRun

config = dotenv_values(".env")
TIMEZONE = config["TIMEZONE"]
API_KEY = config["STRIPE_API_KEY"]

stripe.api_key = API_KEY


def main(year_month: str):
	from_timestamp, to_timestamp = get_timestamps(year_month, TIMEZONE)

	download_invoices(from_timestamp, to_timestamp)
	download_report("balance.summary.1", from_timestamp, to_timestamp)


def get_timestamps(year_month: str, tz: str) -> tuple[int, int]:
	month, year = map(int, year_month.split("/"))

	from_date = date(year, month, 1)
	to_date = date(year, month, calendar.monthrange(year, month)[1])

	tz = timezone(tz)
	from_datetime = tz.localize(datetime.combine(from_date, datetime.min.time()))
	to_datetime = tz.localize(datetime.combine(to_date, datetime.max.time()))

	return int(from_datetime.timestamp()), int(to_datetime.timestamp())


def download_invoices(from_timestamp: int, to_timestamp: int):
	client = StripeClient(API_KEY)
	for invoice in client.invoices.list(
		params={
			"created": {
				"gte": from_timestamp,
				"lte": to_timestamp,
			},
			"limit": 100,
		}
	):
		customer_name = invoice["customer_name"]
		invoice_number = invoice["number"]
		pdf_url = invoice["invoice_pdf"]
		invoice_timestamp = invoice["effective_at"]

		if not pdf_url:
			continue

		invoice_isodate = datetime.fromtimestamp(invoice_timestamp).date().isoformat()
		file_name = f"{invoice_isodate} ALYF GmbH - {customer_name} - Rechnung {invoice_number}.pdf"

		response = requests.get(pdf_url)
		response.raise_for_status()

		target_path = Path("~/Downloads").expanduser() / file_name
		target_path.write_bytes(response.content)


def download_report(report_type: str, from_timestamp: int, to_timestamp: int):
	print(f"Creating report {report_type}")
	report_run = ReportRun.create(
		report_type=report_type,
		parameters={
			"interval_start": from_timestamp,
			"interval_end": to_timestamp,
			"timezone": TIMEZONE,
		},
	)

	while report_run.status != "succeeded":
		if report_run.status == "failed":
			print(f"Report {report_type} failed")
			return

		print(f"Report {report_type} is {report_run.status}")
		report_run = ReportRun.retrieve(report_run.id)
		time.sleep(1)

	print(f"Downloading report {report_type}")
	file_link = stripe.FileLink.create(
		file=report_run.result.id,
	)
	response = requests.get(file_link.url)
	response.raise_for_status()

	report_title = report_type.replace(".", " ").title()

	month_year = datetime.fromtimestamp(to_timestamp).strftime("%B %Y")
	nowdate = datetime.now().strftime("%Y-%m-%d")
	file_name = f"{nowdate} - Stripe - {report_title} - {month_year}.csv"

	target_path = Path("~/Downloads").expanduser() / file_name
	target_path.write_bytes(response.content)


if __name__ == "__main__":
	main(sys.argv[1])
