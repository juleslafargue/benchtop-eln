import sys

if sys.version_info < (3, 11):
    raise SystemExit(
        f"Benchtop needs Python 3.11 or newer; this is Python {sys.version.split()[0]}.\n"
        "Install a current Python (e.g. from python.org), then recreate the environment:\n"
        "  rm -rf .venv && python3.13 -m venv .venv && source .venv/bin/activate "
        "&& pip install -r requirements.txt"
    )

__version__ = "0.27.0"
