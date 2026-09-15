"""Unit coverage for the arcpy-free Layers.json author.

The GDB-backed end-to-end check is a manual diff against a Pro-authored catalog
(see the module docstring of ``layers_json/layers_from_aprx.py``); these are the
pure cases that need no data.

Only the tests that actually touch OGR ask for the ``ogr`` fixture and skip
without the ``fgdb`` extra. The module imports GDAL defensively, so toolbox
resolution, CLI validation, table-name guards and .aprx parsing all run on a
plain install — which is most of this file, and most of what CI would otherwise
have covered not at all.
"""

from __future__ import annotations

import json
import zipfile
from datetime import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from layers_json import catalog
from layers_json import layers_from_aprx as script


@pytest.fixture
def ogr():
    """The OGR binding; skips the test when the fgdb extra is not installed."""
    return pytest.importorskip("osgeo.ogr", reason="needs the fgdb extra")


@pytest.fixture(scope="module")
def toolbox():
    return catalog


# ---------------------------------------------------------------------------
# optional-dependency and toolbox resolution
# ---------------------------------------------------------------------------
def test_require_gdal_names_the_install_command(monkeypatch) -> None:
    monkeypatch.setattr(script, "ogr", None)

    with pytest.raises(SystemExit) as excinfo:
        script.require_gdal()

    message = str(excinfo.value)
    assert "--extra fgdb" in message
    # A uvx user cannot run `uv sync`, so the installed-tool form has to be there too.
    assert "uv tool install" in message
    # The binding must match the system GDAL, so the hint has to name the pin.
    assert "gdal-config --version" in message


# ---------------------------------------------------------------------------
# CLI bounds
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("flag", "value"), [("--max-records", "-1"), ("--max-values", "-1")])
def test_main_rejects_out_of_range_limits(tmp_path: Path, flag, value) -> None:
    # A negative --max-records would otherwise surface as a raw ValueError from islice().
    with pytest.raises(SystemExit) as excinfo:
        script.main(["x.aprx", "-o", str(tmp_path), flag, value])

    assert excinfo.value.code == 2  # argparse usage error, not a traceback


def test_main_defaults_output_to_aprx_parent(monkeypatch, tmp_path: Path, toolbox) -> None:
    aprx = tmp_path / "project" / "NorthSea.aprx"
    monkeypatch.setattr(
        script,
        "build_layers",
        lambda *_args, **_kwargs: [_layer(toolbox, "Wells")],
    )

    assert script.main([str(aprx)]) == 0
    assert (aprx.parent / "Layers.json").is_file()


# ---------------------------------------------------------------------------
# dtype mapping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("ogr_type", "subtype", "expected"),
    [
        ("OFTString", "OFSTNone", "String"),
        ("OFTInteger", "OFSTNone", "Integer"),
        ("OFTInteger", "OFSTInt16", "SmallInteger"),
        ("OFTInteger64", "OFSTNone", "BigInteger"),
        ("OFTReal", "OFSTNone", "Double"),
        ("OFTReal", "OFSTFloat32", "Single"),
        ("OFTDateTime", "OFSTNone", "Date"),
        ("OFTDate", "OFSTNone", "DateOnly"),
    ],
)
def test_field_dtype_matches_arcpy_type_names(ogr, ogr_type, subtype, expected) -> None:
    defn = ogr.FieldDefn("f", getattr(ogr, ogr_type))
    defn.SetSubType(getattr(ogr, subtype))

    assert script.field_dtype(defn) == expected


def test_field_dtype_falls_back_to_the_ogr_name(ogr) -> None:
    defn = ogr.FieldDefn("f", ogr.OFTStringList)

    assert script.field_dtype(defn) == "StringList"


def test_time_only_values_do_not_require_a_fake_date() -> None:
    feature = SimpleNamespace(
        IsFieldSetAndNotNull=lambda _index: True,
        GetFieldAsDateTime=lambda _index: (0, 0, 0, 12, 30, 15.5, 0),
    )

    assert script._value(feature, 0, "TimeOnly") == time(12, 30, 15, 500000)


