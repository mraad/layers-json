# APRX terminal exporter

`layers-duckdb` (also available as `scripts/aprx_to_duckdb.py`) provides an interactive path
prompt, live progress bars, and a final table summary. It reads every map's
feature layers and standalone tables, including accessible feature services.
It writes `<project>.duckdb` beside the input `.aprx`.

Run it from an **ArcGIS Pro Python environment or Pro conda clone**. ArcPy is
provided by ArcGIS Pro and cannot be installed from PyPI. If ArcPy is absent,
the script prints a setup message and exits with code 1 without a traceback.
Install the optional dependencies into your chosen Pro environment from this checkout:

```powershell
python -m pip install -e ".[duckdb]"
python scripts/aprx_to_duckdb.py "C:\Projects\NorthSea\NorthSea.aprx"
# Omit the path to enter it interactively:
python scripts/aprx_to_duckdb.py
# Export one map, replacing an existing output only after successful conversion:
python scripts/aprx_to_duckdb.py "C:\Projects\NorthSea\NorthSea.aprx" --map Map --overwrite
```

`--batch-size` controls rows per Arrow batch (default 10000). A normal `uv run`
environment does not include ArcPy; use the Pro environment's Python executable.
The `duckdb` extra declares DuckDB 1.5.5+, PyArrow 25.0.1+, and Rich 15.0.0+,
as declared in `pyproject.toml`. Use a Pro clone when updating packages.

The export preserves hidden attributes, GlobalIDs, original object IDs, nulls,
and temporal types. Geometry becomes WGS 84 (EPSG:4326) `GEOMETRY`, with an
R-tree index and an object-ID primary key where available. The highest-ranked
available ArcPy datum transformation is used and recorded. Saved selections are
cleared in memory, definition queries are honored, and the project is never saved.
Basemaps, group containers, raster layers and utility-network configuration are
reported as skipped; their feature children are still exported. Unknown coordinate
systems, unsupported attribute types (including raster fields), broken sources,
and unreadable services fail the export rather than silently dropping data.
Service access uses the credentials available to ArcPy; the script does not sign in.

Table names replace punctuation/spaces with underscores, prefix leading digits,
and receive numeric suffixes for collisions across maps or with reserved tables.
`_export_layers` records the mapping, field metadata, counts, transformation and
skipped layers; `sp_ref` records the output CRS. This is a complete attribute
export, so its columns may differ from a filtered `Layers.json` catalog.

Every exported count is checked against its source, including empty tables.
The entire database is staged beside the project and published only after all
layers and indexes succeed. Failure or Ctrl+C before publication removes staging
files and preserves any existing database. Existing output requires `--overwrite`.
Live service edits during export can cause a count mismatch; retry against stable
source data in that case. Ctrl+C exits with code 130; other failures return 1.

---

# Loaders — File Geodatabase → DuckDB / PostGIS / Oracle

The no-ArcGIS-Pro alternative to `DuckDBToolbox.pyt`. Same step of the job: fill the
spatial database that the `Layers.json` catalog describes.

Bulk loaders that import an Esri **File Geodatabase** (`.gdb`) into a spatial
database for spatial SQL applications. Each targets one backend:

