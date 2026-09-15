#!/usr/bin/env python3
"""Author ``Layers.json`` from an ``.aprx`` + File GDB, without arcpy.

This reader shares catalog serialization and hints with the ArcGIS Pro toolbox.
It reads metadata directly from two sources:

* the **.aprx** — a plain ZIP of CIM JSON. ``<Map>/<Map>.json`` holds the layer order,
  ``<Map>/<Layer>.json`` holds what ``layer.getDefinition("V3")`` returned: the layer
  name, ``displayField``, ``definitionExpression``, the data connection, and the
  per-field ``alias`` / ``visible`` flags. Field aliases in particular live *only*
  here — ``HideUpdateTool`` rewrites them on the layer, not on the feature class.
* the **File GDB** — or, for an enterprise (SDE) layer, the PostgreSQL database
  behind it, whose password comes from libpq (``PGPASSWORD`` / ``~/.pgpass``),
  never from the .aprx — via GDAL/OGR — field names and types, coded/range domains, and
  the sample values. ``GDB_Items`` (a system table OGR exposes as a layer) carries
  the two things OGR has no API for: ``Definition`` XML (subtypes, the FC alias,
  the shape/area/length/globalID field names) and ``Documentation`` XML (the
  ``idPurp`` summary and the ``//attr/attrdef`` column descriptions).

Catalog serialization and query hints come from ``layers_json.catalog``, shared
with the ArcGIS Pro toolbox. This reader never loads a toolbox or imports ArcPy.

``--pg-table`` catalogs a PostGIS table the map does not carry — with no ``.aprx``
at all, if that is the whole catalog — over the same libpq connection an SDE layer
uses.

To diff the result against a catalog ArcGIS Pro authored, normalize ``uri`` on
both sides — Pro records a Windows path, this records the real local one::

    diff <(jq '.layers[].uri="_"' pro.json) <(jq '.layers[].uri="_"' mine.json)

Requires GDAL's Python bindings, an optional extra (``fgdb``); the tool exits with
an install hint rather than a traceback when they are missing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any

from layers_json import catalog

try:  # Optional extra — see GDAL_MISSING below.
    from osgeo import gdal, ogr
except ModuleNotFoundError:  # pragma: no cover - the whole point is not to raise
    gdal = ogr = None

GDAL_MISSING = """\
error: this tool needs GDAL's Python bindings (osgeo), an optional extra:

    uv sync --extra dev --extra fgdb      # from a checkout
    uv tool install "layers-json[fgdb]"   # as an installed tool

The binding must match the system GDAL and the extra is only a floor, so pin it
when a bare resolve outruns `gdal-config --version`:

    pip install "gdal==$(gdal-config --version).*"
"""


def require_gdal() -> None:
    """Exit with an install hint instead of an ImportError traceback."""
    if ogr is None:
        raise SystemExit(GDAL_MISSING)
    # Make OpenEx failures deterministic now and after GDAL 4 flips the default.
    gdal.UseExceptions()


def close_dataset(ds) -> None:
    """Release a GDAL dataset handle. Older bindings have no ``Close()``."""
    if ds is None:
        return
    closer = getattr(ds, "Close", None)
    if callable(closer):
        closer()


# ---------------------------------------------------------------------------
# .aprx — a ZIP of CIM JSON
# ---------------------------------------------------------------------------
@dataclass
class CimLayer:
    """One operational layer as the map defines it."""

    name: str
    dataset: str
    # The workspace as ``uri`` records it: a File GDB path, or a
    # ``<host>:<port>/<database>`` label for an enterprise (SDE) connection —
    # never opened directly in that case, and never carrying a secret.
    gdb: Path
    # Set only when what OGR opens differs from ``gdb``: the ``PG:`` string.
    conn: str
    display: str
    aliases: dict[str, str] = field(default_factory=dict)
    hidden: set[str] = field(default_factory=set)
    is_table: bool = False
    # ``listLayers()`` path, ``Group\\Name``, used for --include/--exclude.
    # Catalog ``name`` stays the layer's own name — groups are a UI path only.
    long_name: str = ""
    definition_query: str = ""

    def __post_init__(self) -> None:
        if not self.long_name:
            self.long_name = self.name


def _resolve_workspace(conn: str, aprx_dir: Path) -> Path | None:
    """Turn ``DATABASE=.\\NorthSea.gdb`` into an absolute path."""
    match = re.search(r"DATABASE=([^;]+)", conn)
    if not match:
        return None
    raw = match.group(1).strip().replace("\\", "/")
    path = Path(raw)
    return path if path.is_absolute() else (aprx_dir / path).resolve()


def _pg_quote(value: str) -> str:
    """Quote one libpq keyword/value. A space separates parameters unless quoted,
    and database, host and role names may legally hold one."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _pg(params: dict[str, str]) -> tuple[str, str] | None:
    """Build ``(OGR "PG:" string, credential-free label)`` from SDE params + libpq env.

    The password is deliberately *not* among them — ArcGIS stores it encrypted in
    the .aprx, and a catalog is no place to write one back out. libpq supplies it
    (``PGPASSWORD`` or ``~/.pgpass``), and its env vars override host/port/user/
    database, which is how a project authored against a server reaches, say, a
    local container on another port. ``params`` is empty for ``--pg-table``,
    where the environment is the whole connection.
    """
    database = os.environ.get("PGDATABASE") or params.get("DATABASE", "")
    if not database:
        return None
    # INSTANCE is `sde:postgresql:<host>[,<port>]`; SERVER repeats the host alone.
    host, _, port = params.get("INSTANCE", "").split(":")[-1].partition(",")
    host = os.environ.get("PGHOST") or host or params.get("SERVER") or "localhost"
    port = os.environ.get("PGPORT") or port or "5432"
    parts = [
        f"dbname={_pg_quote(database)}",
        f"host={_pg_quote(host)}",
        f"port={_pg_quote(port)}",
    ]
    user = os.environ.get("PGUSER") or params.get("USER")
    if user:
        parts.append(f"user={_pg_quote(user)}")
    return "PG:" + " ".join(parts), f"{host}:{port}/{database}"


