#!/usr/bin/env bash
#
# fgdb_to_duckdb.sh
# Bulk-load a File Geodatabase into a single DuckDB database — no ArcGIS Pro,
# no arcpy. The off-Pro counterpart of DuckDBToolbox.pyt's "Feature Layers to
# DuckDB", and it lands the same shape:
#
#   - one table per catalog layer, named as the toolbox names it
#   - geometry column `geometry` (plain DuckDB GEOMETRY), never Esri JSON
#   - PRIMARY KEY on the layer's own OID column, R-tree index `<table>_rtree`
#   - one-row `sp_ref (wkid, text)` lookup describing the export CRS
#
# Host GDAL reads the GDB and does the reprojection (TARGET_SRS, default
# EPSG:4326), staging one GeoParquet per table; duckdb then ingests the parquet
# inside ONE transaction, so a failure anywhere leaves the target database
# exactly as it was.
#
# The staging hop is deliberate: DuckDB's spatial extension carries its own
# bundled GDAL, and an old OpenFileGDB driver opens a recent ArcGIS GDB but
# yields 0 features — the same failure fgdb_to_oracle.sh splits around. Reading
# with the host's ogr2ogr keeps that under the operator's control, and the
# per-layer row-count assertion below turns a silent 0 into a rolled-back run.

set -euo pipefail

# mapfile/readarray require bash 4+ (macOS ships 3.2 — `brew install bash`)
(( BASH_VERSINFO[0] >= 4 )) || { echo "ERROR: requires bash 4+ (macOS: brew install bash)" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# ArcGIS project directory or source File Geodatabase — REQUIRED first
# positional argument. A project directory resolves to <dir>/<basename>.gdb.
# (The GDB env var is honored as a fallback, matching the sibling loaders.)
case "${1:-}" in
  -h|--help)
    echo "Usage: $0 <project-dir|path-to-file.gdb>   (override target via DUCKDB_PATH/TARGET_SRS/LAYERS/... env vars)" >&2
    exit 0
    ;;
esac
SOURCE="${1:-${GDB:-}}"
[[ -n "$SOURCE" ]] || {
  echo "ERROR: missing required argument: ArcGIS project directory or File Geodatabase" >&2
  echo "Usage: $0 <project-dir|path-to-file.gdb>" >&2
  exit 1
}