def test_sampling_ignores_geometry_and_restores_the_layer(toolbox) -> None:
    names = ["keep", "drop"]

    def get_field_count():
        return len(names)

    def get_field_defn(index):
        return SimpleNamespace(GetName=lambda: names[index])

    def get_field_index(name):
        return names.index(name)

    def field_is_set(_index):
        return True

    def get_field_as_string(_index):
        return "value"

    defn = SimpleNamespace(
        GetFieldCount=get_field_count,
        GetFieldDefn=get_field_defn,
        GetFieldIndex=get_field_index,
    )
    feature = SimpleNamespace(
        IsFieldSetAndNotNull=field_is_set,
        GetFieldAsString=get_field_as_string,
    )

    class Layer:
        def __init__(self):
            self.ignored = []
            self.GetLayerDefn = lambda: defn
            self.SetIgnoredFields = self.ignored.append
            self.ResetReading = lambda: None

        def __iter__(self):
            assert self.ignored[-1] == ["OGR_GEOMETRY", "drop"]
            return iter([feature])

    layer = Layer()
    prepare = toolbox.CatalogBuilder()
    prepare.max_records = 1
    column = toolbox.Column("keep", "keep", "String")

    assert script.sample_values(prepare, layer, [column]) == [{"value": 1}]
    assert layer.ignored[-1] == []


# ---------------------------------------------------------------------------
# table_name guards (mirrors DuckDBToolbox._collect_export_layers)
# ---------------------------------------------------------------------------
def _layer(toolbox, name: str):
    # table_name is the field check_table_names actually guards; describe_layer
    # always sets it from the layer name.
    return toolbox.Layer(
        name=name,
        table_name=name.replace(" ", "_"),
        alias=name,
        stype="Point",
        uri=f"/gdb/{name}",
    )


def test_check_table_names_accepts_spaces(toolbox) -> None:
    script.check_table_names([_layer(toolbox, "Company Leases")])


def test_check_table_names_rejects_names_that_diverge_from_the_catalog(toolbox) -> None:
    # "Wells-A" sanitizes to Wells_A but the catalog maps it to Wells-A.
    with pytest.raises(ValueError, match="cannot map consistently"):
        script.check_table_names([_layer(toolbox, "Wells-A")])


def test_check_table_names_rejects_the_reserved_sidecar(toolbox) -> None:
    with pytest.raises(ValueError, match="reserved DuckDB table"):
        script.check_table_names([_layer(toolbox, "sp_ref")])


def test_check_table_names_rejects_casefold_collisions(toolbox) -> None:
    with pytest.raises(ValueError, match="both map to DuckDB table"):
        script.check_table_names([_layer(toolbox, "Wells"), _layer(toolbox, "wells")])


def test_check_table_names_skips_table_less_service_layers(toolbox) -> None:
    service = toolbox.Layer(name="Hosted 1", alias="hosted", stype="Point", uri="https://x/0")

    script.check_table_names([service])  # no table_name to collide or mangle


# ---------------------------------------------------------------------------
# .aprx (CIM) parsing
# ---------------------------------------------------------------------------
def _feature_layer(name: str, dataset: str, *, layer_type: str = "Operational") -> dict:
    return {
        "type": "CIMFeatureLayer",
        "layerType": layer_type,
        "name": name,
        "featureTable": {
            "displayField": "well_name",
            "dataConnection": {
                "type": "CIMStandardDataConnection",
                "workspaceConnectionString": "DATABASE=.\\NorthSea.gdb",
                "workspaceFactory": "FileGDB",
                "dataset": dataset,
            },
            "fieldDescriptions": [
                {"fieldName": "well_name", "alias": "Well Name", "visible": True},
                {"fieldName": "well_id", "alias": "Well Id", "visible": False},
            ],
        },
    }