def _pg_connection(conn: str) -> tuple[str, str] | None:
    """Turn an SDE workspace connection string into what ``_pg`` returns."""
    params: dict[str, str] = {}
    for part in conn.split(";"):
        key, sep, value = part.partition("=")
        if sep:
            params[key.strip().upper()] = value.strip()
    if "postgres" not in params.get("DBCLIENT", "").lower():
        return None  # only the PostgreSQL flavour has an OGR equivalent
    return _pg(params)


def _cim_layer(doc: dict[str, Any], aprx_dir: Path, *, is_table: bool) -> CimLayer | None:
    table = doc.get("featureTable") or {}
    conn = table.get("dataConnection") or {}
    factory = conn.get("workspaceFactory")
    connection = conn.get("workspaceConnectionString", "")
    pg = ""
    if factory == "FileGDB":
        gdb = _resolve_workspace(connection, aprx_dir)
    elif factory == "SDE":
        resolved = _pg_connection(connection)
        pg, gdb = ("", None) if resolved is None else (resolved[0], Path(resolved[1]))
    else:
        return None
    if gdb is None:
        return None
    descriptions = table.get("fieldDescriptions") or []
    return CimLayer(
        name=doc.get("name") or "",
        dataset=conn.get("dataset") or "",
        gdb=gdb,
        conn=pg,
        display=table.get("displayField") or "",
        aliases={d["fieldName"]: d.get("alias") or d["fieldName"] for d in descriptions},
        # arcpy reads visibility off the same CIM field descriptions.
        hidden={d["fieldName"].casefold() for d in descriptions if not d.get("visible")},
        is_table=is_table,
        definition_query=_definition_query(doc, table),
    )


def _definition_query(doc: dict[str, Any], table: dict[str, Any]) -> str:
    """The layer's definition query, if CIM stored one.

    PrepareTool samples through the map layer, so arcpy applies this automatically.
    OGR reads the feature class; without the filter, sample values (and row
    counts) diverge from what Pro authors.
    """
    for source in (table, doc):
        expr = source.get("definitionExpression")
        if isinstance(expr, str) and expr.strip():
            return expr.strip()
    return ""


def pg_layers(specs: list[str]) -> list[CimLayer]:
    """Turn ``--pg-table <schema>.<table>[=<Layer Name>[:<display field>]]`` into
    ``CimLayer`` objects.

    The catalog entry a map layer would have carried comes from the spec instead:
    the name is what ``Layers.json`` calls the layer, the table is what OGR reads,
    and the optional display field is the one a map's ``displayField`` would have
    named — without it the first column stands in, which for a table keyed on a
    surrogate id is a number nobody can read. Field aliases and hidden fields have
    no source at all here, so the column names stand in for their own aliases.
    """
    if not specs:
        return []
    resolved = _pg({})
    if resolved is None:
        raise SystemExit(
            "error: --pg-table needs a database: set PGDATABASE (plus PGHOST / PGPORT / "
            "PGUSER as needed; the password comes from PGPASSWORD or ~/.pgpass)."
        )
    conn, label = resolved
    layers = []
    for spec in specs:
        dataset, _, rest = spec.partition("=")
        name, _, display = rest.partition(":")
        dataset = dataset.strip()
        if not dataset:
            raise SystemExit(f"error: --pg-table {spec!r} names no table.")
        # An unnamed table is catalogued under its own bare name.
        name = name.strip() or dataset.rsplit(".", 1)[-1]
        layers.append(
            CimLayer(
                name=name,
                dataset=dataset,
                gdb=Path(label),
                conn=conn,
                display=display.strip(),
            )
        )
    return layers


