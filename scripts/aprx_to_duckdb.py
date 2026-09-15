"""Checkout entry point for the ArcGIS Pro terminal exporter.

Install the checkout first: python -m pip install -e ".[duckdb]"
"""

from layers_json.aprx_to_duckdb import main

if __name__ == "__main__":
    raise SystemExit(main())
