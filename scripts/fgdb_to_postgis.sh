#!/usr/bin/env bash
#
# fgdb_to_postgis.sh
# Bulk-load a File Geodatabase into PostGIS.
#
# Two modes (EXPORT_MODE):
#   load  (default) — load into a live PostgreSQL over a connection, then build
#                     GiST indexes + ANALYZE. Tuned for large GDBs: COPY mode,
#                     large transaction groups, no on-load indexes.
#   dump            — server-free: write a gzipped PGDump .sql.gz artifact from
#                     the GDB alone (no running PG). Drop it in your compose
#                     stack's initdb bind mount (typically docker/initdb/) and
#                     `docker compose up` on a FRESH volume replays it into the
#                     postgis container via /docker-entrypoint-initdb.d.

set -euo pipefail

# mapfile/readarray require bash 4+ (macOS ships 3.2 — `brew install bash`)
(( BASH_VERSINFO[0] >= 4 )) || { echo "ERROR: requires bash 4+ (macOS: brew install bash)" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Source File Geodatabase — REQUIRED first positional argument.
# (The GDB env var is still honored as a fallback for existing callers.)
case "${1:-}" in
  -h|--help)
    echo "Usage: $0 <path-to-file.gdb>   (override target via PG_*/TARGET_SRS/EXPORT_MODE/... env vars)" >&2
    exit 0
    ;;