def map_documents(archive: zipfile.ZipFile, names: set[str]) -> list[tuple[str, dict[str, Any]]]:
    """Every ``CIMMap`` document in the project, in ``Index.json`` order.

    The map's entry is ``<Map Name>/<Map Name>.json``, not a fixed ``Map/Map.json``
    — a project whose map is not named "Map" has no such member. ``Index.json``
    names them; scanning every JSON member is the fallback for a project without one.
    """
    entries = []
    if "Index.json" in names:
        try:
            index = json.loads(archive.read("Index.json"))
            entries = [
                node.get("FileName")
                for node in index.get("Nodes", [])
                if node.get("NodeType") == "Map" and node.get("FileName") in names
            ]
        except (AttributeError, json.JSONDecodeError):
            entries = []
    if entries:
        maps = []
        for entry in dict.fromkeys(entries):
            try:
                doc = json.loads(archive.read(entry))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if doc.get("type") == "CIMMap":
                maps.append((entry, doc))
        if maps:
            return maps

    maps = []
    for entry in sorted(names):
        # Map documents are ``<MapName>/<MapName>.json``. Layer JSON lives in
        # the same folder, so scanning every ``*.json`` would parse the whole
        # map and, worse, treat a layer that happens to carry ``type: CIMMap``
        # as a second map — forcing ``--map`` on a single-map project.
        path = PurePosixPath(entry)
        if len(path.parts) != 2 or path.suffix.lower() != ".json" or path.stem != path.parent.name:
            continue
        try:
            doc = json.loads(archive.read(entry))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if doc.get("type") == "CIMMap":
            maps.append((entry, doc))
    return maps


def select_map(
    archive: zipfile.ZipFile, names: set[str], map_name: str | None
) -> tuple[str, dict[str, Any]]:
    """The one map to read. A multi-map project has no reliable "active map" in the
    archive, so it must be named."""
    maps = map_documents(archive, names)
    if map_name is not None:
        maps = [(entry, doc) for entry, doc in maps if doc.get("name") == map_name]
        if not maps:
            raise ValueError(f"project has no map named {map_name!r}")
    elif len(maps) > 1:
        available = ", ".join(repr(doc.get("name") or entry) for entry, doc in maps)
        raise ValueError(f"project has multiple maps; choose one with --map: {available}")
    if not maps:
        raise ValueError("project has no map")
    return maps[0]


def read_aprx(path: Path, map_name: str | None = None) -> tuple[list[CimLayer], list[str]]:
    """Return the map's operational File GDB / SDE layers, plus names that were skipped.

    Preserves the map's layer order, which is the order ``listLayers()`` walks and
    therefore the order the layers land in ``Layers.json``. Group layers are
    flattened the same way: Pro yields the children, not the group.
    """
    aprx_dir = path.parent
    layers: list[CimLayer] = []
    skipped: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        try:
            _entry, cim_map = select_map(archive, names, map_name)
        except ValueError as exc:
            raise RuntimeError(f"{path}: {exc}") from exc

        seen: set[str] = set()

        def visit(refs: list[str], *, is_table: bool, parents: tuple[str, ...] = ()) -> None:
            for ref in refs:
                entry = ref.removeprefix("CIMPATH=")
                if entry in seen or entry not in names:
                    continue
                seen.add(entry)
                doc = json.loads(archive.read(entry))
                if not is_table and doc.get("type") == "CIMGroupLayer":
                    # Recurse before the Operational check: a group is a folder,
                    # not a drawable, and listLayers() still walks its children.
                    visit(
                        doc.get("layers") or [],
                        is_table=False,
                        parents=(*parents, doc.get("name") or ""),
                    )
                    continue
                # Basemaps carry layerType BasemapBackground/BasemapTopReference.
                if not is_table and doc.get("layerType") != "Operational":
                    continue
                if doc.get("type") not in ("CIMFeatureLayer", "CIMStandaloneTable"):
                    continue
                layer = _cim_layer(doc, aprx_dir, is_table=is_table)
                if layer is None:
                    skipped.append(doc.get("name") or entry)
                    continue
                if parents:
                    layer.long_name = "\\".join((*parents, layer.name))
                layers.append(layer)

        visit(cim_map.get("layers") or [], is_table=False)
        visit(cim_map.get("standaloneTables") or [], is_table=True)
    return layers, skipped


