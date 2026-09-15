# Smoke testing

Run from a checkout with the `dev`, `fgdb`, and `duckdb` extras. Install the DuckDB
spatial extension before the spatial integration test if it is not cached:

```bash
uv sync --extra dev --extra fgdb --extra duckdb
uv run python -c "import duckdb; duckdb.connect().execute('INSTALL spatial')"
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

Tests needing GDAL, DuckDB, or Rich skip if their optional dependency is missing.
The spatial test skips when the extension is unavailable. Check the skip summary
with `pytest -ra`; a passing minimal installation is not full integration coverage.

## Automated integration coverage

`tests/test_shared_smoke.py` exercises:

- Identity of the shared catalog classes and hint implementation in the Pro adapter.
- A generated File Geodatabase and CIM project through catalog, OKF, and field-update
  commands in a fresh process that rejects ArcPy imports and `.pyt` loading.
- Sample values, field aliases, knowledge files, and source-project preservation.
- A complete terminal export using ArcPy doubles with real DuckDB spatial SQL:
  multiple Arrow batches, null geometry, original IDs, metadata, primary-key/index
  creation, and publication protection.

The rest of the suite covers XML rejection, domains/subtypes, temporal/binary
values, name collisions, empty tables, query handling, failed exports, rollback,
loader argument handling, and the Oracle date parser.

For distribution testing, build a wheel, install it without dependencies into a
new virtual environment outside the checkout, and run `--help` for `layers-json`,
`layers-okf`, `layers-hide-update`, and `layers-duckdb`. Confirm that the shared
modules and both `.pyt` files are installed. Missing ArcPy should produce a setup
message and exit 1 from `layers-duckdb`, without a traceback.

## Native ArcGIS Pro checklist

Install the package into the Pro environment before adding the toolboxes. Use a
copy of a small project with point/line/polygon features, a standalone table,
an empty table, nullable attributes, a domain, and a subtype.

1. Open both toolboxes and check their parameter dialogs. Run Prepare Metadata.
   Compare its catalog with the CLI output for the same local layers and limits;
   normalize source paths before comparing.
2. Test Hide and Update Fields on the copy, then Set Definition Query. Confirm the
   query constrains sampling and can be cleared. Check the resulting aliases.
3. Export through DuckDBToolbox with a small batch size. Verify hidden/system-field
   exclusions, OID preservation, geometry, spatial indexes, and extent filtering.
4. Query that database back into Pro as both a feature layer and an attribute-only
   table. Check geometry placement and temporal values.
5. Run `layers-duckdb` on the saved copy, with and without `--map`. Check hidden
   attributes, GlobalIDs, collisions across maps, nulls, and empty tables. Confirm
   saved selections are cleared for export and definition queries remain effective.
6. Re-run without `--overwrite` and confirm rejection. Test an unreadable source
   and cancellation against an existing output; its contents should remain intact.
7. Test authenticated services and a source requiring a datum transformation in
   the actual Pro environment. Check the transformation recorded in `_export_layers`.

ArcPy doubles cannot validate licensing, native project loading, UI hooks,
authentication, or the installed ArcPy/Arrow DLL combination.

## Bulk loaders

Use temporary data and dedicated destinations. The DuckDB loader replaces loaded
tables; the PostGIS loader recreates its target schema; Oracle drops and recreates
its target tables. Do not point smoke tests at a working database.

```bash
DUCKDB_PATH=/tmp/northsea-smoke.ddb LOG_DIR=/tmp/northsea-logs \
  bash scripts/fgdb_to_duckdb.sh /path/to/NorthSea.gdb
EXPORT_MODE=dump DUMP_DIR=/tmp/northsea-dump LOG_DIR=/tmp/northsea-logs \
  bash scripts/fgdb_to_postgis.sh /path/to/NorthSea.gdb
```

Check source/target counts, geometry coordinates, primary keys, indexes, and date
values. Test catalog-driven DuckDB loading and the no-catalog case separately.
A PostGIS dump validates export generation; restore it into a disposable database
to validate server execution. Oracle requires a disposable running container.

## Refactor verification — 2026-09-14

- Full suite on macOS: **160 passed, 0 failed**, with GDAL and DuckDB spatial
  integration enabled. An earlier Windows run recorded 136 passes and 8 failures
  (two Windows path expectations, six Bash tests needing an installed WSL distribution);
  those failures are environment-specific and were not reproduced on macOS.
- Ruff lint/format checks and source/wheel builds passed.
- Clean wheel install: all four command help pages and missing-ArcPy diagnostics passed.
- Generated File GDB: DuckDB bulk load and PostGIS dump both exited 0; source values,
  DuckDB row counts/geometry, and SQL dump contents were checked.
- Native ArcGIS Pro UI/services and live PostGIS/Oracle loads were not run on this Mac.
- Post-merge simplification (shared field policy, one XML error type, removed
  toolbox forwarding shims): 160 passed, Ruff and build clean on macOS. Pro UI not rerun.

## Embedded Pro imports

Pro can disable user-site packages even when its external Python interpreter
imports them successfully. The bundled toolboxes resolve the adjacent package
before importing shared modules. The isolated-process regression test disables
all site packages and verifies this checkout-loading path.

Verified in the running ArcGIS Pro 3.7.2 Python window with
`site.ENABLE_USER_SITE == False`: imported all four Layers tools, ran PrepareTool
on the current NorthSea map with three sampled rows per input, and generated a
five-entry catalog in a temporary folder. Both DuckDB tools also imported.