def _write_aprx(
    tmp_path: Path,
    docs: dict[str, dict],
    layer_refs: list[str],
    *,
    map_entry: str = "Map/Map.json",
    map_name: str = "Map",
) -> Path:
    aprx = tmp_path / "NorthSea.aprx"
    with zipfile.ZipFile(aprx, "w") as archive:
        archive.writestr(
            map_entry, json.dumps({"type": "CIMMap", "name": map_name, "layers": layer_refs})
        )
        for entry, doc in docs.items():
            archive.writestr(entry, json.dumps(doc))
    return aprx


def test_read_aprx_finds_a_map_not_named_map(tmp_path: Path) -> None:
    """Pro names the map's folder after the map, so ``Map/Map.json`` is a
    coincidence of the default name, not the format."""
    docs = {"Pipeline Insights/Wells.json": _feature_layer("Wells", "Wellbores")}
    aprx = _write_aprx(
        tmp_path,
        docs,
        ["CIMPATH=Pipeline Insights/Wells.json"],
        map_entry="Pipeline Insights/Pipeline Insights.json",
        map_name="Pipeline Insights",
    )

    (layer,), _ = script.read_aprx(aprx)

    assert layer.name == "Wells"


def test_read_aprx_uses_index_json_when_several_maps_exist(tmp_path: Path) -> None:
    """Index.json is the project's own map list; scanning every JSON is the fallback."""
    aprx = tmp_path / "NorthSea.aprx"
    with zipfile.ZipFile(aprx, "w") as archive:
        archive.writestr(
            "Index.json",
            json.dumps({"Nodes": [{"NodeType": "Map", "FileName": "South/South.json"}]}),
        )
        for name, dataset in (("North", "NorthWells"), ("South", "SouthWells")):
            archive.writestr(
                f"{name}/{name}.json",
                json.dumps(
                    {"type": "CIMMap", "name": name, "layers": [f"CIMPATH={name}/Wells.json"]}
                ),
            )
            archive.writestr(f"{name}/Wells.json", json.dumps(_feature_layer("Wells", dataset)))

    (layer,), _ = script.read_aprx(aprx)

    assert layer.dataset == "SouthWells"


def test_read_aprx_does_not_treat_layer_json_as_a_map(tmp_path: Path) -> None:
    """A layer document that happens to say ``type: CIMMap`` is not a second map."""
    docs = {"Map/Wells.json": _feature_layer("Wells", "Wellbores")}
    aprx = _write_aprx(tmp_path, docs, ["CIMPATH=Map/Wells.json"])
    with zipfile.ZipFile(aprx, "a") as archive:
        archive.writestr(
            "Map/Decoy.json",
            json.dumps({"type": "CIMMap", "name": "Decoy", "layers": []}),
        )

    (layer,), _ = script.read_aprx(aprx)

    assert layer.name == "Wells"


def test_read_aprx_needs_a_map_name_when_the_project_has_several(tmp_path: Path) -> None:
    aprx = tmp_path / "NorthSea.aprx"
    with zipfile.ZipFile(aprx, "w") as archive:
        for name, dataset in (("North", "NorthWells"), ("South", "SouthWells")):
            archive.writestr(
                f"{name}/{name}.json",
                json.dumps(
                    {"type": "CIMMap", "name": name, "layers": [f"CIMPATH={name}/Wells.json"]}
                ),
            )
            archive.writestr(f"{name}/Wells.json", json.dumps(_feature_layer("Wells", dataset)))

    with pytest.raises(RuntimeError, match="choose one with --map"):
        script.read_aprx(aprx)

    (layer,), _ = script.read_aprx(aprx, "South")
    assert layer.dataset == "SouthWells"
    assert layer.gdb == (tmp_path / "NorthSea.gdb").resolve()