# ---------------------------------------------------------------------------
# File GDB
# ---------------------------------------------------------------------------
@cache
def _dtype_table() -> dict[tuple[int, int], str]:
    """OGR ``(type, subtype)`` -> the arcpy type name ``PrepareTool`` would report.

    Built on demand because the ``ogr`` constants do not exist until GDAL imports.
    """
    return {
        (ogr.OFTString, ogr.OFSTNone): "String",
        (ogr.OFTInteger, ogr.OFSTNone): "Integer",
        (ogr.OFTInteger, ogr.OFSTInt16): "SmallInteger",
        (ogr.OFTInteger, ogr.OFSTBoolean): "Boolean",
        (ogr.OFTInteger64, ogr.OFSTNone): "BigInteger",
        (ogr.OFTReal, ogr.OFSTNone): "Double",
        (ogr.OFTReal, ogr.OFSTFloat32): "Single",
        (ogr.OFTDateTime, ogr.OFSTNone): "Date",
        (ogr.OFTDate, ogr.OFSTNone): "DateOnly",
        (ogr.OFTTime, ogr.OFSTNone): "TimeOnly",
        (ogr.OFTBinary, ogr.OFSTNone): "Blob",
    }


def field_dtype(defn: ogr.FieldDefn) -> str:
    """Map an OGR field to the arcpy type name ``PrepareTool`` would have seen."""
    return _dtype_table().get((defn.GetType(), defn.GetSubType())) or defn.GetTypeName() or "String"


@dataclass
class GdbItem:
    """The ArcGIS-only metadata for one feature class, from ``GDB_Items``."""

    alias: str = ""
    subtype_field: str = ""
    subtypes: dict[str, str] = field(default_factory=dict)
    system_fields: set[str] = field(default_factory=set)
    shape_type: str = ""
    # The two values distilled from the Documentation XML, never the XML itself:
    # one NorthSea dataset carries 1.5MB of it to yield ~660 bytes. Unused
    # datasets are skipped before that blob is read.
    summary: str = ""
    column_infos: dict[str, dict] = field(default_factory=dict)


def _parse_xml(text: str):
    """Parse an ArcGIS definition/documentation blob; None when absent or unparseable."""
    if not text:
        return None
    try:
        return catalog._safe_fromstring(text)
    except ValueError:  # an unparseable item simply carries no metadata
        return None


def read_gdb_items(prepare, ds: gdal.Dataset, wanted: set[str] | None = None) -> dict[str, GdbItem]:
    """Parse ``GDB_Items`` into per-dataset metadata.

    This is where ``arcpy.da.ListSubtypes`` and ``arcpy.metadata.Metadata`` come
    from: OGR has no API for either, but both are plain XML in this table.
    ``wanted`` is the short names of datasets the map actually references —
    other items are skipped so unused Documentation blobs are never parsed.
    """
    try:
        layer = ds.GetLayerByName("GDB_Items")
    except RuntimeError:  # not a GDB at all (SDE) — UseExceptions() raises here
        return {}
    if layer is None:
        return {}
    wanted_folded = {name.casefold() for name in wanted} if wanted is not None else None
    items: dict[str, GdbItem] = {}
    try:
        layer.ResetReading()
        for feature in layer:
            name = (feature.GetFieldAsString("Name") or "").rsplit("\\", 1)[-1]
            if not name:
                continue
            if wanted_folded is not None and name.casefold() not in wanted_folded:
                continue
            definition = feature.GetFieldAsString("Definition") or ""
            is_dataset = "DEFeatureClassInfo" in definition or "DETableInfo" in definition
            if not is_dataset:
                continue
            root = _parse_xml(definition)
            if root is None:
                continue
            documentation = feature.GetFieldAsString("Documentation") or ""
            doc_root = _parse_xml(documentation)
            item = GdbItem(
                alias=root.findtext("AliasName") or "",
                subtype_field=root.findtext("SubtypeFieldName") or "",
                shape_type=root.findtext("ShapeType") or "",
                # arcpy.metadata.Metadata.summary is the idPurp element.
                summary=(
                    (doc_root.findtext(".//idPurp") or "").strip() if doc_root is not None else ""
                ),
                # Same walk extract_column_info_from_metadata does, on the tree
                # already parsed for idPurp — a second _safe_fromstring on a
                # 1.5MB Documentation blob is how this used to pay twice.
                column_infos=(
                    prepare.column_infos_from_root(doc_root) if doc_root is not None else {}
                ),
            )
            for subtype in root.findall("./Subtypes/Subtype"):
                code = subtype.findtext("SubtypeCode")
                label = subtype.findtext("SubtypeName")
                if code is not None and label is not None:
                    # Raw, as arcpy.da.ListSubtypes reports it: the toolbox puts the
                    # name in keyval untouched and normalizes only the hint text.
                    item.subtypes[str(code)] = label
            for tag in ("ShapeFieldName", "AreaFieldName", "LengthFieldName", "GlobalIDFieldName"):
                value = root.findtext(tag)
                if value:
                    item.system_fields.add(value)
            items[name] = item
    finally:
        layer.ResetReading()
    return items


