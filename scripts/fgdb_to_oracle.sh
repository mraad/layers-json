#!/usr/bin/env bash
#
# fgdb_to_oracle.sh
# Bulk-load a File Geodatabase into Oracle Spatial running in a gvenzl/oracle-free
# container (Oracle Linux 8 + Oracle DB 26ai Free).
#
# WHY THIS IS HOST-ORCHESTRATED (not a pure in-container script):
#   The oracle-free image ships Oracle DB only. The newest GDAL available to it
#   (EPEL gdal 3.0.4 on OL8) CANNOT read FileGDBs authored by recent ArcGIS —
#   its OpenFileGDB driver opens the schema but reads 0 features. A modern host
#   GDAL (3.x ≥ 3.4) reads them fine. So the GDAL *read* must happen on the host.
#   Likewise, Homebrew/conda GDAL is built WITHOUT the proprietary OCI driver, so
#   ogr2ogr cannot write SDO_GEOMETRY directly either.
#
#   This script therefore splits the work along the only seam that works:
#     host side       : `ogr2ogr` reads the GDB, reprojects to 4326, and writes one
#                        CSV-with-WKT per layer + an `ogrinfo -json` schema sidecar.
#     in-container side: a python-oracledb loader (THIN mode — no Oracle client)
#                        runs via `docker exec` INSIDE the container, creating a
#                        typed table per layer, registering USER_SDO_GEOM_METADATA,
#                        inserting SDO_GEOMETRY(wkt, SRID), and building a spatial
#                        index. The DB writes all execute inside the container.
#
#   The staging dir + loader are pushed in with `docker cp` (you cannot add a bind
#   mount to an already-running container). To use real `-v` mounts instead, start
#   the container fresh — see the header of the chat / README for that variant.
#
# Per layer you get: a typed table in ORA_USER's schema, a USER_SDO_GEOM_METADATA
# row (X/Y -180..180 / -90..90, tol 0.05, the chosen SRID), and an
# MDSYS.SPATIAL_INDEX_V2 index named <TABLE>_SIDX. Re-runnable: tables are
# DROP ... PURGE'd before recreate.
#
# Config (env vars, all overridable):
#   GDB           host path to the .gdb directory (positional arg; GDB= env fallback)
#   CONTAINER     target container name           (default oracle-spatial)
#   ORA_USER      DB user / schema owner          (default spatial_user)
#   ORA_PASSWORD  DB password                     (required, no default)
#   ORA_DSN       easy-connect DSN, container-internal
#                                                 (default localhost:1521/FREEPDB1)
#   TARGET_SRS    reproject every layer to this   (default EPSG:4326)
#   SRID          Oracle SRID stamped on geoms    (default 4326)
#   TABLE_PREFIX  prefix prepended to table names (default empty)
#   LAYERS        space-separated subset          (default: all non-attachment layers)
#   WORKDIR       staging dir inside container     (default /tmp/work)
#
# Usage:  bash scripts/fgdb_to_oracle.sh <path-to-file.gdb>

set -euo pipefail
(( BASH_VERSINFO[0] >= 4 )) || { echo "ERROR: requires bash 4+ (macOS: brew install bash)" >&2; exit 1; }

# Source File Geodatabase — REQUIRED first positional argument.
# (The GDB env var is still honored as a fallback for existing callers.)
case "${1:-}" in
  -h|--help)
    echo "Usage: $0 <path-to-file.gdb>   (override target via ORA_*/TARGET_SRS/SRID/... env vars)" >&2
    exit 0
    ;;
