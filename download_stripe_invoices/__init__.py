"""Download Stripe invoices CLI."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import subprocess

__all__ = ["__version__"]


def _get_version() -> str:
    try:
        return version("download-stripe-invoices")
    except PackageNotFoundError:
        repo_root = Path(__file__).resolve().parent.parent
        try:
            completed = subprocess.run(
                ["git", "describe", "--tags", "--dirty", "--always"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment specific.
            return "0+unknown"

        return completed.stdout.strip()


__version__ = _get_version()