def _domain_values(ds: gdal.Dataset, name: str) -> tuple[dict[str, str], list[Any]]:
    """Return ``(keyval, minmax)`` for a GDB domain — the ListDomains equivalent."""
    domain = ds.GetFieldDomain(name)
    if domain is None:
        return {}, []
    if domain.GetDomainType() == ogr.OFDT_CODED:
        coded = domain.GetEnumeration() or {}
        return {str(k).strip(): str(v).strip() for k, v in coded.items()}, []
    if domain.GetDomainType() == ogr.OFDT_RANGE:
        return {}, [domain.GetMinAsDouble(), domain.GetMaxAsDouble()]
    return {}, []


# ---------------------------------------------------------------------------
# Describe — mirrors PrepareTool._desc_columns / _desc_layer / _desc_values
# ---------------------------------------------------------------------------
def _layer_alias(cim: CimLayer, item: GdbItem) -> str:
    return (item.alias or cim.name).lower().replace("_", " ")


def describe_columns(prepare, ds, ogr_layer, cim: CimLayer, item: GdbItem) -> list:
    """Build the Column list for one layer, in OGR (== arcpy ``desc.fields``) order."""
    columns = []
    defn = ogr_layer.GetLayerDefn()
    for i in range(defn.GetFieldCount()):
        fdefn = defn.GetFieldDefn(i)
        name = fdefn.GetName()
        dtype = field_dtype(fdefn)
        if name.casefold() in cim.hidden:
            continue
        if dtype.lower() in prepare.exclude_types:
            continue
        if name.lower() in prepare.exclude_names:
            continue
        if name in item.system_fields:
            continue

        hints: list[str] = []
        keyval: dict[str, str] = {}
        minmax: list[Any] = []

        # Alias comes from the CIM, not the GDB: HideUpdateTool rewrites aliases
        # on the layer, and long aliases outlive the 31-char field-name limit.
        raw_alias = cim.aliases.get(name) or fdefn.GetAlternativeName() or name
        alias = raw_alias.replace("_", " ")

        if item.subtype_field and name.lower() == item.subtype_field.lower():
            suffix = _layer_alias(cim, item) if prepare.subtype_alias_suffix else ""
            for code, label in item.subtypes.items():
                keyval[str(code)] = label
                hints.append(
                    catalog.coded_hint(
                        name, code, label, prepare.type_mapping.get(dtype), suffix=suffix
                    )
                )

        domain_name = fdefn.GetDomainName()
        if domain_name and not keyval:
            coded, rng = _domain_values(ds, domain_name)
            for code, value in coded.items():
                keyval[code] = value
                hints.append(catalog.coded_hint(name, code, value, prepare.type_mapping.get(dtype)))
            if rng:
                minmax = rng
                hints.append(f"Range is from {minmax[0]} to {minmax[1]}")

        columns.append(
            catalog.Column(
                name=name,
                alias=alias.lower(),
                dtype=dtype,
                minmax=minmax,
                keyval=keyval,
                hints=hints,
            )
        )
    return columns


