Python CLI for downloading invoice PDFs and reports from Stripe.

## Install

```bash
uv tool install .
```

or

```bash
pipx install .
```

## Configure

Run the interactive setup command:

```bash
download-stripe-invoices setup
```

It prompts for `TIMEZONE` and `STRIPE_API_KEY`, then writes `~/.download-stripe-invoices/.env`:

```env
TIMEZONE=Europe/Berlin
STRIPE_API_KEY=rk_live_***
```

Environment variables override values from the file.

## Usage

```bash
download-stripe-invoices 01/2025
```

Optional flags:

```bash
download-stripe-invoices 01/2025 --output-dir "~/Downloads"
```

The legacy local entry point still works:

```bash
python main.py setup
python main.py 01/2025
```