def test_read_aprx_keeps_map_order_and_resolves_the_workspace(tmp_path: Path) -> None:
    docs = {
        "Map/Wells.json": _feature_layer("Wells", "Wellbores"),
        "Map/Pipelines.json": _feature_layer("Pipelines", "Pipelines"),
    }
    aprx = _write_aprx(tmp_path, docs, ["CIMPATH=Map/Wells.json", "CIMPATH=Map/Pipelines.json"])

    layers, skipped = script.read_aprx(aprx)

    assert [layer.name for layer in layers] == ["Wells", "Pipelines"]
    # Layer identity is the map's, not the feature class's.
    assert layers[0].dataset == "Wellbores"
    assert layers[0].gdb == (tmp_path / "NorthSea.gdb").resolve()
    assert skipped == []


def test_read_aprx_carries_cim_aliases_and_hidden_fields(tmp_path: Path) -> None:
    docs = {"Map/Wells.json": _feature_layer("Wells", "Wellbores")}
    aprx = _write_aprx(tmp_path, docs, ["CIMPATH=Map/Wells.json"])

    (layer,), _ = script.read_aprx(aprx)

    # Aliases live only in the CIM — HideUpdateTool rewrites them on the layer.
    assert layer.aliases == {"well_name": "Well Name", "well_id": "Well Id"}
    assert layer.hidden == {"well_id"}
    assert layer.display == "well_name"


def test_read_aprx_flattens_grouped_feature_layers(tmp_path: Path) -> None:
    # listLayers() yields the children, not the group; PrepareTool catalogs
    # layer.name ("Wells") and filters on layer.longName ("Group\\Wells").
    docs = {
        "Map/Wells.json": _feature_layer("Wells", "Wellbores"),
        "Map/Group.json": {
            "type": "CIMGroupLayer",
            "layerType": "Operational",
            "name": "Group",
            "layers": ["CIMPATH=Map/Wells.json"],
        },
    }
    aprx = _write_aprx(tmp_path, docs, ["CIMPATH=Map/Group.json"])

    layers, skipped = script.read_aprx(aprx)

    assert [layer.name for layer in layers] == ["Wells"]
    assert layers[0].long_name == r"Group\Wells"
    assert skipped == []


def test_read_aprx_carries_the_definition_query(tmp_path: Path) -> None:
    doc = _feature_layer("Wells", "Wellbores")
    doc["featureTable"]["definitionExpression"] = "OBJECTID < 10"
    aprx = _write_aprx(tmp_path, {"Map/Wells.json": doc}, ["CIMPATH=Map/Wells.json"])

    (layer,), _ = script.read_aprx(aprx)

    assert layer.definition_query == "OBJECTID < 10"


@pytest.mark.usefixtures("ogr")
def test_include_matches_grouped_long_name_not_short_name(tmp_path: Path) -> None:
    # Matching Pro: include/exclude compare against longName. An ungrouped
    # --include Wells must not silently select Group\\Wells.
    docs = {
        "Map/Wells.json": _feature_layer("Wells", "Wellbores"),
        "Map/Group.json": {
            "type": "CIMGroupLayer",
            "name": "Group",
            "layers": ["CIMPATH=Map/Wells.json"],
        },
    }
    aprx = _write_aprx(tmp_path, docs, ["CIMPATH=Map/Group.json"])

    layers = script.build_layers(aprx, include=["Wells"])

    assert layers == []


def test_sampling_applies_the_definition_query_and_clears_it(toolbox) -> None:
    names = ["keep"]
    defn = SimpleNamespace(
        GetFieldCount=lambda: 1,
        GetFieldDefn=lambda _i: SimpleNamespace(GetName=lambda: "keep"),
        GetFieldIndex=lambda name: names.index(name),
    )
    feature = SimpleNamespace(
        IsFieldSetAndNotNull=lambda _index: True,
        GetFieldAsString=lambda _index: "value",
    )

    class Layer:
        def __init__(self):
            self.ignored = []
            self.filters = []
            self.GetLayerDefn = lambda: defn
            self.SetIgnoredFields = self.ignored.append
            self.SetAttributeFilter = self.filters.append
            self.ResetReading = lambda: None

        def __iter__(self):
            assert self.filters[-1] == "OBJECTID < 10"
            return iter([feature])

    layer = Layer()
    prepare = toolbox.CatalogBuilder()
    prepare.max_records = 1
    column = toolbox.Column("keep", "keep", "String")

    assert script.sample_values(prepare, layer, [column], "OBJECTID < 10") == [{"value": 1}]
    assert layer.filters[-1] is None