def _value(feature: ogr.Feature, index: int, dtype: str):
    """Read one field the way ``arcpy.da.SearchCursor`` would have yielded it."""
    if not feature.IsFieldSetAndNotNull(index):
        return None
    if dtype in ("Date", "DateOnly", "TimeOnly"):
        parts = feature.GetFieldAsDateTime(index)
        if not parts:
            return None
        year, month, day, hour, minute, second, _tz = parts
        whole = int(second)
        micro = int(round((second - whole) * 1_000_000))
        if dtype == "TimeOnly":
            try:
                return dt.time(hour, minute, whole, micro)
            except ValueError:
                return None
        try:
            # str(datetime) matches what arcpy handed str(); GetFieldAsString
            # would yield "2011/06/16 00:00:00" and diverge on every date.
            stamp = dt.datetime(year, month, day, hour, minute, whole, micro)
        except ValueError:
            return None
        return stamp.date() if dtype == "DateOnly" else stamp
    if dtype in ("Double", "Single"):
        return feature.GetFieldAsDouble(index)
    if dtype in ("Integer", "SmallInteger", "BigInteger"):
        return feature.GetFieldAsInteger64(index)
    return feature.GetFieldAsString(index)


def sample_values(prepare, ogr_layer, columns, definition_query: str = "") -> list[Counter]:
    """Count the most common value per column, in feature order."""
    defn = ogr_layer.GetLayerDefn()
    counters = [Counter() for _ in columns]
    # describe_columns built `columns` from this same defn, so every name resolves.
    plan = [(c, defn.GetFieldIndex(col.name), col.dtype) for c, col in zip(counters, columns)]
    selected = {index for _, index, _ in plan}
    # Fields the definition query names must stay readable. Ignoring them after
    # SetAttributeFilter (or before, depending on the driver) can yield every
    # row or none — OBJECTID is the usual case, and it is not a catalog column.
    required = set(selected)
    if definition_query:
        query = definition_query.casefold()
        for i in range(defn.GetFieldCount()):
            name = defn.GetFieldDefn(i).GetName()
            if name and re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(name.casefold())}(?![A-Za-z0-9_])", query
            ):
                required.add(i)
    ignored = ["OGR_GEOMETRY"] + [
        defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount()) if i not in required
    ]
    try:
        # Geometry can dominate read time for complex polygons, but sampling only
        # needs attributes. OpenFileGDB also skips decoding ignored fields.
        ogr_layer.SetIgnoredFields(ignored)
        if definition_query:
            try:
                err = ogr_layer.SetAttributeFilter(definition_query)
            except RuntimeError as exc:
                print(f"warning: definition query not applied ({exc})", file=sys.stderr)
                definition_query = ""
            else:
                if err not in (None, 0):
                    print(
                        f"warning: definition query not applied (OGR error {err})",
                        file=sys.stderr,
                    )
                    definition_query = ""
                    ogr_layer.SetAttributeFilter(None)
        ogr_layer.ResetReading()
        for feature in islice(ogr_layer, prepare.max_records):
            for counter, index, dtype in plan:
                value = _value(feature, index, dtype)
                if value is None:
                    continue
                key = str(prepare._round(value)).strip()
                if key:
                    counter[key] += 1
    finally:
        # A second map layer may reference this same dataset handle.
        ogr_layer.SetIgnoredFields([])
        if definition_query:
            ogr_layer.SetAttributeFilter(None)
    return counters


def _ogr_stype(ogr_layer) -> str:
    """The ``stype`` OGR's geometry type implies — for sources with no ``GDB_Items``.

    A PostGIS column declared as plain ``geometry`` reports ``wkbUnknown``, which is
    most of them: the type is a per-row property there, not a column one. So the
    first feature is asked instead, and only a layer that has no geometry at all —
    or none to look at — is catalogued as a table.
    """
    stypes = {
        ogr.wkbPoint: "Point",
        ogr.wkbMultiPoint: "Multipoint",
        ogr.wkbLineString: "Polyline",
        ogr.wkbMultiLineString: "Polyline",
        ogr.wkbPolygon: "Polygon",
        ogr.wkbMultiPolygon: "Polygon",
    }

    def _stype(geom_type) -> str | None:
        # GT_GetLinear first: a curved type (CurvePolygon, CompoundCurve) flattens
        # to itself and would otherwise miss the table entirely.
        return stypes.get(ogr.GT_Flatten(ogr.GT_GetLinear(geom_type)))

    stype = _stype(ogr_layer.GetGeomType())
    if stype is None:
        try:
            ogr_layer.ResetReading()
            feature = ogr_layer.GetNextFeature()
        finally:
            ogr_layer.ResetReading()
        geometry = feature.GetGeometryRef() if feature is not None else None
        if geometry is not None:
            stype = _stype(geometry.GetGeometryType())
    return stype or "Table"