esac
GDB="${1:-${GDB:-}}"
[[ -n "$GDB" ]] || {
  echo "ERROR: missing required argument: path to a File Geodatabase" >&2
  echo "Usage: $0 <path-to-file.gdb>" >&2
  exit 1
}
# Capture before anything else clobbers LAYERS — this script used to name its
# inventory array LAYERS, which silently discarded the operator's subset.
LAYERS_FILTER="${LAYERS:-}"
# Drop *every* trailing slash; `%/` drops only one, so "x.gdb//" would stem to ""
# and `dirname` of "NorthSea.gdb/" is NorthSea.gdb itself — Layers.json then
# resolves inside the GDB folder.
GDB="${GDB%"${GDB##*[!/]}"}"

# Database name defaults to the lowercased stem of the GDB path
# (e.g. .../NorthSea.gdb -> "NorthSea"). Override with PG_DB to use a different name.
_GDB_STEM="${GDB##*/}" # basename: NorthSea.gdb
_GDB_STEM="${_GDB_STEM%.*}"  # strip extension: NorthSea

PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_DB="${PG_DB:-${_GDB_STEM,,}}"
PG_USER="${PG_USER:-postgres}"
PG_PASSWORD="${PG_PASSWORD:-postgres}"
PG_SCHEMA="${PG_SCHEMA:-${_GDB_STEM,,}}"   # defaults to GDB stem (e.g. "NorthSea")

# Reproject all geometries to this CRS on load. Default WGS84 (EPSG:4326) so every
# imported table lands in SRID 4326. Override to another EPSG to target a different
# CRS; the post-load step asserts the resulting SRID matches.
# `${VAR-default}`, not `${VAR:-default}`: every -t_srs / SRID-verification branch
# below is already guarded on an empty TARGET_SRS, so an explicitly empty value has
# to survive as empty or the documented "keep native per-layer SRIDs" mode is
# unreachable. Unset still defaults to EPSG:4326.
TARGET_SRS="${TARGET_SRS-EPSG:4326}"   # e.g. "EPSG:25831"
EXPECT_SRID="${TARGET_SRS##*:}"   # "EPSG:4326" -> "4326"; empty when TARGET_SRS is

# Transaction group size — bigger = faster, more memory, longer rollback on fail
GROUP_TXN="${GROUP_TXN:-100000}"

# Session maintenance_work_mem for GiST index builds — bigger = faster build, more RAM
MAINT_WORK_MEM="${MAINT_WORK_MEM:-1GB}"

# Concurrent table index builds. 1 = sequential. PostgreSQL does NOT parallelize a
# single GiST build, so N>1 runs one psql backend per table across cores.
# Peak RAM ≈ PARALLEL_IDX × MAINT_WORK_MEM; each job is one connection.
PARALLEL_IDX="${PARALLEL_IDX:-1}"

# Output mode: "load" (live PostgreSQL) or "dump" (server-free PGDump .sql.gz).
EXPORT_MODE="${EXPORT_MODE:-load}"
# Where dump-mode writes the artifact. Compose mounts this into the postgis
# container's /docker-entrypoint-initdb.d, where *.sql.gz replays on first init.
# Relative to the CWD, NOT to this script — deliberately, so running it from a
# consuming stack's checkout (`../layers-json/scripts/fgdb_to_postgis.sh`) still lands in
# that stack's bind mount. Don't make it $BASH_SOURCE-relative.
DUMP_DIR="${DUMP_DIR:-./docker/initdb}"

# Optional operator Layers.json. When present, loaded tables are renamed to match
# the catalog: each layer's GDB source (uri basename) -> its lowercased "name"
# (e.g. Wellbores -> wells). Catalog consumers derive
# table_name from the catalog name, so this makes the enriched catalog map to
# real tables.
# Defaults to a Layers.json sitting next to the GDB; unset/missing -> no rename.
LAYERS_JSON="${LAYERS_JSON:-$(dirname "$GDB")/Layers.json}"

LOG_DIR="${LOG_DIR:-./logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/load_$(date +%Y%m%d_%H%M%S).log"

# psql connection string and ogr2ogr connection string.
# Password is passed via PGPASSWORD (read by libpq/GDAL), NOT in the connstring,
# so it never appears in `ps` output while ogr2ogr runs.
export PGPASSWORD="$PG_PASSWORD"
PSQL="psql -h $PG_HOST -p $PG_PORT -U $PG_USER -d $PG_DB -v ON_ERROR_STOP=1"
# Maintenance connection (to an always-present DB) used only to CREATE the target DB.
PSQL_MAINT="psql -h $PG_HOST -p $PG_PORT -U $PG_USER -d ${PG_MAINT_DB:-postgres} -v ON_ERROR_STOP=1"
# active_schema puts the target schema on search_path so -overwrite finds and drops
# existing tables (without it, re-runs fail: "Layer ... already exists, CreateLayer failed").
PG_OGR="PG:host=$PG_HOST port=$PG_PORT dbname=$PG_DB user=$PG_USER active_schema=$PG_SCHEMA"

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

declare -A RENAME_MAP   # laundered_table -> catalog target name (populated below)

# Build RENAME_MAP from the operator Layers.json. Each catalog layer's GDB source
# (uri basename, e.g. Wellbores) maps to its lowercased "name"
# (Wells -> wells). The loaded table is the lowercased GDB
# layer name (ogr2ogr LAUNDER=YES), so map lower(gdb_layer) -> target.
build_rename_map() {
  [[ -f "$LAYERS_JSON" ]] || { log "Layers.json not found ($LAYERS_JSON) — keeping laundered table names"; return; }
  command -v python3 >/dev/null || { log "WARN: python3 missing — skipping catalog rename"; return; }
  local from to n=0 output
  # Command substitution (not process substitution) so a parse error is a
  # hard failure — a corrupt catalog must not silently skip every rename.
  if ! output="$(python3 - "$LAYERS_JSON" <<'PY'
import json, re, sys

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        catalog = json.load(handle)
except (OSError, ValueError) as exc:
    sys.exit(f"could not read {sys.argv[1]}: {exc}")
if not isinstance(catalog, dict):
    sys.exit(f"{sys.argv[1]} must contain a JSON object")

layers = catalog.get("layers")
if not isinstance(layers, list):
    sys.exit(f"{sys.argv[1]} must contain a layers list")
for layer in layers:
    if not isinstance(layer, dict):
        continue
    uri = (layer.get("uri") or "").replace("\\", "/").rstrip("/")
    gdb_layer = uri.split("/")[-1] if uri else ""
    name = (layer.get("name") or "").strip()
    if not gdb_layer or not name:
        continue
    # ogr2ogr LAUNDER=YES: lowercase and replace non-alphanumerics. Keying on
    # `.lower()` alone misses `Site-Parts` → `site_parts` and the rename never
    # fires.
    laundered = re.sub(r"[^A-Za-z0-9_]", "_", gdb_layer).lower()
    target = name.lower().replace(" ", "_")
    if not IDENT.fullmatch(laundered) or not IDENT.fullmatch(target):
        sys.exit(
            f"catalog layer {name!r} maps to an unsafe table rename "
            f"{laundered!r} -> {target!r}"
        )
    if laundered != target:
        print(f"{laundered}\t{target}")
PY
)"; then
    log "ERROR: could not read $LAYERS_JSON"
    exit 1
  fi
  while IFS=$'\t' read -r from to; do
    [[ -n "$from" && -n "$to" ]] && { RENAME_MAP["$from"]="$to"; n=$((n + 1)); }
  done <<< "$output"
  log "Catalog rename map: $n table(s)"
}