def test_sampling_keeps_definition_query_fields_readable(toolbox) -> None:
    names = ["keep", "OBJECTID", "drop"]

    defn = SimpleNamespace(
        GetFieldCount=lambda: 3,
        GetFieldDefn=lambda index: SimpleNamespace(GetName=lambda: names[index]),
        GetFieldIndex=lambda name: names.index(name),
    )
    feature = SimpleNamespace(
        IsFieldSetAndNotNull=lambda _index: True,
        GetFieldAsString=lambda _index: "value",
    )

    class Layer:
        def __init__(self):
            self.ignored = []
            self.filters = []
            self.GetLayerDefn = lambda: defn
            self.SetIgnoredFields = self.ignored.append
            self.SetAttributeFilter = self.filters.append
            self.ResetReading = lambda: None

        def __iter__(self):
            ignored = self.ignored[-1]
            assert "OBJECTID" not in ignored
            assert "keep" not in ignored
            assert "drop" in ignored
            return iter([feature])

    layer = Layer()
    prepare = toolbox.CatalogBuilder()
    prepare.max_records = 1
    column = toolbox.Column("keep", "keep", "String")

    script.sample_values(prepare, layer, [column], "OBJECTID < 10")


def test_sampling_warns_when_attribute_filter_returns_an_error(toolbox, capsys) -> None:
    names = ["keep"]
    defn = SimpleNamespace(
        GetFieldCount=lambda: 1,
        GetFieldDefn=lambda _i: SimpleNamespace(GetName=lambda: "keep"),
        GetFieldIndex=lambda name: names.index(name),
    )
    feature = SimpleNamespace(
        IsFieldSetAndNotNull=lambda _index: True,
        GetFieldAsString=lambda _index: "value",
    )

    class Layer:
        def __init__(self):
            self.filters = []
            self.GetLayerDefn = lambda: defn
            self.SetIgnoredFields = lambda _fields: None
            self.ResetReading = lambda: None

            def set_filter(query):
                self.filters.append(query)
                return 1

            self.SetAttributeFilter = set_filter

        def __iter__(self):
            return iter([feature])

    layer = Layer()
    prepare = toolbox.CatalogBuilder()
    prepare.max_records = 1
    column = toolbox.Column("keep", "keep", "String")

    script.sample_values(prepare, layer, [column], "OBJECTID < 10")

    assert "definition query not applied" in capsys.readouterr().err
    assert layer.filters[-1] is None


def test_read_aprx_drops_basemaps_and_group_layers(tmp_path: Path) -> None:
    docs = {
        "Map/Wells.json": _feature_layer("Wells", "Wellbores"),
        "Map/Base.json": {
            "type": "CIMVectorTileLayer",
            "layerType": "BasemapBackground",
            "name": "Dark Gray Base",
        },
        "Map/Group.json": {"type": "CIMGroupLayer", "layerType": "Operational", "name": "Group"},
    }
    aprx = _write_aprx(
        tmp_path,
        docs,
        ["CIMPATH=Map/Base.json", "CIMPATH=Map/Group.json", "CIMPATH=Map/Wells.json"],
    )

    layers, skipped = script.read_aprx(aprx)

    assert [layer.name for layer in layers] == ["Wells"]
    assert skipped == []


def test_read_aprx_reports_non_filegdb_layers_as_skipped(tmp_path: Path) -> None:
    doc = _feature_layer("Hosted", "0")
    doc["featureTable"]["dataConnection"]["workspaceFactory"] = "FeatureService"
    aprx = _write_aprx(tmp_path, {"Map/Hosted.json": doc}, ["CIMPATH=Map/Hosted.json"])

    layers, skipped = script.read_aprx(aprx)

    assert layers == []
    assert skipped == ["Hosted"]