def find_layer(ds, dataset: str):
    """Look a dataset up, tolerating SDE's ``<database>.<schema>.<table>`` naming.

    Inside a PostgreSQL connection the database qualifier is not part of the
    name, and a lookup that names it raises (``Schema "nsgdb" does not exist``)
    rather than returning None, because ``require_gdal`` turns errors into
    exceptions.
    """
    names = [dataset] + ([dataset.split(".", 1)[1]] if dataset.count(".") == 2 else [])
    for name in names:
        try:
            layer = ds.GetLayerByName(name)
        except RuntimeError:
            continue
        if layer is not None:
            return layer
    return None


def describe_layer(prepare, ds, cim: CimLayer, items: dict[str, GdbItem]):
    """Build one ``Layer``, following ``PrepareTool._desc_layer`` step for step."""
    ogr_layer = find_layer(ds, cim.dataset)
    if ogr_layer is None:
        raise RuntimeError(f"{cim.gdb}: no dataset named {cim.dataset!r}")
    item = items.get(cim.dataset, GdbItem())

    columns = describe_columns(prepare, ds, ogr_layer, cim, item)
    if not columns:
        return None

    hints = [item.summary] if item.summary else []

    display = cim.display if any(cim.display == c.name for c in columns) else columns[0].name

    for column in columns:
        description = item.column_infos.get(column.name, {}).get("description")
        if description:
            column.hints.append(description)

    counters = sample_values(prepare, ogr_layer, columns, cim.definition_query)
    prepare._add_values_hints(columns, counters)
    prepare._add_domains_to_values(columns)

    stype = "Table" if cim.is_table else prepare._esri_shape_type_to_stype(item.shape_type)
    if not stype:  # no GDB_Items to read a ShapeType from — ask OGR instead
        stype = _ogr_stype(ogr_layer)

    return catalog.Layer(
        name=cim.name,
        table_name=cim.name.replace(" ", "_"),
        uri=str(cim.gdb / cim.dataset),
        alias=_layer_alias(cim, item),
        stype=stype,
        display=display,
        subtype=item.subtype_field,
        columns=columns,
        hints=hints,
    )


# ---------------------------------------------------------------------------
# table_name guards (mirrors DuckDBToolbox._collect_export_layers)
# ---------------------------------------------------------------------------
def check_table_names(layers) -> None:
    """Reject names that would not round-trip between Layers.json and DuckDB.

    `DuckDBToolbox._collect_export_layers` refuses to export a layer whose
    `_sanitize_table_name` differs from the catalog's `name.replace(" ", "_")`.
    Sanitizing here would only ever *force* that inequality, so the rule is stated
    directly: the catalog name has to already be a plain ASCII identifier. A
    collision or a mangled name has no safe downstream repair — `normalize_catalog`
    cannot invent a rename — so it has to fail here, where a human can rename the
    layer in Pro.
    """
    seen: dict[str, str] = {}
    for layer in layers:
        catalog = layer.table_name
        if not catalog:  # web-service layers are deliberately table-less
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
            raise ValueError(
                f"Layer '{layer.name}' cannot map consistently to Layers.json and DuckDB. "
                "Rename it to start with an ASCII letter or underscore and use only ASCII "
                "letters, digits, underscores, and spaces."
            )
        key = catalog.casefold()
        if key == "sp_ref":
            raise ValueError(f"Layer '{layer.name}' maps to reserved DuckDB table '{catalog}'.")
        if key in seen:
            raise ValueError(
                f"Layers '{seen[key]}' and '{layer.name}' both map to DuckDB table '{catalog}'."
            )
        seen[key] = layer.name


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_layers(
    aprx: Path | None,
    *,
    pg_tables: list[str] | None = None,
    max_records: int = 20000,
    max_values: int = 20,
    use_ilike: bool = False,
    subtype_alias_suffix: bool = False,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    map_name: str | None = None,
):
    """Describe every File GDB / SDE layer in ``aprx`` and return shared ``Layer`` objects."""
    require_gdal()
    prepare = catalog.CatalogBuilder()
    prepare.max_records = max_records
    prepare.max_values = max_values
    prepare.use_ilike = use_ilike
    prepare.subtype_alias_suffix = subtype_alias_suffix

    cim_layers, skipped = read_aprx(aprx, map_name) if aprx else ([], [])
    for name in skipped:
        print(f"skip (unsupported workspace): {name}", file=sys.stderr)
    # Tables named on the command line follow the map's own layers.
    cim_layers = cim_layers + pg_layers(pg_tables or [])

    selected = [
        layer
        for layer in cim_layers
        if layer.long_name not in (exclude or ()) and (not include or layer.long_name in include)
    ]

    described = []
    # One dataset handle + one GDB_Items parse per GDB, reused across its layers.
    opened: dict[str, tuple[gdal.Dataset, dict[str, GdbItem]]] = {}
    wanted_by_source: dict[str, set[str]] = {}
    for cim in selected:
        source = cim.conn or str(cim.gdb)
        names = wanted_by_source.setdefault(source, set())
        names.add(cim.dataset)
        short = cim.dataset.rsplit("\\", 1)[-1]
        if short:
            names.add(short)
    try:
        for cim in selected:
            source = cim.conn or str(cim.gdb)
            if source not in opened:
                # Missing, locked, or not actually a GDB. UseExceptions() turns those
                # into RuntimeError; without it OpenEx returns None. Both mean "skip
                # this layer", never a traceback.
                try:
                    ds = gdal.OpenEx(source, gdal.OF_VECTOR)
                except RuntimeError as exc:
                    ds = None
                    detail = f": {exc}"
                else:
                    detail = ""
                if ds is None:
                    print(f"skip (cannot open {cim.gdb}{detail}): {cim.name}", file=sys.stderr)
                    continue
                opened[source] = (
                    ds,
                    read_gdb_items(prepare, ds, wanted_by_source.get(source)),
                )
            ds, items = opened[source]
            print(f"describing {cim.name} ({cim.dataset})", file=sys.stderr)
            layer = describe_layer(prepare, ds, cim, items)
            if layer is None:
                print(f"skip (no columns): {cim.name}", file=sys.stderr)
                continue
            described.append(layer)
    finally:
        for ds, _items in opened.values():
            close_dataset(ds)

    pruned = [layer.prune_columns() for layer in described]
    return [layer for layer in pruned if layer.has_columns]


