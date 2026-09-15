#!/usr/bin/env python3
"""Hide fields and rewrite aliases in an ArcGIS Pro project without arcpy.

The command reads the existing ``HideUpdateTool.json`` format, edits one map's
CIM JSON inside the ``.aprx``, and writes a new project atomically. GDAL
supplies the field metadata that ``arcpy.Describe`` supplied in the toolbox.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterator

from . import catalog
from .hide_update import LayerChange, Rules, Schema, apply_rules, build_schema, load_rules
from .layers_from_aprx import (
    _resolve_workspace,
    close_dataset,
    find_layer,
    gdal,
    ogr,
    require_gdal,
    select_map,
)


def _definition_schema(definition: str) -> Schema:
    root = catalog._safe_fromstring(definition)

    def names(*tags: str) -> list[str]:
        return [(root.findtext(tag) or "").strip() for tag in tags]

    return build_schema(
        names("OIDFieldName", "ShapeFieldName", "SubtypeFieldName"),
        names("AreaFieldName", "LengthFieldName", "GlobalIDFieldName"),
        [
            (
                (field.findtext("Name") or "").strip(),
                field.findtext("AliasName"),
                field.findtext("FieldType"),
            )
            for field in root.findall(".//GPFieldInfoEx")
        ],
    )


def _ogr_schema(layer) -> Schema:
    definition = layer.GetLayerDefn()
    names = [layer.GetFIDColumn(), layer.GetGeometryColumn()]
    names += [
        definition.GetGeomFieldDefn(i).GetNameRef() for i in range(definition.GetGeomFieldCount())
    ]
    names = [name for name in dict.fromkeys(names) if name]
    visible = {name.casefold() for name in names}
    fields = [(name, name) for name in names]
    hidden = set()
    uuid_subtype = getattr(ogr, "OFSTUUID", None)
    for index in range(definition.GetFieldCount()):
        field = definition.GetFieldDefn(index)
        name = field.GetNameRef()
        fields.append((name, field.GetAlternativeNameRef() or name))
        default = (field.GetDefault() or "").upper()
        if field.GetType() == ogr.OFTBinary or (
            uuid_subtype is not None and field.GetSubType() == uuid_subtype
        ):
            hidden.add(name.casefold())
        if "FILEGEODATABASE_SHAPE_AREA" in default or "FILEGEODATABASE_SHAPE_LENGTH" in default:
            hidden.add(name.casefold())
    return Schema(frozenset(visible), frozenset(hidden), tuple(fields))


def _gdb_definitions(dataset) -> dict[str, str]:
    try:
        items = dataset.GetLayerByName("GDB_Items")
    except RuntimeError:
        return {}
    if items is None:
        return {}
    definitions = {}
    try:
        items.ResetReading()
        for feature in items:
            item_name = (feature.GetFieldAsString("Name") or "").rsplit("\\", 1)[-1]
            definition = feature.GetFieldAsString("Definition") or ""
            if item_name and definition:
                definitions[item_name.casefold()] = definition
    finally:
        items.ResetReading()
    return definitions


def _merge_definition(fallback: Schema, definition: str) -> Schema:
    if not definition:
        return fallback
    try:
        exact = _definition_schema(definition)
    except ValueError:
        return fallback
    return Schema(
        fallback.visible | exact.visible,
        fallback.hidden | exact.hidden,
        exact.fields or fallback.fields,
    )


def describe_schema(
    doc: dict[str, Any],
    aprx_dir: Path,
    opened: dict[str, Any],
    definitions: dict[str, dict[str, str]],
    schemas: dict[tuple[str, str], Schema],
) -> Schema:
    """Return fields forced visible/hidden by the original toolbox."""
    connection = (doc.get("featureTable") or {}).get("dataConnection") or {}
    factory = connection.get("workspaceFactory")
    raw = connection.get("workspaceConnectionString", "")
    if factory != "FileGDB":
        raise ValueError(f"unsupported {factory or 'unknown'} workspace")
    workspace = _resolve_workspace(raw, aprx_dir)
    source = str(workspace) if workspace else ""
    if not source:
        raise ValueError("workspace connection has no usable database")

    if source not in opened:
        opened[source] = gdal.OpenEx(source, gdal.OF_VECTOR)
        if opened[source] is not None:
            definitions[source] = _gdb_definitions(opened[source])
    dataset = opened[source]
    if dataset is None:
        raise ValueError(f"cannot open {source}")

    name = connection.get("dataset") or ""
    key = (source, name.casefold())
    if key in schemas:
        return schemas[key]
    layer = find_layer(dataset, name)
    if layer is None:
        raise ValueError(f"{source} has no dataset named {name!r}")

    fallback = _ogr_schema(layer)
    definition = definitions[source].get(name.rsplit("\\", 1)[-1].casefold(), "")
    schemas[key] = _merge_definition(fallback, definition)
    return schemas[key]


def _entry(ref: str) -> str:
    return ref.removeprefix("CIMPATH=")


def iter_feature_layers(
    archive: zipfile.ZipFile, map_name: str | None = None
) -> Iterator[tuple[str, dict[str, Any], str]]:
    """Yield one map's feature layers, flattening groups like ``listLayers()``."""
    names = set(archive.namelist())
    _map_entry, map_doc = select_map(archive, names, map_name)
    seen: set[str] = set()

    def visit(refs: list[str], parents: tuple[str, ...] = ()):
        for ref in refs:
            entry = _entry(ref)
            if entry in seen or entry not in names:
                continue
            seen.add(entry)
            doc = json.loads(archive.read(entry))
            name = doc.get("name") or entry
            if doc.get("type") == "CIMGroupLayer":
                yield from visit(doc.get("layers") or [], (*parents, name))
            elif doc.get("type") == "CIMFeatureLayer" and doc.get("layerType") == "Operational":
                yield entry, doc, "\\".join((*parents, name))

    yield from visit(map_doc.get("layers") or [])