def test_read_aprx_resolves_an_absolute_workspace(tmp_path: Path) -> None:
    doc = _feature_layer("Wells", "Wellbores")
    doc["featureTable"]["dataConnection"]["workspaceConnectionString"] = "DATABASE=/data/NS.gdb"
    aprx = _write_aprx(tmp_path, {"Map/Wells.json": doc}, ["CIMPATH=Map/Wells.json"])

    (layer,), _ = script.read_aprx(aprx)

    assert layer.gdb == Path("/data/NS.gdb")


SDE_CONNECTION = (
    "ENCRYPTED_PASSWORD=00022e68abcd*00;SERVER=dbhost;INSTANCE=sde:postgresql:dbhost;"
    "DBCLIENT=postgresql;DATABASE=nsgdb;USER=ns_reader;VERSION=sde.DEFAULT"
)


def _sde_layer(name: str, dataset: str) -> dict:
    doc = _feature_layer(name, dataset)
    doc["featureTable"]["dataConnection"] |= {
        "workspaceFactory": "SDE",
        "workspaceConnectionString": SDE_CONNECTION,
    }
    return doc


def test_read_aprx_turns_an_sde_layer_into_a_pg_connection(tmp_path: Path, monkeypatch) -> None:
    for var in ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE"):
        monkeypatch.delenv(var, raising=False)
    aprx = _write_aprx(
        tmp_path,
        {"Map/P.json": _sde_layer("Parcels", "nsgdb.ns.v_parcels")},
        ["CIMPATH=Map/P.json"],
    )

    (layer,), skipped = script.read_aprx(aprx)

    assert layer.conn == "PG:dbname='nsgdb' host='dbhost' port='5432' user='ns_reader'"
    # The .aprx password is encrypted and stays there: libpq supplies the real one,
    # and `uri` records a label a catalog can carry — no credentials either way.
    assert "PASSWORD" not in layer.conn
    assert layer.gdb == Path("dbhost:5432/nsgdb")
    assert skipped == []


def test_read_aprx_lets_libpq_env_vars_redirect_an_sde_layer(tmp_path: Path, monkeypatch) -> None:
    # How a project authored against a server reaches a local container instead.
    # PGDATABASE stays unset so the .aprx's own DATABASE is what survives — an
    # ambient one on a dev machine or CI runner would otherwise decide the assert.
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.setenv("PGHOST", "localhost")
    monkeypatch.setenv("PGPORT", "5433")
    monkeypatch.setenv("PGUSER", "ns")
    aprx = _write_aprx(
        tmp_path,
        {"Map/P.json": _sde_layer("Parcels", "nsgdb.ns.v_parcels")},
        ["CIMPATH=Map/P.json"],
    )

    (layer,), _ = script.read_aprx(aprx)

    assert layer.conn == "PG:dbname='nsgdb' host='localhost' port='5433' user='ns'"
    assert layer.gdb == Path("localhost:5433/nsgdb")


def test_read_aprx_skips_an_sde_layer_on_a_backend_ogr_cannot_reach(tmp_path: Path) -> None:
    doc = _sde_layer("Parcels", "sde.SDE.PARCELS")
    doc["featureTable"]["dataConnection"]["workspaceConnectionString"] = SDE_CONNECTION.replace(
        "DBCLIENT=postgresql", "DBCLIENT=oracle"
    )
    aprx = _write_aprx(tmp_path, {"Map/P.json": doc}, ["CIMPATH=Map/P.json"])

    layers, skipped = script.read_aprx(aprx)

    assert layers == []
    assert skipped == ["Parcels"]


