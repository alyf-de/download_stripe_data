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

Create a `.env` file in the directory where you run the command:

```env
TIMEZONE=Europe/Berlin
STRIPE_API_KEY=rk_live_***
```

You can also point to a different file with `--env-file`, and environment variables override values from the file.

## Usage

```bash
download-stripe-invoices 01/2025
```

Optional flags:

```bash
download-stripe-invoices 01/2025 --env-file "/path/to/.env" --output-dir "~/Downloads"
```

The legacy local entry point still works:

```bash
python main.py 01/2025
```
