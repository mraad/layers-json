# Project development guide

Read `README.md` for public usage and `scripts/README.md` for bulk-loader policies.

## Architecture

- `layers_json/catalog.py` owns the standard-library catalog models, atomic JSON
  writer, XML guard, settings, and query-hint transformations.
- `Layers.pyt` imports that module. `PrepareTool` extends `CatalogBuilder` with
  ArcPy reading and UI. Keep ArcPy imports out of the shared catalog module.
- `layers_from_aprx.py` reads CIM and GDAL and calls the same catalog code.
  `okf_from_aprx.py` reuses that reader and writes Markdown. Neither loads `.pyt`.
- `duckdb_export.py` owns Arrow types/conversion, identifier quoting, and DuckDB
  batch writes/indexes. The Pro toolbox and `aprx_to_duckdb.py` both call it.
- `scripts/aprx_to_duckdb.py` is a wrapper for the installed `layers-duckdb` entry
  point. Do not duplicate the implementation in the wrapper.
- `hide_update.py` owns Hide/Update configuration, selection, field rules, and the
  field classification policy (`build_schema`: OID/shape/subtype stay visible;
  area/length/GlobalID and blob/GUID/raster/XML fields are hidden). Both
  `Layers.pyt` and `hide_update_aprx.py` pass raw names and types to it; neither
  calls the other. Keep ArcPy and GDAL out of the shared rule engine.
- `layers.py` is the optional Pydantic read model. CLI imports must not require it.
- `tests/toolbox_support.py` loads adapters with ArcPy doubles for unit tests only.

## Preserve these behaviors

Catalog serialization includes empty values and preserves field order. Service
layers omit `table_name`. XML parsing rejects DTD and entity declarations and
raises `ValueError` for malformed input, so callers handle one exception type.
`CatalogBuilder.message` is the only progress hook; the CLI leaves it a no-op.
Readers use layer aliases from CIM, not just feature-class names. Reuse parsed
metadata and open each source once. Catalog columns without values are pruned.

The active-map DuckDB toolbox excludes hidden/system fields and uses strict table
names matching the catalog. The full-project exporter preserves attributes and
suffixes name collisions; `_export_layers` records the mapping. Both use WKB,
explicit Arrow temporal types, primary keys, and spatial indexes. Keep these
scope policies in their adapters and the conversion logic shared.

The toolbox replaces each table in a transaction. The full-project exporter
stages a database beside its destination and publishes only on success. The
hide/update CLI similarly writes a new sibling `.aprx`; preserve the original.

Toolboxes ship inside `layers_json/toolboxes` and resolve their sibling package
from the checkout or wheel. Pro disables user-site packages; do not rely on a
`pip install --user` installation. ArcGIS names its hooks in camelCase; Ruff permits those names.
The `.pyt` formatter is excluded to avoid unrelated UI-code churn.

## Verification

```bash
uv sync --extra dev --extra fgdb --extra duckdb
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

See `docs/smoke-testing.md` for native Pro checks. ArcPy doubles do not prove
native Pro behavior. Use temporary outputs for data tests; do not overwrite
source projects or existing databases. See `scripts/CLAUDE.md` for loader invariants.