def test_find_layer_drops_the_sde_database_qualifier() -> None:
    def lookup(name):
        # What the PG driver raises for `<database>.<schema>.<table>`.
        if name != "ns.v_parcels":
            raise RuntimeError('Schema "nsgdb" does not exist.')
        return "the layer"

    dataset = SimpleNamespace(GetLayerByName=lookup)

    assert script.find_layer(dataset, "nsgdb.ns.v_parcels") == "the layer"
    assert script.find_layer(dataset, "nsgdb.ns.missing") is None


def _fake_layer(geom_type, *, feature_type=None):
    """A layer stub: a declared geometry type, and optionally one feature to sniff."""
    geometry = SimpleNamespace(GetGeometryType=lambda: feature_type)
    feature = SimpleNamespace(GetGeometryRef=lambda: geometry if feature_type else None)
    return SimpleNamespace(
        GetGeomType=lambda: geom_type,
        ResetReading=lambda: None,
        GetNextFeature=lambda: feature,
    )


def test_ogr_stype_reads_the_geometry_type_when_there_is_no_gdb_items(ogr) -> None:
    # An SDE/PostgreSQL source has no GDB_Items to carry a ShapeType.
    assert script._ogr_stype(_fake_layer(ogr.wkbMultiPolygon)) == "Polygon"
    assert script._ogr_stype(_fake_layer(ogr.wkbNone)) == "Table"
    # A curved type flattens to itself, so it has to be linearized first.
    assert script._ogr_stype(_fake_layer(ogr.wkbCurvePolygon)) == "Polygon"


def test_ogr_stype_asks_a_feature_when_the_column_is_plain_geometry(ogr) -> None:
    # PostGIS `geometry` columns report wkbUnknown: there the type is a property of
    # the row, not of the column, so the declaration cannot answer.
    unknown = ogr.wkbUnknown

    assert script._ogr_stype(_fake_layer(unknown, feature_type=ogr.wkbPoint)) == "Point"
    assert (
        script._ogr_stype(_fake_layer(unknown, feature_type=ogr.wkbMultiLineString)) == "Polyline"
    )
    # Nothing to sniff (an empty table) is the one case left as a table.
    assert script._ogr_stype(_fake_layer(unknown)) == "Table"


def test_pg_quote_survives_a_space_or_a_quote_in_a_value() -> None:
    # libpq splits keyword/value strings on spaces; PostgreSQL allows them in names.
    assert script._pg_quote("my db") == "'my db'"
    assert script._pg_quote("o'brien\\") == "'o\\'brien\\\\'"


@pytest.fixture
def pg_env(monkeypatch):
    """A libpq environment — the whole of a `--pg-table` connection."""
    monkeypatch.setenv("PGDATABASE", "nsgdb")
    monkeypatch.setenv("PGHOST", "localhost")
    monkeypatch.setenv("PGPORT", "5433")
    monkeypatch.setenv("PGUSER", "ns_reader")


def test_pg_layers_reads_name_and_display_from_the_spec(pg_env) -> None:
    (parcels, sites) = script.pg_layers(["ns.parcels=Parcels:entity_name", "ns.sites=Sites"])

    assert (parcels.name, parcels.dataset, parcels.display) == (
        "Parcels",
        "ns.parcels",
        "entity_name",
    )
    assert parcels.conn == "PG:dbname='nsgdb' host='localhost' port='5433' user='ns_reader'"
    assert parcels.gdb == Path("localhost:5433/nsgdb")
    # No display field named: describe_layer falls back to the first column.
    assert (sites.name, sites.display) == ("Sites", "")


def test_pg_layers_catalogs_an_unnamed_table_under_its_own_name(pg_env) -> None:
    (layer,) = script.pg_layers(["ns.sites"])

    assert (layer.name, layer.dataset) == ("sites", "ns.sites")


def test_pg_layers_needs_no_database_when_no_table_is_named(monkeypatch) -> None:
    # build_layers calls this on every run, .aprx-only ones included.
    monkeypatch.delenv("PGDATABASE", raising=False)

    assert script.pg_layers([]) == []


