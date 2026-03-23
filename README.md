Python CLI for downloading invoice PDFs and monthly reconciliation CSV reports from Stripe.

## Install

```bash
uv tool install https://github.com/alyf-de/download_stripe_data.git
```

Upgrade:

```bash
uv tool upgrade download-stripe-invoices
```

## Configure

Run the interactive setup command:

```bash
stripe-helper setup
```

It prompts for `TIMEZONE` and `STRIPE_API_KEY`, then writes `~/.download-stripe-invoices/.env`:

```env
TIMEZONE=Europe/Berlin
STRIPE_API_KEY=rk_live_***
```

Environment variables override values from the file.

## Usage

```bash
stripe-helper download 01/2025
```

This saves invoices and two CSV reports into the current directory by default.

The exported CSVs include:

- Stripe's `balance.summary.1` monthly balance summary, so the starting and ending Stripe balance is visible
- Stripe's `balance_change_from_activity.itemized.7` payment reconciliation export, combining `reporting_category=charge` and `reporting_category=refund`, so customer payments and refunds can be matched against receivables from the downloaded invoices

This is intended to be run once per month for the previous month.

Pass a target folder as the second argument to save somewhere else:

```bash
stripe-helper download 01/2025 "~/Downloads"
```

The local Python entry point still works:

```bash
python main.py setup
python main.py download 01/2025
python main.py download 01/2025 "~/Downloads"
```