_SOURCE_PATH="${SOURCE%"${SOURCE##*[!/]}"}"  # drop *every* trailing slash; `%/` drops only one
if [[ "${_SOURCE_PATH,,}" == *.gdb ]]; then
  GDB="$_SOURCE_PATH"
else
  _PROJECT_NAME="${_SOURCE_PATH##*/}"
  GDB="${_SOURCE_PATH}/${_PROJECT_NAME}.gdb"
fi

_GDB_PATH="$GDB"
_GDB_STEM="${_GDB_PATH##*/}"  # basename: NorthSea.gdb
_GDB_STEM="${_GDB_STEM%.*}"   # strip extension: NorthSea
_GDB_PARENT="$(dirname "$_GDB_PATH")"

# Output database. `.ddb`, not `.duckdb`: DuckDBToolbox.pyt writes <project>.ddb
# the consumer's entrypoint discovers its database with `ls /project/*.ddb`, so a
# different extension means a database nothing finds. By default it is a sibling
# of the source GDB; an explicit relative DUCKDB_PATH remains CWD-relative.
DUCKDB_PATH="${DUCKDB_PATH:-${_GDB_PARENT}/${_GDB_STEM}.ddb}"

# Reproject every layer to this CRS. Default WGS84 so the whole database is
# lon/lat regardless of what each feature class was authored in. Set empty to
# keep native per-layer CRSs — then the database is mixed-CRS and sp_ref cannot
# describe it, so it is dropped rather than written.
#
# `${VAR-default}`, not `${VAR:-default}`: an explicitly empty TARGET_SRS has to
# survive as empty, or "keep native" would be unreachable.
TARGET_SRS="${TARGET_SRS-EPSG:4326}"
if [[ -n "$TARGET_SRS" ]]; then
  SRID="${SRID-${TARGET_SRS##*:}}"   # "EPSG:4326" -> "4326"
else
  # Native mode has no single SRID to record. Clear it unconditionally: SRID is a
  # real env var in fgdb_to_oracle.sh, and an inherited value would otherwise
  # stamp sp_ref with a CRS the geometries are not in.
  SRID=""
fi

# OID column. Default is per-layer: whatever OGR reports as the layer's FID column
# (OBJECTID_1 after an ArcGIS append, etc.), which is what the toolbox keys on via
# desc.OIDFieldName. Set OID_FIELD to force one name across every layer.
OID_FIELD="${OID_FIELD:-}"
OID_FALLBACK="OBJECTID"   # layers OGR reports with no FID column name

# Space-separated subset of GDB feature classes to load. Empty = everything the
# catalog lists (or every non-system layer when there is no catalog).
LAYERS_FILTER="${LAYERS:-}"

# Optional operator Layers.json. When present it *drives* the load: one table per
# catalog layer, named by the catalog's table_name — so a Pro layer named "Wells"
# over feature class "Wellbores" lands as table "Wells", and two layers over one
# feature class land as two tables, exactly as DuckDBToolbox.pyt would export
# them. Missing file -> load the GDB's own layers under their own names.
LAYERS_JSON="${LAYERS_JSON:-$(dirname "$GDB")/Layers.json}"

LOG_DIR="${LOG_DIR:-./logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/duckdb_$(date +%Y%m%d_%H%M%S).log"

# GeoParquet staging. STAGE_DIR is the *parent*: this script always mktemp's its
# own subdirectory under it and only ever removes that. It must never rm -rf a
# path the operator handed in — that path can be, and has been, a directory with
# other files in it.
STAGE_PARENT="${STAGE_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "$STAGE_PARENT"
STAGE="$(mktemp -d "$STAGE_PARENT/fgdb_duckdb.XXXXXX")"
KEEP_STAGE="${KEEP_STAGE:-0}"
cleanup() { [[ "$KEEP_STAGE" == "1" ]] || rm -rf "$STAGE"; }
trap cleanup EXIT

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# How to run the duckdb Python API. There is no bash-native DuckDB client, and
# the CLI is a separate install, so resolve in order: an explicit override, a
# python3 that already has duckdb, else uv (which fetches it into a throwaway env).
declare -a DUCKDB_PY
if [[ -n "${DUCKDB_PY_CMD:-}" ]]; then
  read -r -a DUCKDB_PY <<< "$DUCKDB_PY_CMD"
elif command -v python3 >/dev/null && python3 -c 'import duckdb' 2>/dev/null; then
  DUCKDB_PY=(python3)
elif command -v uv >/dev/null; then
  DUCKDB_PY=(uv run --no-project --with duckdb python)
else
  echo "ERROR: need the duckdb Python package. Install it (pip install duckdb), or install uv," >&2
  echo "       or point DUCKDB_PY_CMD at an interpreter that has it." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
log "=== FileGDB → DuckDB bulk load ==="
log "Source GDB:   $GDB"
log "Target DB:    $DUCKDB_PATH"
log "Target SRS:   ${TARGET_SRS:-<native per-layer>}"
log "Staging:      $STAGE"

[[ -d "$GDB" ]] || { log "ERROR: GDB not found: $GDB"; exit 1; }

if [[ -n "$TARGET_SRS" ]]; then
  [[ "$SRID" =~ ^[0-9]+$ ]] || {
    log "ERROR: cannot derive a numeric SRID from TARGET_SRS='$TARGET_SRS' — set SRID explicitly"; exit 1; }
fi

command -v ogr2ogr >/dev/null || { log "ERROR: ogr2ogr not on PATH"; exit 1; }
command -v ogrinfo >/dev/null || { log "ERROR: ogrinfo not on PATH"; exit 1; }
command -v python3 >/dev/null || { log "ERROR: python3 not on PATH"; exit 1; }

log "GDAL: $(ogr2ogr --version)"
# Captured once and matched in-shell, never `ogrinfo --formats | grep -q`: that
# pipeline races under `set -o pipefail` — grep exits at the first match, ogrinfo
# dies of SIGPIPE (141), and the pipeline reports the driver missing when it is
# present. The longer the driver list, the more reliably it fires.
OGR_FORMATS="$(ogrinfo --formats 2>/dev/null)"
[[ "$OGR_FORMATS" == *OpenFileGDB* ]] \
  || { log "ERROR: OpenFileGDB driver missing"; exit 1; }
# GeoParquet is the staging format; without the driver there is nothing to hand duckdb.
[[ "$OGR_FORMATS" == *Parquet* ]] \
  || { log "ERROR: GDAL has no Parquet driver (brew install gdal, built with Arrow support)"; exit 1; }

# ---------------------------------------------------------------------------
# Plan: resolve (GDB layer -> DuckDB table) pairs
# ---------------------------------------------------------------------------
# Done in python, not bash: the naming rules are a hand-mirror of
# DuckDBToolbox._collect_export_layers and layers_from_aprx.check_table_names,
# and bash's ${var//[^A-Za-z0-9_]/_} and ${var,,} are locale-dependent — under
# LC_ALL=C (cron, Docker, CI) they split multi-byte characters per byte and would
# name the same layer differently than the Python copies do.
PLAN="$STAGE/plan.tsv"
PLAN_PY="$STAGE/plan.py"
cat > "$PLAN_PY" <<'PY'
import json
import re
import sys

catalog_path, layers_filter = sys.argv[1], sys.argv[2]

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Attachment sidecars OGR reports as ordinary layers. DuckDBToolbox never sees
# them (it iterates the active map's feature layers, not the GDB's contents), and
# a <FC>__ATTACH table is every attached photo as a BLOB.
# ponytail: name-based skip only. Annotation/dimension feature classes are not
# detected here and load as polygons — exclude them via LAYERS, or drive the load
# with a Layers.json, which is the path that mirrors the toolbox exactly.
SYSTEM = re.compile(r"__ATTACH(REL)?$", re.IGNORECASE)


def die(message):
    sys.exit(f"ERROR: {message}")


try:
    payload = json.load(sys.stdin)
    ogr_layers = payload.get("layers") or []
    if not isinstance(ogr_layers, list):
        raise TypeError("layers is not a list")
    meta = {
        layer["name"]: layer
        for layer in ogr_layers
        if isinstance(layer, dict) and layer.get("name")
    }
    names = list(meta)
except (ValueError, AttributeError, TypeError) as exc:
    die(f"could not parse `ogrinfo -json` output: {exc}")
if not names:
    die("no layers found in the GDB")

# Only an *absent* catalog selects the GDB-layer fallback. A catalog that exists
# but cannot be read or parsed must not quietly demote the run to loading every
# feature class under its source name — that is a different database than the one
# the operator asked for, and it would look like a successful load.
try:
    with open(catalog_path, encoding="utf-8") as handle:
        catalog = json.load(handle)
except FileNotFoundError:
    catalog = None
except (OSError, ValueError) as exc:
    die(f"could not read {catalog_path}: {exc}")
else:
    if not isinstance(catalog, dict):
        die(f"{catalog_path} must contain a JSON object")

pairs = []  # (gdb layer, duckdb table)
if catalog is not None:
    missing = []
    for layer in catalog.get("layers") or []:
        uri = (layer.get("uri") or "").replace("\\", "/").rstrip("/")
        source = uri.split("/")[-1] if uri else ""
        # table_name is the catalog's own field; `name` with spaces underscored is
        # what the toolbox would have named the table.
        target = (layer.get("table_name") or layer.get("name") or "").strip().replace(" ", "_")
        if not source or not target:
            continue  # web-service layers carry no GDB source
        if source not in names:
            missing.append(source)
            continue
        pairs.append((source, target))
    if not pairs:
        die(f"{catalog_path} lists no layer that exists in this GDB (looked for: {', '.join(sorted(set(missing))) or 'nothing'})")
    for source in sorted(set(missing)):
        print(f"WARNING: {catalog_path} references '{source}', which is not in this GDB — skipped", file=sys.stderr)
else:
    pairs = [(name, name.replace(" ", "_")) for name in names if not SYSTEM.search(name)]
    if not pairs:
        die("every layer in the GDB is an attachment sidecar")

if layers_filter.strip():
    wanted = layers_filter.split()
    unknown = [w for w in wanted if w not in names]
    if unknown:
        die(f"LAYERS names no such GDB layer: {', '.join(unknown)}")
    pairs = [(source, target) for source, target in pairs if source in wanted]
    if not pairs:
        die("LAYERS selected no layer that this run would load")
elif layers_filter:
    # Set but whitespace-only: a wrapper passing an empty subset must not silently
    # produce an empty database.
    die("LAYERS is set but names no layer")

# The three guards DuckDBToolbox._collect_export_layers and check_table_names
# state. None has a safe automatic repair — a human renames the layer in Pro.
seen = {}
for source, target in pairs:
    if not IDENT.fullmatch(target):
        die(
            f"layer '{source}' maps to table '{target}', which cannot map consistently to "
            "Layers.json and DuckDB. Rename it to start with an ASCII letter or underscore "
            "and use only ASCII letters, digits, underscores, and spaces."
        )
    key = target.casefold()
    if key == "sp_ref":
        die(f"layer '{source}' maps to reserved DuckDB table 'sp_ref' — rename the layer")
    if key in seen:
        die(f"layers '{seen[key]}' and '{source}' both map to table '{target}' — rename one")
    seen[key] = source
    count = meta[source].get("featureCount")
    if count is None:
        count = -1
    fid = meta[source].get("fidColumnName") or ""
    print(f"{source}\t{target}\t{count}\t{fid}")
PY

# One dataset-level `-json -so` is enough: it carries every layer's name,
# featureCount and fidColumnName. A second per-layer ogrinfo was just
# re-opening the GDB for numbers already in this payload.
if ! ogrinfo -json -so "$GDB" 2>>"$LOG_FILE" \
     | python3 "$PLAN_PY" "$LAYERS_JSON" "$LAYERS_FILTER" > "$PLAN"; then
  log "ERROR: could not resolve the layer/table plan (see above)"; exit 1
fi

mapfile -t PLAN_ROWS < "$PLAN"
(( ${#PLAN_ROWS[@]} )) || { log "ERROR: empty load plan"; exit 1; }
log "Loading ${#PLAN_ROWS[@]} table(s):"
for row in "${PLAN_ROWS[@]}"; do
  IFS=$'\t' read -r _src _tbl _n _oid <<< "$row"
  log "  - ${_src} -> ${_tbl}"
done

# ---------------------------------------------------------------------------
# Export each planned table to GeoParquet
# ---------------------------------------------------------------------------
MANIFEST="$STAGE/manifest.tsv"
: > "$MANIFEST"
# Two catalog layers over one feature class are two DuckDB tables of the same
# rows. Export the GDB layer once and hardlink the parquet for the rest.
declare -A PARQUET_FOR_SOURCE

for row in "${PLAN_ROWS[@]}"; do
  IFS=$'\t' read -r L table expected oid <<< "$row"
  oid="${OID_FIELD:-${oid:-$OID_FALLBACK}}"
  pq="$STAGE/$table.parquet"

  if [[ -n "${PARQUET_FOR_SOURCE[$L]+x}" ]]; then
    src_pq="${PARQUET_FOR_SOURCE[$L]}"
    ln "$src_pq" "$pq" 2>/dev/null || cp "$src_pq" "$pq"
    log "--- $L -> $table (reused ${expected} features, OID $oid) ---"
    printf '%s\t%s\t%s\t%s\n' "$table" "$pq" "$expected" "$oid" >> "$MANIFEST"
    continue
  fi

  log "--- $L -> $table (${expected} features, OID $oid) ---"

  OGR_ARGS=(
    -f Parquet "$pq" "$GDB" "$L"
    # M-aware feature classes (a measured pipeline centerline) are XYM/XYZM, which
    # the Parquet writer refuses by default — "Only 2D and Z geometry types are
    # supported". DuckDB's GEOMETRY carries M, and so does the WKB the toolbox
    # exports, so the M must survive the staging hop rather than be dropped.
    --config OGR_PARQUET_ALLOW_ALL_DIMS YES
    -preserve_fid                  # without it FID=<oid> writes a fresh 0-based sequence
    -lco "FID=$oid"                # keep the OID as a real column for the PRIMARY KEY
    -lco GEOMETRY_NAME=shape       # renamed to `geometry` on ingest, toolbox convention
    -lco GEOMETRY_ENCODING=WKB     # plain GeoParquet WKB; no GeoArrow-only readers needed
    -lco WRITE_COVERING_BBOX=NO    # skip GDAL's bbox struct column — not part of the contract
    -lco COMPRESSION=ZSTD
    # No -nlt PROMOTE_TO_MULTI: FileGDB feature classes are single-typed, and the
    # toolbox exports native geometry types (see scripts/README.md flag divergences).
  )
  [[ -n "$TARGET_SRS" ]] && OGR_ARGS+=(-t_srs "$TARGET_SRS")

  if ! ogr2ogr "${OGR_ARGS[@]}" 2>>"$LOG_FILE"; then
    log "ERROR: ogr2ogr failed for layer '$L' (see $LOG_FILE)"; exit 1
  fi
  PARQUET_FOR_SOURCE["$L"]="$pq"
  printf '%s\t%s\t%s\t%s\n' "$table" "$pq" "$expected" "$oid" >> "$MANIFEST"
done

# ---------------------------------------------------------------------------
# Ingest into DuckDB
# ---------------------------------------------------------------------------
# WKT for sp_ref.text. The toolbox stores arcpy's exportToString(); off Pro the
# closest faithful thing is GDAL's WKT1 for the same CRS.
SRS_WKT=""
if [[ -n "$TARGET_SRS" ]] && command -v gdalsrsinfo >/dev/null; then
  SRS_WKT="$(gdalsrsinfo -o wkt1 --single-line "$TARGET_SRS" 2>/dev/null | head -1)" || true
fi

log "--- Ingesting into $DUCKDB_PATH ---"
# `duckdb.connect` creates the file before the transaction opens, so a rolled-back
# first load would leave an empty .ddb behind — which the consumer's `ls /project/*.ddb`
# would then pick as the project database. Remove it iff this run created it.
DB_PREEXISTED=0
[[ -e "$DUCKDB_PATH" ]] && DB_PREEXISTED=1

ingest() {
  "${DUCKDB_PY[@]}" - "$DUCKDB_PATH" "$MANIFEST" "$SRID" "$SRS_WKT" <<'PY' 2>&1 | tee -a "$LOG_FILE"
import pathlib
import sys

import duckdb

db_path, manifest, srid, srs_wkt = sys.argv[1:5]

# DuckDBToolBase.EXCLUDE_NAMES, mirrored: the toolbox drops these from every
# export, so keeping them here would give the same GDB two different schemas
# depending on which tool loaded it.
# ponytail: name-based only. The toolbox also drops CIM-hidden fields, which live
# in the .aprx this script never opens — run `layers-json` if that matters.
EXCLUDE_NAMES = {
    "globalid",
    "shape_length",
    "shape_area",
    "shape__length",
    "shape__area",
    "st_area(shape)",
    "st_perimeter(shape)",
}


def q(name):
    """Quote a DuckDB identifier, escaping embedded double quotes."""
    return '"' + str(name).replace('"', '""') + '"'


def lit(text):
    return "'" + str(text).replace("'", "''") + "'"


con = duckdb.connect(db_path)
con.execute("INSTALL spatial;")
con.execute("LOAD spatial;")

rows = [
    line.split("\t")
    for line in pathlib.Path(manifest).read_text().splitlines()
    if line.strip()
]

# One transaction for the whole load. Every CREATE OR REPLACE TABLE otherwise
# auto-commits, so an assertion tripping on layer 5 of 12 would leave a queryable
# database mixing two exports — the exact "plausible-looking database" the staging
# hop exists to prevent.
con.execute("BEGIN TRANSACTION;")
try:
    for table, parquet, expected, oid_field in rows:
        src = f"read_parquet({lit(parquet)})"
        types = {
            name: dtype
            for name, dtype, *_ in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
        }

        # `geometry` is this format's reserved column, like sp_ref is its reserved
        # table. DuckDB would silently disambiguate a collision to geometry_1 and
        # leave the R-tree bound to the wrong column.
        clash = [n for n in types if n != "shape" and n.casefold() == "geometry"]
        if clash:
            raise SystemExit(
                f"ERROR: {table}: attribute column {clash[0]!r} collides with the reserved "
                "geometry column — rename the field in the source feature class"
            )

        dropped = [
            n for n in types
            if n.casefold() in EXCLUDE_NAMES and n.casefold() != oid_field.casefold()
        ]
        excluded = ", ".join(q(n) for n in (["shape"] if "shape" in types else []) + dropped)
        select = f"* EXCLUDE({excluded})" if excluded else "*"
        if "shape" in types:
            # GDAL's GeoParquet reader hands back a CRS-parameterized
            # GEOMETRY('OGC:CRS84') on current duckdb, a raw WKB blob on older
            # ones. Cast either to the plain GEOMETRY the toolbox writes —
            # RTREE indexes reject the parameterized type outright.
            expr = q("shape") if types["shape"].startswith("GEOMETRY") else f"ST_GeomFromWKB({q('shape')})"
            select += f", {expr}::GEOMETRY AS geometry"

        con.execute(f"CREATE OR REPLACE TABLE {q(table)} AS SELECT {select} FROM {src};")
        loaded = con.execute(f"SELECT count(*) FROM {q(table)}").fetchone()[0]

        # The whole point of staging through host GDAL: a driver that opens the
        # schema but reads 0 features must not produce a plausible database.
        if int(expected) >= 0 and loaded != int(expected):
            raise SystemExit(f"ERROR: {table}: loaded {loaded} rows, source reports {expected}")

        if oid_field in types:
            con.execute(f"ALTER TABLE {q(table)} ADD PRIMARY KEY ({q(oid_field)});")
        if "shape" in types:
            con.execute(f"CREATE INDEX {q(table + '_rtree')} ON {q(table)} USING RTREE (geometry);")

        kind = "geometry" if "shape" in types else "attributes only"
        note = f", dropped {len(dropped)} excluded field(s)" if dropped else ""
        print(f"  {table}: {loaded} rows ({kind}{note})")

    if srid:
        con.execute("CREATE OR REPLACE TABLE sp_ref (wkid INTEGER, text TEXT);")
        con.execute("INSERT INTO sp_ref (wkid, text) VALUES (?, ?)", (int(srid), srs_wkt))
        print(f"  sp_ref: wkid={srid}")
    else:
        # Native mode: a stale row from an earlier reprojected run would declare a
        # CRS these geometries are not in. sp_ref is this tool's own table (the
        # toolbox CREATE OR REPLACEs it too), so dropping it is not the
        # warn-don't-delete case that protects the operator's own tables.
        con.execute("DROP TABLE IF EXISTS sp_ref;")
        print("  sp_ref: dropped (TARGET_SRS empty — database holds native per-layer CRSs)")
except BaseException:
    con.execute("ROLLBACK;")
    raise
con.execute("COMMIT;")
con.close()
PY
}

if ! ingest; then
  (( DB_PREEXISTED )) || rm -f "$DUCKDB_PATH" "$DUCKDB_PATH.wal"
  log "ERROR: ingest failed — $DUCKDB_PATH left $( ((DB_PREEXISTED)) && echo unchanged || echo uncreated )"
  exit 1
fi

log "=== Done: $DUCKDB_PATH ==="