def test_pg_layers_says_what_to_set_when_the_database_is_unknown(monkeypatch) -> None:
    monkeypatch.delenv("PGDATABASE", raising=False)

    with pytest.raises(SystemExit, match="PGDATABASE"):
        script.pg_layers(["ns.sites"])


def test_main_reports_a_multi_map_project_without_a_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    # The error is raised while reading the .aprx, before any GDB is opened.
    monkeypatch.setattr(script, "require_gdal", lambda: None)
    aprx = tmp_path / "NorthSea.aprx"
    with zipfile.ZipFile(aprx, "w") as archive:
        for name in ("North", "South"):
            archive.writestr(
                f"{name}/{name}.json",
                json.dumps(
                    {"type": "CIMMap", "name": name, "layers": [f"CIMPATH={name}/Wells.json"]}
                ),
            )
            archive.writestr(f"{name}/Wells.json", json.dumps(_feature_layer("Wells", "Wellbores")))

    assert script.main([str(aprx), "-o", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "choose one with --map" in err
    assert "Traceback" not in err


def test_main_requires_a_project_or_a_table(capsys) -> None:
    # Both optional, but not both absent — argparse would otherwise accept a run
    # with nothing to read and report "no feature layers".
    with pytest.raises(SystemExit):
        script.main([])

    assert "--pg-table" in capsys.readouterr().err


def test_read_gdb_items_skips_unused_datasets_and_parses_documentation_once(toolbox) -> None:
    """Unmapped feature classes must not have their Documentation XML parsed."""
    wells_def = "<DEFeatureClassInfo><AliasName>wellbores</AliasName></DEFeatureClassInfo>"
    wells_doc = (
        "<metadata><idPurp>Offshore wells.</idPurp>"
        "<attr><attrlabl>DEPTH</attrlabl><attrdef>Measured depth.</attrdef></attr>"
        "</metadata>"
    )
    other_def = "<DEFeatureClassInfo><AliasName>other</AliasName></DEFeatureClassInfo>"
    reads: list[tuple[str, str]] = []

    def feature(fields: dict[str, str]):
        return SimpleNamespace(
            GetFieldAsString=lambda field, _fields=fields: (
                reads.append((_fields["Name"], field)),
                _fields[field],
            )[1]
        )

    class Items:
        def __init__(self) -> None:
            self.ResetReading = lambda: None

        def __iter__(self):
            return iter(
                [
                    feature(
                        {
                            "Name": r"NorthSea.gdb\Other",
                            "Definition": other_def,
                            "Documentation": "<metadata>unused and huge</metadata>",
                        }
                    ),
                    feature(
                        {
                            "Name": r"NorthSea.gdb\Wellbores",
                            "Definition": wells_def,
                            "Documentation": wells_doc,
                        }
                    ),
                ]
            )

    dataset = SimpleNamespace(GetLayerByName=lambda _name: Items())
    prepare = toolbox.CatalogBuilder()

    items = script.read_gdb_items(prepare, dataset, wanted={"Wellbores"})

    assert set(items) == {"Wellbores"}
    assert items["Wellbores"].alias == "wellbores"
    assert items["Wellbores"].summary == "Offshore wells."
    assert items["Wellbores"].column_infos["DEPTH"]["description"] == "Measured depth."
    assert ("NorthSea.gdb\\Other", "Documentation") not in reads
    assert ("NorthSea.gdb\\Wellbores", "Documentation") in reads


def test_build_layers_skips_a_gdb_it_cannot_open(ogr, tmp_path: Path, capsys) -> None:
    # The .aprx points at a workspace that is not there; OpenEx returns None (or
    # raises RuntimeError under UseExceptions) and read_gdb_items would crash on it.
    aprx = _write_aprx(
        tmp_path,
        {"Map/Wells.json": _feature_layer("Wells", "Wellbores")},
        ["CIMPATH=Map/Wells.json"],
    )

    layers = script.build_layers(aprx)

    assert layers == []
    assert "cannot open" in capsys.readouterr().err