| Script | Target | Geometry | How it runs |
|---|---|---|---|
| [`fgdb_to_duckdb.sh`](#fgdb_to_duckdbsh) | DuckDB | `geometry` (EPSG:4326) | `ogr2ogr` → GeoParquet → duckdb |
| [`fgdb_to_postgis.sh`](#fgdb_to_postgissh) | PostGIS | `geometry` (EPSG:4326) | `ogr2ogr` straight into PostgreSQL |
| [`fgdb_to_oracle.sh`](#fgdb_to_oraclesh) | Oracle Spatial | `SDO_GEOMETRY` (SRID 4326) | host export → load **inside** the container |

All three read the GDB with the GDAL **OpenFileGDB** driver, reproject every layer to
WGS84 (EPSG:4326) by default, and are **re-runnable** (each target table is
recreated on every run). All knobs are environment variables with sane defaults.
Trailing slashes on the GDB path are stripped — `NorthSea.gdb/` is the same as
`NorthSea.gdb`, and `dirname` of the slashed form is not the GDB itself.

> **GDAL version matters.** Recent ArcGIS-authored GDBs are not readable by old
> OpenFileGDB drivers (they open the schema but yield 0 features). Use a modern
> host GDAL — `brew install gdal` (3.4+). On macOS the bash 4+ requirement also
> means `brew install bash` (the system bash is 3.2).

## What a sibling `Layers.json` does — and it is not the same thing

All three loaders look for a catalog next to the GDB, but they use it differently, so
**the same GDB does not produce the same set of tables**:

| Loader | What it loads | What the catalog does |
|---|---|---|
| `fgdb_to_duckdb.sh` | one table per **catalog layer** | *drives* the load — an uncatalogued feature class is never loaded, and two layers over one feature class become two tables |
| `fgdb_to_postgis.sh` | every **GDB layer** | *renames* loaded tables to the catalog's `name`, lowercased with spaces underscored (`Wells` → `wells`); an uncatalogued feature class still lands |
| `fgdb_to_oracle.sh` | every **GDB layer** (or `LAYERS`) | nothing — names come from the GDB, uppercased |

For example, with 7 feature classes and 5 catalogued, the PostGIS
loader writes 7 tables and the DuckDB loader writes 5. Both are correct for what they
are — the DuckDB one exists to reproduce what `DuckDBToolbox.pyt` exports from the
active map, and Pro exports map layers, not GDB contents; the PostGIS one fills a
database a catalog is later written *against*. Use `LAYERS` on either to be explicit.

---

## `fgdb_to_duckdb.sh`

Loads a GDB into a single DuckDB database — the same output `DuckDBToolbox.pyt`
produces, without ArcGIS Pro or `arcpy`.

### How it runs

Host `ogr2ogr` reads the GDB, reprojects to `TARGET_SRS`, and stages **one
GeoParquet per table** in a temp dir; the `duckdb` Python API then ingests the
parquet and builds the indexes, **inside one transaction** — a failure anywhere
rolls the whole load back and leaves the target database exactly as it was.

The staging hop is deliberate. DuckDB's spatial extension can read a `.gdb`
directly (`ST_Read`), but through *its own bundled* GDAL — the same version
exposure that makes `fgdb_to_oracle.sh` host-orchestrated, where an old
OpenFileGDB driver opens the schema and yields **0 features** with no error.
Reading with the host's `ogr2ogr` puts that under the operator's control, and
each table's loaded row count is asserted against `ogrinfo`'s `featureCount`, so
a silent empty read rolls the run back instead of producing a plausible database.

### What gets loaded

**With a sibling `Layers.json`** (the normal case) the catalog drives the load:
one table per catalog layer, named by its `table_name`. That is what makes a Pro
layer named `Wells` over feature class `Wellbores` land as table `Wells`, and two
layers over one feature class land as **two** tables — the same set
`DuckDBToolbox.pyt` would export, since it iterates map layers, not GDB contents.
Catalog entries with no GDB source (web-service layers) are skipped silently; ones
naming a feature class this GDB lacks are skipped with a warning.

**Without one**, every layer in the GDB loads under its own name, minus
`<FC>__ATTACH` / `<FC>__ATTACHREL` attachment sidecars. Annotation and dimension
feature classes are *not* detected and load as polygon tables — exclude them with
`LAYERS`, or drive the load with a `Layers.json`.

A definition query is a *layer* property in the `.aprx`; these bulk loaders do not
read the project or apply that query: two catalog layers over one feature class get the same full row set.

### Requirements

`bash` 4+, `ogr2ogr` + `ogrinfo` with the **OpenFileGDB** and **Parquet** drivers
(`brew install gdal`), `python3`, and the `duckdb` Python package. No DuckDB CLI
is needed and none is assumed: the script resolves an interpreter in order —
`DUCKDB_PY_CMD`, a `python3` that already imports `duckdb`, else `uv run
--no-project --with duckdb python` (which fetches it into a throwaway env).

### Usage

```bash
# Whole project GDB into NorthSea.ddb, reprojected to WGS84 (defaults shown)
bash scripts/fgdb_to_duckdb.sh ~/Documents/ArcGIS/Projects/NorthSea

# A different CRS, a subset of feature classes, an explicit output path
TARGET_SRS=EPSG:3857 LAYERS="Pipelines Discoveries" DUCKDB_PATH=./merc.ddb \
  bash scripts/fgdb_to_duckdb.sh /path/to/NorthSea.gdb

# Keep every layer in its native CRS (mixed-CRS database, no sp_ref)
TARGET_SRS= bash scripts/fgdb_to_duckdb.sh /path/to/NorthSea.gdb
```

Passing a project directory resolves its matching `<project>/<project>.gdb`;
passing a `.gdb` directly remains supported. By default `DUCKDB_PATH` is a
sibling of the source GDB. An explicitly supplied relative `DUCKDB_PATH`, and
`LOG_DIR`, are relative to your **current directory**, not to the script. The default extension is `.ddb`, matching the Pro toolbox.
The terminal project exporter defaults to `.duckdb`; both are DuckDB databases.

### Environment variables

| Var | Default | Notes |
|---|---|---|
| `GDB` | — | Project directory or `.gdb` fallback for the positional arg, which is the documented interface |
| `DUCKDB_PATH` | `<GDB parent>/<GDB stem>.ddb` | Output database; an explicit relative path is CWD-relative |
| `TARGET_SRS` | `EPSG:4326` | Reprojection target; **empty = keep native per-layer CRSs** (then `sp_ref` is dropped) |
| `SRID` | numeric tail of `TARGET_SRS` | What `sp_ref.wkid` records; forced empty in native mode |
| `OID_FIELD` | *(per layer, from OGR)* | Forces one OID column name across every layer instead of each layer's own FID column |
| `LAYERS` | all planned layers | Space-separated subset, by **GDB feature-class** name (unknown or blank = hard error) |
| `LAYERS_JSON` | `<gdb dir>/Layers.json` | Catalog that drives the load; missing file = load the GDB's own layers |
| `DUCKDB_PY_CMD` | *(auto-detected)* | Interpreter command that provides the `duckdb` package |
| `STAGE_DIR` | `$TMPDIR` | **Parent** for staging; the script `mktemp`s its own subdir under it and removes only that |
| `KEEP_STAGE` | `0` | `1` keeps the staged parquet for inspection |
| `LOG_DIR` | `./logs` | Timestamped run log written here |

### Output

One table per loaded layer, matching what `DuckDBToolbox.pyt` writes:

- geometry column **`geometry`**, plain DuckDB `GEOMETRY` (native types — no
  `PROMOTE_TO_MULTI`; the CRS-parameterized `GEOMETRY('OGC:CRS84')` GDAL's
  GeoParquet writer produces is cast away, since R-tree indexes reject it),
- `PRIMARY KEY` on the layer's own OID column — `OBJECTID_1` when that is what the
  feature class uses — carrying the **source** OIDs (`ogr2ogr -preserve_fid`),
- an R-tree index named `<table>_rtree`,
- a one-row **`sp_ref (wkid, text)`** lookup describing the export CRS,
- `globalid`, `shape_length`, `shape_area` and the other `EXCLUDE_NAMES` fields
  dropped, as the toolbox drops them. (CIM-hidden fields live in the `.aprx` this
  script never opens, so those it cannot drop.)

Three name guards are hard errors, mirroring `_collect_export_layers` and
`check_table_names`: a table name that is not a plain ASCII identifier, one
colliding with the reserved `sp_ref` table, and two layers mapping to one table.
None has a safe automatic repair — rename the layer in Pro. A source **field**
named `geometry` is likewise refused, since it would collide with the reserved
geometry column and DuckDB would silently rename it to `geometry_1`.

A layer with no geometry (a standalone GDB table) loads its attributes and is
skipped for the R-tree.

### Notes / gotchas

- `sp_ref.text` is GDAL's WKT1 for `TARGET_SRS` (via `gdalsrsinfo`); the toolbox
  stores `arcpy`'s `exportToString()`. Same CRS, different serialization — off Pro
  there is no `arcpy` to ask.
- Reprojecting a datum that needs a grid shift (e.g. ED50 → WGS84) makes GDAL warn
  that several coordinate operations were used. That warning is real signal about
  accuracy; it is left unsilenced. Pin one with `-ct` by editing the `OGR_ARGS`
  block if a specific transform is required.
- M-aware feature classes (XYM / XYZM, e.g. a measured pipeline centerline) keep
  their M values through the GeoParquet hop: `OGR_PARQUET_ALLOW_ALL_DIMS=YES`.
  Without it the Parquet writer refuses anything but 2D/Z, and the load dies
  rather than silently dropping M. DuckDB's `GEOMETRY` and the toolbox's WKB
  both carry M, so the staging format has to as well.
- Re-running replaces every table it loads (`CREATE OR REPLACE`), but does **not**
  drop tables from an earlier run with more layers — `DUCKDB_PATH` is a file you
  chose, not one this script owns. `sp_ref` is the exception: it is this format's
  own table, so native mode drops it rather than leaving a stale CRS behind.
- No duckdb version is pinned. The ingest works across the versions tested (1.4
  through 1.5.x); if `DUCKDB_PY_CMD` points at something much older, the run fails
  loud at ingest rather than writing a partial database.

---

## `fgdb_to_postgis.sh`

Loads a GDB into PostGIS, then builds GiST indexes and runs `ANALYZE`.

### Two modes (`EXPORT_MODE`)

- **`load`** (default) — load into a live PostgreSQL over a connection. Tuned for
  big GDBs: `COPY` mode, large transaction groups, indexes built *after* load,
  post-load SRID assertion.
- **`dump`** — server-free. Writes a gzipped `PGDump` `.sql.gz` artifact (no
  running PG needed) into `docker/initdb/`. The `postgis` compose service replays
  `*.sql.gz` on a **fresh volume only**; use a dedicated new database volume for import.

### Requirements

`bash` 4+, `ogr2ogr` + `ogrinfo` (OpenFileGDB driver), and for `load` mode a
reachable PostgreSQL with `psql` on PATH (the script `CREATE EXTENSION postgis`
and creates the target DB/schema itself).

### Usage

```bash
# Load into a live PostGIS (defaults shown)
PG_HOST=localhost PG_PORT=5432 PG_DB=data PG_USER=postgres PG_PASSWORD=postgres \
  bash scripts/fgdb_to_postgis.sh /path/to/NorthSea.gdb

# Build an offline dump artifact for a compose stack instead
EXPORT_MODE=dump bash scripts/fgdb_to_postgis.sh /path/to/NorthSea.gdb
```

`DUMP_DIR` and `LOG_DIR` are relative to your **current directory**, not to the
script — deliberately, so the dump lands in the stack that will replay it. From
the consuming stack's checkout:

```bash
cd ~/GWorkspace/<your-stack>
EXPORT_MODE=dump ../layers-json/scripts/fgdb_to_postgis.sh /path/to/NorthSea.gdb
#   -> ./docker/initdb/99-load-<schema>.sql.gz, which its docker-compose.yml
#      bind-mounts into the postgis container
docker compose up   # initialization scripts run only on a fresh database volume
```

### Environment variables

| Var | Default | Notes |
|---|---|---|
| `PG_HOST` / `PG_PORT` | `localhost` / `5432` | |
| `PG_DB` | lowercased GDB stem | Target database (created if missing) |
| `PG_USER` / `PG_PASSWORD` | `postgres` / `postgres` | Password passed via `PGPASSWORD`, never on the command line |
| `PG_SCHEMA` | lowercased GDB stem | Clean-slated each run; **never** `public` (guarded) |
| `TARGET_SRS` | `EPSG:4326` | Reproject target; empty = keep native per-layer SRIDs |
| `EXPORT_MODE` | `load` | `load` or `dump` |
| `DUMP_DIR` | `./docker/initdb` | Where `dump` mode writes `99-load-<schema>.sql.gz` |
| `GROUP_TXN` | `100000` | `ogr2ogr` transaction group size |
| `MAINT_WORK_MEM` | `1GB` | Per-session `maintenance_work_mem` for index builds |
| `PARALLEL_IDX` | `1` | Concurrent per-table GiST builds (peak RAM ≈ `PARALLEL_IDX × MAINT_WORK_MEM`) |
| `LAYERS_JSON` | `<gdb dir>/Layers.json` | Optional catalog: renames loaded tables to the operator `name`. A missing file keeps laundered names; a file that exists but cannot be parsed is a hard error. |
| `LAYERS` | all GDB layers | Space-separated subset, by **GDB feature-class** name (unknown or blank = hard error) |
| `LOG_DIR` | `./logs` | Timestamped run log written here |

### Output

Tables in `PG_SCHEMA`, geometry column `geometry`, FID column `gid`, GiST index
per table, every geometry asserted to be `TARGET_SRS`. When a `Layers.json` is
present, laundered table names are renamed to the catalog's lowercased `name`
(e.g. `Wellbores` → `wells`) so the catalog maps to real tables.

### Geometry and index policy

The loader names its FID column `gid`, builds GiST indexes after loading, and
keeps native single/multipart geometry types without forcing `PROMOTE_TO_MULTI`.
Applications should inspect the resulting schema rather than assume an ArcGIS
object-ID column name or a particular multipart geometry type.

---

## `fgdb_to_oracle.sh`

Loads a GDB into Oracle Spatial running in a `gvenzl/oracle-free` container.

### Why this is host-orchestrated (not pure in-container)

The `oracle-free` image ships Oracle DB only. The newest GDAL available to it
(EPEL `gdal` 3.0.4 on Oracle Linux 8) **cannot read** recent ArcGIS GDBs — its
OpenFileGDB driver opens the schema but reads 0 features. Homebrew/conda GDAL has
no proprietary **OCI** driver, so `ogr2ogr -f OCI` can't write `SDO_GEOMETRY`
either. The script therefore splits along the only seam that works:

- **host side** — modern `ogr2ogr` reads the GDB, reprojects to 4326, writes one
  CSV-with-WKT per layer plus an `ogrinfo -json` schema sidecar.
- **container side** — a `python-oracledb` loader (THIN mode, no Oracle client)
  runs **inside** the container via `docker exec`: creates a typed table per
  layer, registers `USER_SDO_GEOM_METADATA`, inserts `SDO_GEOMETRY(wkt, SRID)`,
  and builds a spatial index. All DB writes execute in the container.

Staging is pushed in with `docker cp` (you can't add a bind mount to an
already-running container). The loader auto-installs `python39` + `oracledb` in
the container on first run.

### Requirements

Host: `bash` 4+, `python3`, `ogr2ogr` + `ogrinfo` (GDAL 3.4+), `docker`, and a
**running** target container. The container only needs to be a `gvenzl/oracle-free`
instance with the spatial-enabled DB up.

Start the container (example):

```bash
docker run -d --rm --name oracle-spatial \
  -e ORACLE_PASSWORD=YourSecurePassword123 \
  -e APP_USER=spatial_user -e APP_USER_PASSWORD=UserPassword123 \
  -p 1521:1521 \
  gvenzl/oracle-free
```

### Usage

```bash
# ORA_PASSWORD is required (no baked-in default) — export it once for the session
export ORA_PASSWORD='your-oracle-free-app-user-password'

# Load every layer (other defaults shown)
CONTAINER=oracle-spatial \
ORA_USER=spatial_user ORA_DSN=localhost:1521/FREEPDB1 \
  bash scripts/fgdb_to_oracle.sh /path/to/NorthSea.gdb

# Load a subset, with a table-name prefix
LAYERS="Wellbores Pipelines" TABLE_PREFIX=NS_ \
  bash scripts/fgdb_to_oracle.sh /path/to/NorthSea.gdb
```

### Environment variables

| Var | Default | Notes |
|---|---|---|
| `GDB` | — | Fallback for the positional arg, which is the documented interface |
| `CONTAINER` | `oracle-spatial` | Target container name (must be running) |
| `ORA_USER` | `spatial_user` | Schema owner |
| `ORA_PASSWORD` | *(required, no default)* | DB password — export it; never baked into the script |
| `ORA_DSN` | `localhost:1521/FREEPDB1` | Easy-connect DSN, **container-internal** |
| `TARGET_SRS` | `EPSG:4326` | Reprojection target |
| `SRID` | `4326` | Oracle SRID stamped on geometries (must be geodetic — guarded) |
| `TABLE_PREFIX` | *(empty)* | Prefix prepended to every table name |
| `LAYERS` | all non-attachment layers | Space-separated subset, by **GDB feature-class** name (unknown or blank = hard error). Unset skips `<FC>__ATTACH` / `<FC>__ATTACHREL` sidecars |
| `WORKDIR` | `/tmp/work` | Staging dir inside the container |

### Output

One table per layer in `ORA_USER`'s schema, names **uppercased** (Oracle folds
unquoted identifiers): `Wellbores` → `WELLBORES`. Each table gets:

- a `GEOMETRY SDO_GEOMETRY` column,
- a `USER_SDO_GEOM_METADATA` row (lon/lat bounds −180..180 / −90..90, tol 0.05, the chosen SRID),
- an `MDSYS.SPATIAL_INDEX_V2` index named `<TABLE>_SIDX`,
- optimizer statistics (`DBMS_STATS.GATHER_TABLE_STATS`, cascade) — each table is
  created fresh, so without this the optimizer plans from block counts alone.

The script ends by printing the registered spatial metadata. Re-running drops and
recreates each table (`DROP TABLE ... PURGE`) — which also drops any B-tree indexes
you added by hand afterwards. Keep that DDL in a file you can replay.

### Notes / gotchas

- Container runs as uid `oracle`; only `/tmp`, `/var/tmp`, `/opt/oracle` are
  writable, so staging lives under `/tmp/work` and `microdnf`/`pip` run via
  `docker exec -u 0`.
- The SDO_DIM bounds assume a **geodetic** CRS. Changing `SRID` to a projected
  one without matching bounds fails loud (a small geodetic-SRID allow-list); add
  the SRID or supply projected bounds rather than stamping wrong metadata.
- Source CRS is detected and reprojected by the host GDAL — e.g. a Web Mercator
  GDB lands as proper lon/lat in SRID 4326.
- Dates arrive through the CSV hop as `2024/07/26 12:07:38+00`, so `parse_dt`
  handles the trailing UTC offset itself (`%z` rejects the bare-hour form) and
  stores UTC in the naive `TIMESTAMP` columns. A value it cannot parse loads as
  NULL and is counted on that layer's `[ok]` line — a non-zero
  `unparsed dates -> NULL` there means a format the parser does not know yet, not
  empty source data.
- Verify a load:
  ```bash
  docker exec -i oracle-spatial \
    sqlplus -S spatial_user/UserPassword123@localhost:1521/FREEPDB1 <<'SQL'
  SELECT table_name, column_name, srid FROM user_sdo_geom_metadata ORDER BY table_name;
  SQL
  ```
