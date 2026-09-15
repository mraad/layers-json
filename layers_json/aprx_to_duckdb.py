"""Export an ArcGIS Pro project to a sibling DuckDB with an interactive terminal UI."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from layers_json.duckdb_export import (
    arrow_batch,
    arrow_types,
    create_indices,
    quote,
    write_batch,
    write_sp_ref,
)


def make_console():
    from rich.console import Console

    # Rich uses Unicode spinners and borders; Windows redirected stdout may be cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return Console()


def collect_items(project, map_name=None):
    items, skipped, used = [], [], {"sp_ref", "_export_layers"}
    maps = [m for m in project.listMaps() if map_name is None or m.name == map_name]
    if not maps:
        raise ValueError(f"No maps matched {map_name!r}")
    for map_obj in maps:
        entries = [(x, False) for x in map_obj.listLayers()]
        entries += [(x, True) for x in map_obj.listTables()]
        for layer, is_table in entries:
            info = {"map": map_obj.name, "layer": getattr(layer, "longName", layer.name)}
            if not is_table and not getattr(layer, "isFeatureLayer", False):
                skipped.append(dict(info, status="skipped", reason="Not a feature layer or table"))
                continue
            base = re.sub(r"[^a-zA-Z0-9_]", "_", layer.name) or "layer"
            if base[0].isdigit():
                base = "_" + base
            name, suffix = base, 2
            while name.casefold() in used:
                name = f"{base}_{suffix}"
                suffix += 1
            used.add(name.casefold())
            items.append((layer, dict(info, table=name)))
    if not items:
        raise ValueError("No feature layers or standalone tables found")
    return items, skipped


def describe_fields(desc, pa):
    types = arrow_types(pa)
    types = {key.casefold(): value for key, value in types.items()}
    oid = getattr(desc, "OIDFieldName", "") or ""
    shape = getattr(desc, "shapeFieldName", "") or ""
    attrs = [f for f in desc.fields if f.type.casefold() != "geometry"]
    for field in attrs:
        if field.type.casefold() not in types:
            raise ValueError(f"Unsupported field {field.name!r} ({field.type})")
        if shape and field.name.casefold() == "geometry":
            raise ValueError("Attribute named geometry conflicts with the geometry column")
    fields = [f.name for f in attrs]
    schema = [pa.field(f.name, types[f.type.casefold()]) for f in attrs]
    if shape:
        fields.append("SHAPE@WKB")
        schema.append(pa.field(shape, pa.binary()))
    return oid, shape, fields, pa.schema(schema)


def export_layer(conn, layer, info, arcpy, pa, sr, batch_size, update):
    if getattr(layer, "isBroken", False):
        raise ValueError("Broken data source")
    if hasattr(layer, "setSelectionSet"):
        layer.setSelectionSet([], "NEW")
    desc = arcpy.Describe(layer)
    oid, shape, fields, schema = describe_fields(desc, pa)
    info = dict(
        info,
        definition_query=getattr(layer, "definitionQuery", ""),
        fields=[
            {"name": f.name, "alias": f.aliasName, "type": f.type, "domain": f.domain}
            for f in desc.fields
        ],
    )
    kwargs = {}
    if shape:
        if desc.spatialReference.name == "Unknown":
            raise ValueError("Unknown source coordinate system")
        info["source_wkt"] = desc.spatialReference.exportToString()
        kwargs["spatial_reference"] = sr
        transforms = arcpy.ListTransformations(desc.spatialReference, sr, desc.extent)
        if transforms:
            kwargs["datum_transformation"] = transforms[0]
            info["datum_transformation"] = transforms[0]
    expected = int(arcpy.management.GetCount(layer)[0])
    rows, count, created = [], 0, False
    update(0, expected)
    with arcpy.da.SearchCursor(layer, fields, **kwargs) as cursor:
        for row in cursor:
            rows.append(row)
            if len(rows) >= batch_size:
                write_batch(conn, info["table"], shape, arrow_batch(rows, schema, pa), not created)
                count += len(rows)
                rows, created = [], True
                update(count, expected)
        if rows or not created:
            write_batch(conn, info["table"], shape, arrow_batch(rows, schema, pa), not created)
            count += len(rows)
    qt = quote(info["table"])
    actual = conn.execute(f"SELECT count(*) FROM {qt}").fetchone()[0]
    if actual != expected or count != expected:
        raise RuntimeError(f"Source count {expected}, exported {actual}")
    create_indices(conn, info["table"], oid, bool(shape))
    info.update(status="exported", rows=actual)
    if shape:
        info["null_geometries"] = conn.execute(
            f"SELECT count(*) FROM {qt} WHERE geometry IS NULL"
        ).fetchone()[0]
    update(actual, expected)
    return info


def publish(staged, output, overwrite):
    if overwrite:
        os.replace(staged, output)
    elif os.name == "nt":
        os.rename(staged, output)
    else:
        os.link(staged, output)
        staged.unlink()


def export_project(aprx, *, overwrite=False, map_name=None, batch_size=10000, console=None):
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
    from rich.table import Table

    console = console or make_console()
    aprx = Path(aprx).expanduser().resolve()
    if aprx.suffix.lower() != ".aprx" or not aprx.is_file():
        raise ValueError(f"Expected an existing .aprx file: {aprx}")
    if batch_size < 1:
        raise ValueError("Batch size must be positive")
    output = aprx.with_suffix(".duckdb")
    if output.exists() and not overwrite:
        raise FileExistsError(f"{output} exists; use --overwrite to replace it")
    # Load Arrow before ArcPy can load its bundled Arrow DLLs on Windows.
    # isort: off
    import pyarrow as pa
    import arcpy
    import duckdb
    # isort: on

    with console.status("Reading ArcGIS project..."):
        project = arcpy.mp.ArcGISProject(str(aprx))
        items, skipped = collect_items(project, map_name)
    console.print(f"Destination: {output}", markup=False)
    console.print(f"{len(items)} tables to export; {len(skipped)} non-tabular layers skipped.")
    summary = []
    # Stage beside the destination; publish only after every layer and index succeeds.
    with TemporaryDirectory(prefix=".aprx-duckdb-", dir=aprx.parent) as temp:
        staged = Path(temp) / output.name
        with duckdb.connect(str(staged)) as conn:
            try:
                conn.execute("LOAD spatial")
            except duckdb.Error:
                console.print("Installing DuckDB spatial extension...")
                conn.execute("INSTALL spatial")
                conn.execute("LOAD spatial")
            sr = arcpy.SpatialReference(4326)
            write_sp_ref(conn, 4326, sr.exportToString())
            conn.execute("CREATE TABLE _export_layers (metadata JSON)")
            with (
                arcpy.EnvManager(extent=None),
                Progress(
                    SpinnerColumn(),
                    TextColumn("{task.description}"),
                    BarColumn(),
                    TextColumn("{task.completed:,.0f}/{task.total:,.0f}"),
                    console=console,
                ) as progress,
            ):
                overall = progress.add_task("Tables", total=len(items))
                current = progress.add_task("Reading...", total=0)
                for layer, info in items:
                    progress.update(
                        current, description=info["layer"].replace("[", r"\["), completed=0, total=0
                    )

                    def update(count, total):
                        progress.update(current, completed=count, total=total)

                    try:
                        result = export_layer(conn, layer, info, arcpy, pa, sr, batch_size, update)
                    except Exception as exc:
                        raise RuntimeError(f"{info['map']}/{info['layer']}: {exc}") from exc
                    summary.append(result)
                    progress.advance(overall)
            for item in [*summary, *skipped]:
                conn.execute("INSERT INTO _export_layers VALUES (?)", [json.dumps(item)])
            conn.execute("CHECKPOINT")
        publish(staged, output, overwrite)
    table = Table("Table", "Rows", title="Export complete")
    for item in summary:
        table.add_row(item["table"], f"{item['rows']:,}")
    console.print(table)
    for item in skipped:
        console.print(f"Skipped: {item['map']}/{item['layer']}", markup=False)
    console.print(f"Created {output} ({sum(x['rows'] for x in summary):,} rows)", markup=False)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export APRX layers and tables to a sibling .duckdb with terminal progress (requires ArcGIS Pro Python)."
    )
    parser.add_argument("aprx", nargs="?", help="ArcGIS Pro project; prompts when omitted")
    parser.add_argument("--map", dest="map_name", help="Export only this map (default: every map)")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing database only after a successful export",
    )
    parser.add_argument("--batch-size", type=int, default=10000)
    args = parser.parse_args(argv)
    try:
        if importlib.util.find_spec("arcpy") is None:
            print(
                "ArcPy is not available. Run this script with the Python environment "
                "provided by ArcGIS Pro (or a Pro conda clone). "
                "ArcPy cannot be installed from PyPI.",
                file=sys.stderr,
            )
            return 1
        from rich.prompt import Prompt

        console = make_console()
        if not args.aprx:
            if not sys.stdin.isatty():
                parser.error("aprx is required when input is not interactive")
            args.aprx = Prompt.ask("ArcGIS Pro project (.aprx)").strip().strip('"')
        export_project(
            args.aprx,
            overwrite=args.overwrite,
            map_name=args.map_name,
            batch_size=args.batch_size,
            console=console,
        )
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130
    except ImportError as exc:
        print(
            f'Missing dependency: {exc}. Run in an ArcGIS Pro Python environment with pip install -e ".[duckdb]".',
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