esac
GDB="${1:-${GDB:-}}"
[[ -n "$GDB" ]] || {
  echo "ERROR: missing required argument: path to a File Geodatabase" >&2
  echo "Usage: $0 <path-to-file.gdb>" >&2
  exit 1
}
LAYERS_FILTER="${LAYERS:-}"
GDB="${GDB%"${GDB##*[!/]}"}"  # drop *every* trailing slash; `%/` drops only one
CONTAINER="${CONTAINER:-oracle-spatial}"
ORA_USER="${ORA_USER:-spatial_user}"
ORA_PASSWORD="${ORA_PASSWORD:-}"   # required — no baked-in default; set to your oracle-free APP_USER_PASSWORD
ORA_DSN="${ORA_DSN:-localhost:1521/FREEPDB1}"
TARGET_SRS="${TARGET_SRS:-EPSG:4326}"
SRID="${SRID:-4326}"
TABLE_PREFIX="${TABLE_PREFIX:-}"
WORKDIR="${WORKDIR:-/tmp/work}"

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Pre-flight (host)
# ---------------------------------------------------------------------------
log "=== FileGDB → Oracle Spatial bulk load ==="
log "Source GDB:   $GDB"
log "Container:    $CONTAINER"
log "Target:       $ORA_USER@$ORA_DSN  (SRID $SRID, reproject ${TARGET_SRS})"

[[ -n "$ORA_PASSWORD" ]] || die "ORA_PASSWORD must be set (e.g. export ORA_PASSWORD=… — your oracle-free APP_USER_PASSWORD)"
[[ -d "$GDB" ]] || die "GDB not found: $GDB"
command -v ogr2ogr >/dev/null || die "ogr2ogr not on PATH (brew install gdal)"
command -v ogrinfo  >/dev/null || die "ogrinfo not on PATH"
command -v python3  >/dev/null || die "python3 not on PATH"
command -v docker   >/dev/null || die "docker not on PATH"
# Matched in-shell, never `ogrinfo --formats | grep -q`: that pipeline races under
# `set -o pipefail` — grep exits at the first match, ogrinfo dies of SIGPIPE (141),
# and the pipeline reports the driver missing when it is present.
[[ "$(ogrinfo --formats 2>/dev/null)" == *OpenFileGDB* ]] || die "OpenFileGDB driver missing"
[[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == "true" ]] \
  || die "container '$CONTAINER' is not running"
log "GDAL: $(ogr2ogr --version)"

# ---------------------------------------------------------------------------
# Inventory layers
# ---------------------------------------------------------------------------
# ogrinfo -json, not a sed of the human listing — same payload the other
# loaders plan from, and attachment sidecars are dropped unless named in LAYERS.
if ! _LAYER_LIST="$(ogrinfo -json -so "$GDB" 2>/dev/null | python3 -c '
import json, re, sys
SYSTEM = re.compile(r"__ATTACH(REL)?$", re.IGNORECASE)
try:
    layers = json.load(sys.stdin).get("layers") or []
except (ValueError, AttributeError, TypeError) as exc:
    sys.exit(f"could not parse ogrinfo -json: {exc}")
names = [layer["name"] for layer in layers if isinstance(layer, dict) and layer.get("name")]
if not names:
    sys.exit("no layers found in the GDB")
wanted = sys.argv[1]
if wanted.strip():
    selected = wanted.split()
    unknown = [name for name in selected if name not in names]
    if unknown:
        sys.exit("LAYERS names no such GDB layer: " + ", ".join(unknown))
    names = [name for name in selected if name in names]
    if not names:
        sys.exit("LAYERS selected no layer")
elif wanted:
    sys.exit("LAYERS is set but names no layer")
else:
    names = [name for name in names if not SYSTEM.search(name)]
    if not names:
        sys.exit("every layer in the GDB is an attachment sidecar")
print("\n".join(names))
' "$LAYERS_FILTER")"; then
  die "could not list GDB layers"
fi
mapfile -t SELECTED <<< "$_LAYER_LIST"
[[ ${#SELECTED[@]} -gt 0 ]] || die "no layers found in GDB"
log "Layers to load (${#SELECTED[@]}): ${SELECTED[*]}"

# ---------------------------------------------------------------------------
# Export each layer on the host: CSV (geometry as WKT, reprojected, 2D) + the
# ogrinfo -json schema sidecar the loader uses for accurate Oracle column types.
# ---------------------------------------------------------------------------
STAGE="$(mktemp -d -t fgdb_oracle.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT
MANIFEST="$STAGE/manifest.tsv"
: > "$MANIFEST"

log "--- Exporting layers (host GDAL) → $STAGE ---"
for L in "${SELECTED[@]}"; do
  slug="$(printf '%s' "$L" | sed -E 's/[^A-Za-z0-9_]/_/g')"
  csv="$STAGE/$slug.csv"
  json="$STAGE/$slug.json"

  ogr2ogr -f CSV "$csv" "$GDB" "$L" \
    -t_srs "$TARGET_SRS" \
    -dim XY \
    -lco GEOMETRY=AS_WKT \
    -lco SEPARATOR=COMMA \
    -lco STRING_QUOTING=IF_AMBIGUOUS \
    2>>"$STAGE/export.log" \
    || die "ogr2ogr export failed for layer '$L' (see $STAGE/export.log)"

  ogrinfo -json -so "$GDB" "$L" > "$json" 2>>"$STAGE/export.log" \
    || die "ogrinfo -json failed for layer '$L'"

  # Header-only CSV = 0 features. `head -2` reads at most 2 lines, so the ~1M-row
  # layer isn't fully re-scanned just for a count (the loader reports real counts).
  if [[ $(head -2 "$csv" | wc -l) -lt 2 ]]; then
    log "  WARN: '$L' exported 0 features — skipping"
    continue
  fi
  printf '%s\t%s\n' "$L" "$slug" >> "$MANIFEST"
  log "  exported $L"
done
[[ -s "$MANIFEST" ]] || die "no non-empty layers exported"

# ---------------------------------------------------------------------------
# Emit the in-container loader (python-oracledb, THIN — needs no Oracle client).
# ---------------------------------------------------------------------------
cat > "$STAGE/loader.py" <<'PYEOF'
import csv, json, os, re, sys
from datetime import datetime, timedelta
import oracledb

STAGE  = os.environ["STAGE_DIR"]
SRID   = int(os.environ.get("SRID", "4326"))
PREFIX = os.environ.get("TABLE_PREFIX", "")
USER   = os.environ["ORA_USER"]
PW     = os.environ["ORA_PASSWORD"]
DSN    = os.environ["ORA_DSN"]
BATCH  = 5000

# The USER_SDO_GEOM_METADATA bounds below are lon/lat world bounds, valid only for a
# geodetic CRS. Layers are reprojected to TARGET_SRS (default EPSG:4326), so SRID is
# normally geodetic; fail loud rather than stamp wrong bounds if it isn't.
GEODETIC_SRIDS = {4326, 4269, 4267, 4258, 4283, 8307}

# Generous: handle wide attribute rows (some layers have 80+ columns).
csv.field_size_limit(2**27)

def ident(name):
    s = re.sub(r'[^A-Za-z0-9_]', '_', name).upper().strip('_')
    if not s or not s[0].isalpha():
        s = 'C_' + s
    return s[:128]

def ora_type(otype, width):
    if otype in ('Integer', 'Integer64', 'Real'):
        return 'NUMBER'
    if otype == 'Date':
        return 'DATE'
    if otype == 'DateTime':
        return 'TIMESTAMP'
    # ponytail: GDB declared width is a soft hint — real values (or multibyte
    # UTF-8 bytes) can exceed it → ORA-12899. VARCHAR2 is variable-length, so
    # 4000 costs nothing and removes the whole class of overflow failures.
    return 'VARCHAR2(4000)'

DT_FORMATS = ('%Y/%m/%d %H:%M:%S', '%Y/%m/%d', '%Y-%m-%dT%H:%M:%S',
              '%Y-%m-%d %H:%M:%S', '%Y-%m-%d')

# ogr2ogr stamps the offset it knows onto a CSV datetime: '2024/07/26 12:07:38+00'
# (also +00:00, +0000, Z). strptime's %z rejects that bare-hour form, so strip the
# offset and fold it in by hand. The Oracle columns are naive TIMESTAMP, so every
# value lands as UTC.
#
# The lookbehind is load-bearing: without it this pattern reads the '-26' of a
# date-only '2024-07-26' as an offset, strips the day, and the value parses as
# nothing — the same silent NULL this whole function exists to prevent.
TZ_SUFFIX = re.compile(r'(?<=\d{2}:\d{2})\s*(Z|[+-]\d{2}(?::?\d{2})?)$')

# A date that fails every format is loaded NULL. Counting them is what turns a
# silently all-NULL column into a visible number on the per-layer line.
BAD_DATES = 0

def parse_dt(v):
    v = v.strip()
    minutes = 0
    m = TZ_SUFFIX.search(v)
    if m:
        tz = m.group(1)
        if tz != 'Z':
            hh, _, mm = tz[1:].partition(':')
            if len(hh) == 4:                      # +0530
                hh, mm = hh[:2], hh[2:]
            hh, mm = int(hh), int(mm or 0)
            if hh > 14 or mm > 59:
                return None                       # not a real UTC offset; count it
            minutes = (hh * 60 + mm) * (1 if tz[0] == '+' else -1)
        v = v[:m.start()]
    for f in DT_FORMATS:
        try:
            return datetime.strptime(v, f) - timedelta(minutes=minutes)
        except ValueError:
            pass
    return None

def cast(v, otype):
    if v is None or v == '':
        return None
    if otype in ('Integer', 'Integer64'):
        try: return int(float(v))
        except ValueError: return None
    if otype == 'Real':
        try: return float(v)
        except ValueError: return None
    if otype in ('Date', 'DateTime'):
        global BAD_DATES
        dt = parse_dt(v)
        if dt is None:
            BAD_DATES += 1
        return dt
    return v

def load(con, layer, slug):
    with open(os.path.join(STAGE, f'{slug}.json'), encoding='utf-8') as fh:
        meta = json.load(fh)['layers'][0]
    gfield = (meta.get('geometryFields') or [{}])[0]
    gtype  = gfield.get('type', 'Point')
    gname  = gfield.get('name')
    # ogr field name -> (oracle col, ogr type)
    cols, used = [], set()
    for f in meta['fields']:
        col = ident(f['name'])
        while col in used or col == 'GEOMETRY':
            col += '_'
        used.add(col)
        cols.append((f['name'], col, f['type'], f.get('width')))

    tbl = ident(PREFIX + layer)
    cur = con.cursor()
    try:
        cur.execute(f'DROP TABLE "{tbl}" PURGE')
    except oracledb.DatabaseError as e:
        if e.args[0].code != 942:        # ORA-00942: table does not exist
            raise

    defs = [f'"{c}" {ora_type(t, w)}' for _, c, t, w in cols]
    defs.append('"GEOMETRY" SDO_GEOMETRY')
    cur.execute(f'CREATE TABLE "{tbl}" ({", ".join(defs)})')

    cur.execute('DELETE FROM USER_SDO_GEOM_METADATA '
                'WHERE TABLE_NAME = :1 AND COLUMN_NAME = :2', [tbl, 'GEOMETRY'])
    cur.execute(
        """INSERT INTO USER_SDO_GEOM_METADATA VALUES (:1, :2,
             SDO_DIM_ARRAY(SDO_DIM_ELEMENT('X', -180, 180, 0.05),
                           SDO_DIM_ELEMENT('Y',  -90,  90, 0.05)), :3)""",
        [tbl, 'GEOMETRY', SRID])

    collist = [f'"{c}"' for _, c, _, _ in cols] + ['"GEOMETRY"']
    values  = [f':{i+1}' for i in range(len(cols))] + [f'SDO_GEOMETRY(:{len(cols)+1}, {SRID})']
    insert  = f'INSERT INTO "{tbl}" ({", ".join(collist)}) VALUES ({", ".join(values)})'

    # Non-point geometries (incl. MultiPoint, lines, polygons) can produce WKT
    # > 4000 chars → bind geometry as CLOB. Only a single Point is guaranteed
    # short, so exact-match it to keep the fast VARCHAR2 path; 'MultiPoint'
    # contains 'Point' but must NOT take it.
    if gtype != 'Point':
        cur.setinputsizes(*([None] * len(cols) + [oracledb.DB_TYPE_CLOB]))

    global BAD_DATES
    bad_before = BAD_DATES
    total = skipped = 0
    batch = []
    attr_names = {orig for orig, _, _, _ in cols}
    with open(os.path.join(STAGE, f'{slug}.csv'), newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        # ogr2ogr -lco GEOMETRY=AS_WKT names the column after the source geometry
        # field (e.g. "shape"), else "WKT". Trust the JSON sidecar's field name;
        # fall back to the one header column not among the attributes.
        fields_ = reader.fieldnames or []
        geomcol = gname if gname in fields_ else \
            next((h for h in fields_ if h not in attr_names), 'WKT')
        for r in reader:
            wkt = (r.get(geomcol) or '').strip()
            if not wkt:
                skipped += 1
                continue
            row = [cast(r.get(orig), t) for orig, _, t, _ in cols]
            row.append(wkt)
            batch.append(row)
            if len(batch) >= BATCH:
                cur.executemany(insert, batch)
                total += len(batch); batch.clear()
    if batch:
        cur.executemany(insert, batch); total += len(batch)

    cur.execute(f'CREATE INDEX "{tbl[:120]}_SIDX" ON "{tbl}"("GEOMETRY") '
                f'INDEXTYPE IS MDSYS.SPATIAL_INDEX_V2')
    con.commit()

    # Every table here is CREATEd fresh, so it starts with no statistics at all and
    # the optimizer falls back to guessing from block counts — which on a join
    # between a 4M-row table and a 1.8k-row one is how you get a nested loop where
    # a hash join belongs. Seconds per table, and it is the one thing that has to
    # be redone after each load.
    cur.execute('BEGIN DBMS_STATS.GATHER_TABLE_STATS(ownname => USER, tabname => :1,'
                ' degree => 2, cascade => TRUE); END;', [tbl])
    notes = []
    if skipped:
        notes.append(f'skipped {skipped} empty-geom')
    if BAD_DATES > bad_before:
        notes.append(f'{BAD_DATES - bad_before} unparsed dates -> NULL')
    note = f' ({", ".join(notes)})' if notes else ''
    print(f'  [ok] {layer:24s} -> {tbl:24s} {total:>9d} rows, '
          f'{len(cols)} cols{note}', flush=True)

def main():
    if SRID not in GEODETIC_SRIDS:
        sys.exit(f'SRID {SRID} is not geodetic; the SDO_DIM bounds assume lon/lat '
                 f'(-180..180 / -90..90). Add it to GEODETIC_SRIDS or supply projected '
                 f'bounds before loading. Known: {sorted(GEODETIC_SRIDS)}')
    con = oracledb.connect(user=USER, password=PW, dsn=DSN)   # thin mode
    print(f'  connected: {USER}@{DSN}', flush=True)
    with open(os.path.join(STAGE, 'manifest.tsv'), encoding='utf-8') as fh:
        items = [ln.rstrip('\n').split('\t') for ln in fh if ln.strip()]
    for layer, slug in items:
        try:
            load(con, layer, slug)
        except Exception as e:
            print(f'  [FAIL] {layer}: {e}', file=sys.stderr, flush=True)
            con.close()
            sys.exit(1)
    con.close()

if __name__ == '__main__':
    main()
PYEOF

# ---------------------------------------------------------------------------
# Push staging into the container and run the loader INSIDE it.
# ---------------------------------------------------------------------------
log "--- Copying staging into $CONTAINER:$WORKDIR ---"
docker exec -u 0 "$CONTAINER" mkdir -p "$WORKDIR"
docker cp "$STAGE/." "$CONTAINER:$WORKDIR"

log "--- Loading (inside container) ---"
START=$SECONDS
docker exec -u 0 \
  -e STAGE_DIR="$WORKDIR" \
  -e ORA_USER="$ORA_USER" -e ORA_PASSWORD="$ORA_PASSWORD" -e ORA_DSN="$ORA_DSN" \
  -e SRID="$SRID" -e TABLE_PREFIX="$TABLE_PREFIX" \
  "$CONTAINER" bash -lc '
    set -e
    command -v python3.9 >/dev/null 2>&1 \
      || microdnf install -y python39 python39-pip >/dev/null 2>&1
    python3.9 -c "import oracledb" >/dev/null 2>&1 \
      || python3.9 -m pip install --quiet oracledb >/dev/null 2>&1
    python3.9 "'"$WORKDIR"'/loader.py"
  '
log "Load complete in $((SECONDS - START))s"

# ---------------------------------------------------------------------------
# Inventory (registered spatial layers + row counts) via sqlplus in-container.
# ---------------------------------------------------------------------------
log "--- Registered spatial metadata ---"
# Creds via -e (not interpolated into the host command line, so they don't leak
# into `ps`); single-quoted payload expands them inside the container.
docker exec -i \
  -e ORA_USER="$ORA_USER" -e ORA_PASSWORD="$ORA_PASSWORD" -e ORA_DSN="$ORA_DSN" \
  "$CONTAINER" \
  bash -lc 'sqlplus -S "$ORA_USER/$ORA_PASSWORD@$ORA_DSN"' <<'SQL'
set linesize 160 pagesize 100 feedback off
col table_name format a28
col column_name format a12
SELECT m.table_name, m.column_name, m.srid
  FROM user_sdo_geom_metadata m ORDER BY m.table_name;
exit
SQL

log "=== Done ==="