def _json_bytes(value: dict[str, Any]) -> bytes:
    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    # ArcGIS writes HTML-sensitive characters escaped in CIM JSON.
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").encode()


def _write_project(source: Path, output: Path, replacements: dict[str, bytes]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent, prefix=f".{output.stem}.", suffix=".aprx", delete=False
        ) as temp:
            temp_name = temp.name
        with zipfile.ZipFile(source) as current, zipfile.ZipFile(temp_name, "w") as updated:
            updated.comment = current.comment
            for info in current.infolist():
                updated.writestr(info, replacements.get(info.filename, current.read(info.filename)))
        with zipfile.ZipFile(temp_name) as check:
            bad_entry = check.testzip()
            if bad_entry:
                raise ValueError(f"updated project has a corrupt ZIP entry: {bad_entry}")
        os.replace(temp_name, output)
        temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def update_project(
    source: Path,
    output: Path,
    rules: Rules,
    *,
    map_name: str | None = None,
    schema_reader: Callable[[dict[str, Any], Path], Schema] | None = None,
) -> tuple[list[LayerChange], list[tuple[str, str]]]:
    """Apply rules and atomically write ``output``; ``source`` is never modified."""
    if source.resolve() == output.resolve():
        raise ValueError("output must differ from the source project")
    opened: dict[str, Any] = {}
    definitions: dict[str, dict[str, str]] = {}
    schemas: dict[tuple[str, str], Schema] = {}
    try:
        reader = schema_reader or (
            lambda doc, path: describe_schema(doc, path, opened, definitions, schemas)
        )
        replacements: dict[str, bytes] = {}
        changes: list[LayerChange] = []
        skipped: list[tuple[str, str]] = []
        processed = 0

        with zipfile.ZipFile(source) as archive:
            for entry, doc, layer_name in iter_feature_layers(archive, map_name):
                if not rules.includes_layer(layer_name):
                    continue
                try:
                    schema = reader(doc, source.parent)
                except (RuntimeError, ValueError) as exc:
                    skipped.append((layer_name, str(exc)))
                    continue
                processed += 1
                change = apply_rules(doc, layer_name, schema, rules)
                changes.append(change)
                replacements[entry] = _json_bytes(doc)

        if not processed:
            detail = f" ({len(skipped)} skipped; {skipped[0][1]})" if skipped else ""
            raise ValueError(f"no feature layers selected{detail}")
        _write_project(source, output, replacements)
        return changes, skipped
    finally:
        for ds in opened.values():
            close_dataset(ds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hide fields and rewrite aliases in an .aprx without arcpy."
    )
    parser.add_argument("aprx", type=Path, help="source ArcGIS Pro project")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        help="HideUpdateTool.json (default: beside the source project)",
    )
    parser.add_argument("--map", dest="map_name", help="map name (required for multi-map projects)")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="new sibling project (default: <project>.updated.aprx)",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing output project")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    source = args.aprx.expanduser()
    config = (args.config or source.parent / "HideUpdateTool.json").expanduser()
    output = (args.output or source.with_name(f"{source.stem}.updated.aprx")).expanduser()

    if not source.is_file():
        parser.error(f"project does not exist: {source}")
    if source.suffix.casefold() != ".aprx":
        parser.error("project must end in .aprx")
    if source.resolve() == output.resolve():
        parser.error("output must differ from the source project")
    if source.parent.resolve() != output.parent.resolve():
        parser.error("output must stay beside the source so relative data connections remain valid")
    if output.exists() and not args.force:
        parser.error(f"output already exists: {output} (use --force to replace it)")

    try:
        rules = load_rules(config)
    except ValueError as exc:
        parser.error(str(exc))
    require_gdal()
    try:
        changes, skipped = update_project(source, output, rules, map_name=args.map_name)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for change in changes:
        print(
            f"{change.name}: hid {len(change.hidden)}, updated {len(change.aliases)} aliases",
            file=sys.stderr,
        )
    for name, reason in skipped:
        print(f"skip {name}: {reason}", file=sys.stderr)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
