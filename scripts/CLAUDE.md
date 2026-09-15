# The loaders (`scripts/*.sh`)

The only non-Python artifacts here: **not in the wheel** (unlike the `.pyt`, which
ArcGIS Pro loads — these have no such need and no exec path inside a wheel),
not linted by ruff, and their embedded Python lives in heredocs so neither ruff nor pytest
sees it. `bash -n` is the check.

One exception, and it earns its keep: `tests/test_oracle_loader_dates.py` extracts the
`loader.py` heredoc out of `fgdb_to_oracle.sh` and tests `parse_dt` directly. That parser
turned a whole schema's date columns silently NULL twice — once by not handling the `+00`
offset ogr2ogr writes, once by reading the `-26` of a date-only `2024-07-26` as that same
offset — and both times the load counted the right rows and exited 0. Pure logic whose
failure mode is wrong data rather than an error is worth lifting out of the heredoc; the
Oracle-facing rest of it is not.

What breaks when you edit them:

- **`fgdb_to_duckdb.sh` is the one place the toolbox's output shape is *reimplemented*.**
  The Python entry points use shared modules; these standalone shell loaders
  keep their own embedded Python, so the `sp_ref (wkid, text)` table, the OID `PRIMARY KEY`, the
  `<table>_rtree` index and the `EXCLUDE_NAMES` field drop are hand-mirrored from
  `DuckDBToolbox.pyt`. Change either side and they silently disagree — a consumer
  pointed at both databases sees two different schemas. The name guards mirror
  `_collect_export_layers`, which is the same rule `check_table_names` states in the
  CLI: three copies, one contract. They live in the script's **`plan.py` heredoc, not
  in bash**, because `${var,,}` and `${var//[^A-Za-z0-9_]/_}` are locale-dependent —
  under `LC_ALL=C` (cron, Docker, CI) they split multi-byte characters per byte and
  would name the same layer differently than the Python copies do.
- **A `Layers.json` *drives* that loader; it is not a post-hoc rename.** The plan is one
  row per catalog layer (`table_name`), so two Pro layers over one feature class produce
  two tables — what the toolbox exports, since it iterates map layers, not GDB contents.
  Keying a rename map by GDB layer instead silently collapses them into one. The corollary
  is that the two loaders **deliberately** disagree on scope: `fgdb_to_postgis.sh` loads
  every GDB layer and only renames per catalog, so a GDB with seven feature classes and
  five catalogued layers yields 7 PostGIS tables and 5 DuckDB ones. Don't reconcile them — the DuckDB side reproduces a Pro export, the PostGIS
  side fills a database a catalog is later written *against*.
- **The whole ingest runs in one DuckDB transaction.** Every `CREATE OR REPLACE TABLE`
  otherwise auto-commits, so the row-count assertion tripping on layer 5 of 12 would
  leave a queryable database mixing two exports — precisely the "plausible-looking
  database" the staging hop exists to prevent. Don't split it into per-layer commits
  "for memory": the assertion means nothing without the rollback.
- **`ogr2ogr` needs `-preserve_fid`.** `-lco FID=OBJECTID` alone writes a *fresh
  0-based sequence* into that column, and the PRIMARY KEY then keys on a fabricated
  identifier that matches neither the GDB nor a toolbox-built `.ddb`. The OID column
  name comes from OGR's `fidColumnName` per layer, because a feature class appended in
  Pro carries `OBJECTID_1` and the toolbox keys on `desc.OIDFieldName`, not a constant.
- **The default output extension is `.ddb`**, matching the active-map toolbox.
  The full-project terminal exporter uses `.duckdb`; both are DuckDB files.
- **`STAGE_DIR` is a staging *parent*, and the EXIT trap only removes the `mktemp -d`
  subdir under it.** The trap is installed before the pre-flight checks, so an
  `rm -rf "$STAGE_DIR"` would delete an operator-supplied directory — `STAGE_DIR=.`
  included — on something as ordinary as a mistyped GDB path.
- **The GeoParquet staging hop in `fgdb_to_duckdb.sh` is not incidental.** DuckDB's
  `ST_Read` would read the `.gdb` in one step, but through duckdb's *bundled* GDAL —
  the old-driver-yields-0-features failure the Oracle loader is split around. Host
  `ogr2ogr` keeps the driver under the operator's control, and the per-layer
  `loaded != featureCount` assertion is what turns a silent empty read into an exit.
  Deleting either re-opens a failure mode that produces a plausible, empty database.
- **`TARGET_SRS` uses `${VAR-default}`, not `${VAR:-default}`**, in both the DuckDB and
  PostGIS loaders — an explicitly empty value has to survive as empty or the documented
  "keep native per-layer CRSs" mode is unreachable, since `:-` treats empty as unset.
  `fgdb_to_oracle.sh` deliberately keeps `:-`: it passes `-t_srs` unconditionally and
  stamps `SRID` on every geometry, so there is no native mode to reach and an empty
  value would only mislabel the data.
- **The default DuckDB is GDB-relative; explicit relative paths stay CWD-relative.**
  `fgdb_to_duckdb.sh` writes `<GDB parent>/<GDB stem>.ddb` unless `DUCKDB_PATH` is
  set. An explicit relative `DUCKDB_PATH`, plus `DUMP_DIR` / `LOG_DIR`, remains
  CWD-relative. Nothing resolves against `$0`/`BASH_SOURCE`. That is what lets a consuming-stack operator run
  `EXPORT_MODE=dump ../layers-json/scripts/fgdb_to_postgis.sh` from their own root and have
  the artifact land in the `docker/initdb` bind mount their compose stack replays. Making
  either `$BASH_SOURCE`-relative silently breaks that handshake across repos.
- **`RENAME_MAP` `[@]`/`[!@]` expansions are `+`-guarded.** An empty associative array trips
  `set -u` even on bash 5.x, and the obvious `${!arr[@]+…}` fix breaks *non-empty* maps via
  indirect expansion. Test both the empty and populated paths.
- **The never-drop-`public` guard** in the clean-slate schema step: dropping `public`
  destroys the PostGIS install itself.