# Emit "ALTER TABLE ... RENAME TO ..." per mapped table (idempotent via IF EXISTS).
# PostGIS geometry_columns is a view, so it reflects the rename automatically.
emit_renames_sql() {
  local from
  # `+`-guard the empty case, then iterate keys plainly. `${#arr[@]}` does NOT
  # survive `set -u` on an empty associative array (bash 5.3: "RENAME_MAP:
  # unbound variable"), which is the common case — a catalog whose names already
  # match the laundered GDB layer names produces no rename rows at all. The
  # `${!RENAME_MAP[@]+...}` form is not the fix either: the `!`+`+` combo triggers
  # indirect expansion of the keys → "invalid variable name" for NON-empty maps.
  # `${arr[*]+x}` tests set-ness without either failure mode; line 354 uses it too.
  [[ -n "${RENAME_MAP[*]+x}" ]] || return 0
  for from in "${!RENAME_MAP[@]}"; do
    echo "ALTER TABLE IF EXISTS \"$PG_SCHEMA\".\"$from\" RENAME TO \"${RENAME_MAP[$from]}\";"
  done
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
log "=== FileGDB → PostGIS bulk load ==="
log "Source GDB:   $GDB"
log "Target:       $PG_HOST:$PG_PORT/$PG_DB schema=$PG_SCHEMA"
log "Target SRS:   ${TARGET_SRS:-<native per-layer>}"
log "Group txn:    $GROUP_TXN"

[[ -d "$GDB" ]] || { log "ERROR: GDB not found: $GDB"; exit 1; }
[[ "$EXPORT_MODE" == "load" || "$EXPORT_MODE" == "dump" ]] \
  || { log "ERROR: EXPORT_MODE must be 'load' or 'dump' (got '$EXPORT_MODE')"; exit 1; }

# Identifier safety: PG_DB / PG_SCHEMA are interpolated into SQL and DDL throughout
# (CREATE DATABASE/SCHEMA, index DDL, geometry_columns lookups). Restrict them to
# plain identifiers so no quoting/escaping is needed and an odd GDB stem or override
# cannot inject SQL.
for _id in "PG_DB=$PG_DB" "PG_SCHEMA=$PG_SCHEMA"; do
  [[ "${_id#*=}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
    || { log "ERROR: unsafe SQL identifier ($_id) — allowed: letter/underscore then letters/digits/underscore. Set ${_id%%=*} explicitly."; exit 1; }
done

command -v ogr2ogr >/dev/null || { log "ERROR: ogr2ogr not on PATH"; exit 1; }
command -v ogrinfo >/dev/null || { log "ERROR: ogrinfo not on PATH"; exit 1; }
command -v python3 >/dev/null || { log "ERROR: python3 not on PATH"; exit 1; }

# Sanity: GDAL version and FileGDB driver availability
log "GDAL: $(ogr2ogr --version)"
# Matched in-shell, never `ogrinfo --formats | grep -q`: that pipeline races under
# `set -o pipefail` — grep exits at the first match, ogrinfo dies of SIGPIPE (141),
# and the pipeline reports the driver missing when it is present.
[[ "$(ogrinfo --formats 2>/dev/null)" == *OpenFileGDB* ]] \
  || { log "ERROR: OpenFileGDB driver missing"; exit 1; }

# Live-server preparation only matters when loading into a running PostgreSQL.
# In dump mode the artifact is generated offline, so skip all of it.
if [[ "$EXPORT_MODE" == "load" ]]; then
  command -v psql >/dev/null || { log "ERROR: psql not on PATH"; exit 1; }

  # Ensure the target database exists. PostgreSQL has no CREATE DATABASE IF NOT EXISTS,
  # so connect to the maintenance DB and create it only when pg_database lacks it.
  $PSQL_MAINT -tAc "SELECT 1" >/dev/null \
    || { log "ERROR: cannot connect to PostgreSQL (maintenance DB ${PG_MAINT_DB:-postgres})"; exit 1; }
  if [[ "$($PSQL_MAINT -tAc "SELECT 1 FROM pg_database WHERE datname='$PG_DB'")" != "1" ]]; then
    log "Database '$PG_DB' not found — creating it"
    $PSQL_MAINT -c "CREATE DATABASE \"$PG_DB\";"
  else
    log "Database '$PG_DB' already exists"
  fi

  # Sanity: target DB reachable + PostGIS installed
  $PSQL -tAc "SELECT 1" >/dev/null \
    || { log "ERROR: cannot connect to PostgreSQL"; exit 1; }

  # postgis = geometry types/ops; pg_trgm = GIN trigram indexes behind the catalog's
  # ILIKE/LIKE text-search hints. Both must exist before any table loads.
  $PSQL -tAc "CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
  PG_VER=$($PSQL -tAc "SELECT version();" | head -n1)
  PG_GIS=$($PSQL -tAc "SELECT PostGIS_Version();")
  log "PostgreSQL: $PG_VER"
  log "PostGIS:    $PG_GIS"

  # Clean slate: drop and recreate the target schema so EVERY table is gone before
  # load (re-runs included). Guarded: never DROP "public" — PostGIS's extension and
  # spatial_ref_sys live there, so a public clean-slate would destroy the install.
  if [[ "$PG_SCHEMA" == "public" ]]; then
    log "WARN: PG_SCHEMA=public — skipping clean-slate (would drop PostGIS); relying on per-table -overwrite"
    $PSQL -c "CREATE SCHEMA IF NOT EXISTS \"$PG_SCHEMA\";"
  else
    log "Clean-slate: DROP SCHEMA \"$PG_SCHEMA\" CASCADE; CREATE SCHEMA"
    $PSQL -c "DROP SCHEMA IF EXISTS \"$PG_SCHEMA\" CASCADE; CREATE SCHEMA \"$PG_SCHEMA\";"
  fi
fi

# ---------------------------------------------------------------------------
# Inventory layers
# ---------------------------------------------------------------------------
log "--- Inventorying layers ---"
# ogrinfo -json, not a sed of the human listing: the listing's "N: name (type)"
# vs "Layer: name (type)" form already split PostGIS from Oracle, and a layer
# name with parentheses would truncate. Same payload the DuckDB loader plans from.
if ! _LAYER_LIST="$(ogrinfo -json -so "$GDB" 2>>"$LOG_FILE" | python3 -c '
import json, sys
try:
    layers = json.load(sys.stdin).get("layers") or []
except (ValueError, AttributeError, TypeError) as exc:
    sys.exit(f"could not parse ogrinfo -json: {exc}")
names = [layer["name"] for layer in layers if isinstance(layer, dict) and layer.get("name")]
if not names:
    sys.exit("no layers found in the GDB")
print("\n".join(names))
')"; then
  log "ERROR: could not list GDB layers (see $LOG_FILE)"
  exit 1
fi
mapfile -t GDB_LAYERS <<< "$_LAYER_LIST"
if [[ ${#GDB_LAYERS[@]} -eq 0 ]]; then
  log "ERROR: no layers found in GDB"
  exit 1
fi

if [[ -n "$LAYERS_FILTER" ]]; then
  if [[ ! "$LAYERS_FILTER" =~ [^[:space:]] ]]; then
    log "ERROR: LAYERS is set but names no layer"; exit 1
  fi
  declare -A _HAVE=()
  for L in "${GDB_LAYERS[@]}"; do _HAVE["$L"]=1; done
  SELECTED=()
  # Quoted array walk, not `for w in $LAYERS_FILTER`: that pathname-expands
  # `LAYERS="*"`. DuckDB/Oracle parse in Python, which does not glob.
  read -r -a requested <<< "$LAYERS_FILTER"
  for w in "${requested[@]}"; do
    [[ -n "${_HAVE[$w]+x}" ]] || { log "ERROR: LAYERS names no such GDB layer: $w"; exit 1; }
    SELECTED+=("$w")
  done
  [[ ${#SELECTED[@]} -gt 0 ]] || { log "ERROR: LAYERS selected no layer"; exit 1; }
else
  SELECTED=("${GDB_LAYERS[@]}")
fi

log "Found ${#SELECTED[@]} layer(s) to load:"
for L in "${SELECTED[@]}"; do log "  - $L"; done

build_rename_map

# ---------------------------------------------------------------------------
# Dump mode — server-free PGDump artifact for docker-entrypoint-initdb.d
# ---------------------------------------------------------------------------
if [[ "$EXPORT_MODE" == "dump" ]]; then
  mkdir -p "$DUMP_DIR"
  # 99- prefix sorts after the postgis image's own 10_/20_ init scripts, so the
  # postgis extension already exists in the target DB when this replays.
  DUMP_FILE="${DUMP_FILE:-$DUMP_DIR/99-load-$PG_SCHEMA.sql.gz}"
  log "--- Writing PGDump artifact: $DUMP_FILE (schema=$PG_SCHEMA) ---"

  _TMP_SQL="$(mktemp)"
  trap 'rm -f "$_TMP_SQL"' EXIT
  PGDUMP_ARGS=(
    -f PGDump "$_TMP_SQL" "$GDB"
    -lco "SCHEMA=$PG_SCHEMA"
    -lco "GEOMETRY_NAME=geometry"  # catalog convention: both consumers default to "geometry"
    -lco "FID=gid"
    -lco "SPATIAL_INDEX=GIST"      # emit CREATE INDEX ... USING GIST in the dump
    -lco "PRECISION=NO"
    -lco "LAUNDER=YES"
    # No -nlt PROMOTE_TO_MULTI — load native geometry types (see load-mode note).
    -skipfailures                  # drop null/empty-geometry rows that COPY can't encode
    --config PG_USE_COPY YES       # emit COPY blocks, not per-row INSERT (smaller, faster restore)
  )
  [[ -n "$TARGET_SRS" ]] && PGDUMP_ARGS+=(-t_srs "$TARGET_SRS")
  [[ -n "$LAYERS_FILTER" ]] && PGDUMP_ARGS+=("${SELECTED[@]}")

  if ! ogr2ogr "${PGDUMP_ARGS[@]}" 2>>"$LOG_FILE"; then
    log "ERROR: ogr2ogr PGDump failed (see $LOG_FILE)"; exit 1
  fi

  # Offline analog of the live-load SRID check: assert every geometry column in the
  # dump declares EXPECT_SRID. PGDump emits geometry(Type,SRID) typmods and/or
  # AddGeometryColumn(...,SRID,...) — both carry the SRID; COPY data rows are not
  # matched by the anchored regex, and 1-2 digit dimension args fall below {4,6}.
  if [[ -n "$EXPECT_SRID" ]]; then
    # `grep -vx` exits 1 when nothing differs (the all-correct case); under
    # `set -o pipefail` that would abort the script, so tolerate it with `|| true`.
    BAD_SRID=$(grep -oiE 'geometry\([A-Za-z]+, *[0-9]+\)|addgeometrycolumn\([^;]*\)' "$_TMP_SQL" \
      | grep -oE '[0-9]{4,6}' | sort -u | grep -vx "$EXPECT_SRID" | paste -sd, -) || true
    if [[ -n "$BAD_SRID" ]]; then
      log "ERROR: dump has geometries not in SRID $EXPECT_SRID (found: $BAD_SRID)"; exit 1
    fi
    log "OK: dump geometries are SRID $EXPECT_SRID"
  fi

  # The PGDump driver emits AddGeometryColumn + GiST CREATE INDEX, plus a bare
  # "CREATE SCHEMA" per layer — rewrite those to IF NOT EXISTS so the repeats
  # don't abort replay under the entrypoint's ON_ERROR_STOP. Prepend an
  # idempotent CREATE EXTENSION so the dump also replays on a plain postgres,
  # then gzip — the postgres entrypoint gunzips *.sql.gz on first init.
  {
    echo "CREATE EXTENSION IF NOT EXISTS postgis;"
    echo "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
    # Clean slate on replay: drop the target schema before the (rewritten) CREATE
    # SCHEMA IF NOT EXISTS recreates it, so a replay into a populated DB starts empty
    # and the end-of-dump renames can't collide. Guarded: never drop "public".
    [[ "$PG_SCHEMA" != "public" ]] && echo "DROP SCHEMA IF EXISTS \"$PG_SCHEMA\" CASCADE;"
    sed -E 's/^CREATE SCHEMA "([^"]+)";/CREATE SCHEMA IF NOT EXISTS "\1";/' "$_TMP_SQL"
    # Rename laundered tables to catalog names (after their CREATE/COPY/index blocks).
    emit_renames_sql
  } | gzip -c > "$DUMP_FILE"
  rm -f "$_TMP_SQL"; trap - EXIT

  log "=== Done. Dump: $DUMP_FILE ($(du -h "$DUMP_FILE" | cut -f1)) ==="
  log "Replay with a FRESH stack:  docker compose down -v && docker compose up"
  exit 0
fi

# ---------------------------------------------------------------------------
# Bulk load — single ogr2ogr call, all layers, NO indexes during load
# ---------------------------------------------------------------------------
log "--- Bulk loading (this is the slow part) ---"
START=$SECONDS

OGR_ARGS=(
  -f PostgreSQL "$PG_OGR" "$GDB"
  -progress
  -overwrite
  -lco "SCHEMA=$PG_SCHEMA"
  -lco "GEOMETRY_NAME=geometry"  # catalog convention: both consumers default to "geometry"
  -lco "FID=gid"
  -lco "SPATIAL_INDEX=NONE"      # critical: build indexes AFTER load
  -lco "PRECISION=NO"            # promote narrow numerics to float8/int8
  -lco "LAUNDER=YES"             # lowercase, alphanum-safe names
  # No -nlt PROMOTE_TO_MULTI: FileGDB feature classes are single-typed, so the
  # native geometry loads cleanly (points -> POINT, not MULTIPOINT). The render
  # writer handles both single and multi parts, so promotion is unneeded here.
  -gt "$GROUP_TXN"
  --config PG_USE_COPY YES
  --config PG_USE_BASE64 YES
  --config OGR_TRUNCATE NO
)

# Add reprojection if requested
if [[ -n "$TARGET_SRS" ]]; then
  OGR_ARGS+=(-t_srs "$TARGET_SRS")
fi
[[ -n "$LAYERS_FILTER" ]] && OGR_ARGS+=("${SELECTED[@]}")

# Run it. Redirect both streams so -progress dots still surface to console.
if ogr2ogr "${OGR_ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"; then
  ELAPSED=$((SECONDS - START))
  log "Bulk load complete in ${ELAPSED}s"
else
  log "ERROR: ogr2ogr failed"
  exit 1
fi

# Rename laundered tables to catalog names before index discovery so indexes and
# the final inventory use the catalog-aligned names.
if [[ -n "${RENAME_MAP[*]+x}" ]]; then
  log "--- Renaming ${#RENAME_MAP[@]} table(s) to catalog names ---"
  emit_renames_sql | $PSQL 2>&1 | tee -a "$LOG_FILE"
fi

# ---------------------------------------------------------------------------
# Post-load: spatial indexes + ANALYZE per table
# ---------------------------------------------------------------------------
log "--- Building spatial indexes ---"
START=$SECONDS

# Discover what actually landed (handles laundered names, geometry_columns is authoritative)
mapfile -t LOADED < <($PSQL -tAc "
  SELECT f_table_name
  FROM geometry_columns
  WHERE f_table_schema = '$PG_SCHEMA'
  ORDER BY f_table_name;
")

log "Tables with geometry: ${#LOADED[@]} (maintenance_work_mem=$MAINT_WORK_MEM, parallel=$PARALLEL_IDX)"

if [[ ${#LOADED[@]} -eq 0 ]]; then
  log "WARN: no geometry tables found in $PG_SCHEMA — skipping index build"
else
  # Build GiST + ANALYZE for one table in its own psql session. Each session sets
  # maintenance_work_mem, so PARALLEL_IDX concurrent builds each get the full budget.
  build_index() {
    local tbl="$1"
    local idx="${tbl}_geom_gist"
    $PSQL <<SQL
SET maintenance_work_mem = '$MAINT_WORK_MEM';
\echo   GiST + ANALYZE: $PG_SCHEMA.$tbl
CREATE INDEX IF NOT EXISTS "$idx" ON "$PG_SCHEMA"."$tbl" USING GIST (geometry);
ANALYZE "$PG_SCHEMA"."$tbl";
SQL
  }
  export -f build_index
  export PSQL PG_SCHEMA MAINT_WORK_MEM PGPASSWORD

  # xargs -P runs up to PARALLEL_IDX builds at once, refilling the pool as each
  # finishes. PARALLEL_IDX=1 → sequential. A nonzero psql exit (ON_ERROR_STOP)
  # makes xargs exit nonzero → pipefail aborts the script.
  printf '%s\n' "${LOADED[@]}" \
    | xargs -P "$PARALLEL_IDX" -I {} bash -c 'build_index "$1"' _ {} 2>&1 \
    | tee -a "$LOG_FILE"
fi

ELAPSED=$((SECONDS - START))
log "Indexes built in ${ELAPSED}s"

# ---------------------------------------------------------------------------
# Verify every loaded geometry landed in the expected SRID
# ---------------------------------------------------------------------------
if [[ -z "$EXPECT_SRID" ]]; then
  log "--- Skipping SRID verification (no TARGET_SRS; native per-layer SRIDs kept) ---"
else
  log "--- Verifying all geometries are SRID $EXPECT_SRID ---"
  BAD_SRID=$($PSQL -tAc "
    SELECT string_agg(f_table_name || '=' || srid, ', ' ORDER BY f_table_name)
    FROM geometry_columns
    WHERE f_table_schema = '$PG_SCHEMA' AND srid <> $EXPECT_SRID;
  ")
  if [[ -n "$BAD_SRID" ]]; then
    log "ERROR: tables not in SRID $EXPECT_SRID: $BAD_SRID"
    exit 1
  fi
  log "OK: all ${#LOADED[@]} geometry tables are SRID $EXPECT_SRID"
fi

# ---------------------------------------------------------------------------
# Final inventory
# ---------------------------------------------------------------------------
log "--- Final inventory ---"
$PSQL <<SQL | tee -a "$LOG_FILE"
\pset format aligned
SELECT
  f_table_name                                AS table,
  type                                        AS geom_type,
  srid,
  coord_dimension                             AS dims,
  (SELECT reltuples::bigint
     FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='$PG_SCHEMA' AND c.relname=f_table_name) AS approx_rows
FROM geometry_columns
WHERE f_table_schema = '$PG_SCHEMA'
ORDER BY f_table_name;
SQL

log "=== Done. Log: $LOG_FILE ==="