def build_parser(
    description: str, *, out_required: bool = True, aprx_required: bool = True
) -> argparse.ArgumentParser:
    """The reading half of the CLI — shared with ``okf_from_aprx``, which differs
    only in what it writes (and cites the .aprx, so it keeps requiring one)."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "aprx",
        type=Path,
        nargs=None if aprx_required else "?",
        help="ArcGIS Pro project file",
    )
    out_help = (
        "output folder"
        if out_required
        else "output folder (default: the APRX parent, or the current directory without one)"
    )
    parser.add_argument("-o", "--out", type=Path, required=out_required, help=out_help)
    parser.add_argument("--max-records", type=int, default=20000)
    parser.add_argument("--max-values", type=int, default=20)
    parser.add_argument("--use-ilike", action="store_true")
    parser.add_argument(
        "--subtype-alias-suffix",
        action="store_true",
        help="subtype hints name the feature with the layer alias: 'oil discoveries', 'dry wells'",
    )
    parser.add_argument("--include", nargs="*", default=None, metavar="NAME")
    parser.add_argument("--exclude", nargs="*", default=None, metavar="NAME")
    parser.add_argument(
        "--pg-table",
        action="append",
        default=None,
        metavar="SCHEMA.TABLE[=NAME[:DISPLAY]]",
        help="catalog a PostGIS table too; repeatable. Connection from PGDATABASE / "
        "PGHOST / PGPORT / PGUSER, password from PGPASSWORD or ~/.pgpass",
    )
    parser.add_argument(
        "--map",
        dest="map_name",
        default=None,
        help="map name (required for multi-map projects)",
    )
    return parser


def check_limits(parser: argparse.ArgumentParser, args) -> None:
    """Same bounds PrepareTool.updateMessages enforces in the Pro dialog; without
    them a negative --max-records reaches islice() as a raw ValueError."""
    if args.max_records < 1:
        parser.error("Max Records Read must be at least 1.")
    if args.max_values < 0:
        parser.error("Max Values Per Field cannot be negative.")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(
        "Author Layers.json from an .aprx + File GDB, without arcpy.",
        out_required=False,
        aprx_required=False,
    )
    args = parser.parse_args(argv)
    check_limits(parser, args)
    if args.aprx is None and not args.pg_table:
        parser.error("give a project .aprx, --pg-table, or both")

    try:
        layers = build_layers(
            args.aprx,
            pg_tables=args.pg_table,
            max_records=args.max_records,
            max_values=args.max_values,
            use_ilike=args.use_ilike,
            subtype_alias_suffix=args.subtype_alias_suffix,
            include=args.include,
            exclude=args.exclude,
            map_name=args.map_name,
        )
        if not layers:
            print("Did not find any feature layers to process :-(", file=sys.stderr)
            return 1

        check_table_names(layers)

        out = args.out or (args.aprx.parent if args.aprx else Path.cwd())
        out.mkdir(parents=True, exist_ok=True)
        written = catalog.Layers(layers=layers).dump(str(out))
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Saved Layers.json to {written}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
