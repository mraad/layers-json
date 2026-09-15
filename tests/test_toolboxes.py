"""Pro adapter coverage using the test-only ArcPy loader."""

from __future__ import annotations

import json
import types
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pytest
from toolbox_support import TOOLBOXES, load_toolbox

from layers_json import duckdb_export


@pytest.fixture(scope="module")
def layers_toolbox():
    return load_toolbox(TOOLBOXES / "Layers.pyt")


@pytest.fixture(scope="module")
def duckdb_toolbox():
    return load_toolbox(TOOLBOXES / "DuckDBToolbox.pyt")


def test_multivalue_parser_preserves_embedded_apostrophes(layers_toolbox) -> None:
    parsed = layers_toolbox.parse_arcpy_multivalue("'Owner''s; Parcels'; Plain; \"Quoted Name\"")

    assert parsed == ["Owner's; Parcels", "Plain", "Quoted Name"]


def test_layers_dump_is_atomic_and_preserves_catalog_shape(layers_toolbox, tmp_path: Path) -> None:
    target = tmp_path / "Layers.json"
    target.write_text("old content", encoding="utf-8")
    column = layers_toolbox.Column("name", "name", "String")
    layer = layers_toolbox.Layer(
        name="Places",
        alias="places",
        stype="Point",
        uri="places",
        columns=[column],
    )

    output = layers_toolbox.Layers([layer]).dump(str(target))

    assert output == str(target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["layers"][0]["columns"][0] == {
        "name": "name",
        "alias": "name",
        "dtype": "String",
        "utype": None,
        "minmax": [],
        "keyval": {},
        "hints": [],
        "values": [],
    }
    assert not target.read_bytes().endswith(b"\n")
    assert list(tmp_path.glob(".Layers.*.tmp")) == []

    previous = target.read_bytes()
    invalid_layer = layers_toolbox.Layer(
        name="Invalid",
        alias=object(),
        stype="Point",
        uri="invalid",
    )
    with pytest.raises(TypeError):
        layers_toolbox.Layers([invalid_layer]).dump(str(target))
    assert target.read_bytes() == previous
    assert list(tmp_path.glob(".Layers.*.tmp")) == []


def test_column_infos_from_root_is_what_extract_returns(layers_toolbox) -> None:
    xml = (
        "<metadata><attr><attrlabl>DEPTH</attrlabl>"
        "<attrtype>Double</attrtype><attrdef>Measured depth.</attrdef></attr></metadata>"
    )
    prepare = layers_toolbox.PrepareTool()
    from_extract = prepare.extract_column_info_from_metadata(xml)
    from_root = prepare.column_infos_from_root(layers_toolbox._safe_fromstring(xml))

    assert (
        from_extract
        == from_root
        == {"DEPTH": {"name": "DEPTH", "type": "Double", "description": "Measured depth."}}
    )


def test_domain_values_are_deduplicated_in_stable_order(layers_toolbox) -> None:
    tool = layers_toolbox.PrepareTool()
    column = layers_toolbox.Column(
        "status",
        "status",
        "String",
        values=["B", "A", "B"],
        keyval={"A": "Active", "C": "Closed"},
    )

    tool._add_domains_to_values([column])

    assert column.values == ["B", "A", "C"]


def test_web_sampling_uses_every_bounded_record(layers_toolbox) -> None:
    class FeatureLayer:
        def query(self, **kwargs):
            assert kwargs["result_record_count"] == 3
            return {
                "features": [
                    {"attributes": {"status": "A"}},
                    {"attributes": {"status": "B"}},
                    {"attributes": {"status": "A"}},
                ]
            }

    tool = layers_toolbox.PrepareTool()
    tool.max_records = 3
    tool.max_values = 10
    column = layers_toolbox.Column("status", "status", "String")

    tool._desc_values_web(FeatureLayer(), "Places", [column])

    assert column.values == ["A", "B"]


def test_local_sampling_avoids_count_and_objectid_slice(layers_toolbox) -> None:
    calls = []

    class Cursor:
        def __enter__(self):
            return iter([("A",), ("B",), ("A",)])

        def __exit__(self, *_args):
            return False

    def search_cursor(*args, **kwargs):
        calls.append((args, kwargs))
        return Cursor()

    tool = layers_toolbox.PrepareTool()
    tool.max_records = 3
    tool.max_values = 10
    column = layers_toolbox.Column("status", "status", "String")
    layer = types.SimpleNamespace(definitionQuery="")
    desc = types.SimpleNamespace(OIDFieldName="OBJECTID")

    with patch.object(
        layers_toolbox.arcpy.da,
        "SearchCursor",
        side_effect=search_cursor,
        create=True,
    ):
        tool._desc_values(layer, "Places", desc, [column])

    assert calls[0][1]["where_clause"] is None
    assert column.values == ["A", "B"]


def test_date_samples_do_not_generate_like_guidance(layers_toolbox) -> None:
    tool = layers_toolbox.PrepareTool()
    column = layers_toolbox.Column("observed_at", "observed at", "Date")

    tool._add_values_hints([column], [Counter({"2026-06-27": 1})])

    assert column.values == ["2026-06-27"]
    assert column.hints == []


def test_catalog_preserves_new_arcgis_temporal_types(layers_toolbox) -> None:
    tool = layers_toolbox.PrepareTool()

    assert tool._esri_field_type_to_dtype("esriFieldTypeDateOnly") == "DateOnly"
    assert tool._esri_field_type_to_dtype("esriFieldTypeTimeOnly") == "TimeOnly"
    assert tool._esri_field_type_to_dtype("esriFieldTypeTimestampOffset") == ("TimestampOffset")


def test_local_catalog_excludes_cim_hidden_fields(layers_toolbox) -> None:
    def field(name: str):
        return types.SimpleNamespace(
            name=name,
            type="String",
            aliasName=name,
            domain="",
        )

    desc = types.SimpleNamespace(
        fields=[field("visible"), field("hidden")],
        subtypeFieldName="",
        catalogPath="places",
    )
    tool = layers_toolbox.PrepareTool()

    columns = tool._desc_columns("Places", desc, {}, hidden_fields={"hidden"})

    assert [column.name for column in columns] == ["visible"]


def test_domains_are_loaded_once_per_geodatabase(layers_toolbox) -> None:
    tool = layers_toolbox.PrepareTool()
    desc = types.SimpleNamespace(path=r"C:\Data\NorthSea.GDB\Wells")
    domain = types.SimpleNamespace(name="status")

    with patch.object(
        layers_toolbox.arcpy.da,
        "ListDomains",
        return_value=[domain],
        create=True,
    ) as list_domains:
        first = tool._get_domains(desc)
        second = tool._get_domains(desc)

    assert first == second == {"status": domain}
    list_domains.assert_called_once_with(r"C:\Data\NorthSea.GDB")


def test_duckdb_helpers_quote_and_parse_safely(duckdb_toolbox) -> None:
    base = duckdb_toolbox.DuckDBToolBase

    assert base._get_install_spatial("C:/O'Brien/spatial.duckdb_extension") == (
        "'C:/O''Brien/spatial.duckdb_extension'"
    )
    assert base._parse_layer_filters("'Owner''s; Parcels'", None) == (
        ["Owner's; Parcels"],
        [],
    )


def test_duckdb_export_rejects_sanitized_name_collisions(duckdb_toolbox) -> None:
    def layer(name: str, long_name: str):
        return types.SimpleNamespace(
            name=name,
            longName=long_name,
            isGroupLayer=False,
            isBasemapLayer=False,
            isBroken=False,
            isFeatureLayer=True,
        )

    with pytest.raises(ValueError, match="both map to DuckDB table"):
        duckdb_toolbox.DuckDBToolBase._collect_export_layers(
            [layer("Road Work", "Current/Road Work"), layer("Road_Work", "Archive/Road_Work")],
            [],
            [],
        )

    with pytest.raises(ValueError, match="reserved DuckDB table"):
        duckdb_toolbox.DuckDBToolBase._collect_export_layers(
            [layer("sp ref", "sp ref")],
            [],
            [],
        )

    with pytest.raises(ValueError, match="cannot map consistently"):
        duckdb_toolbox.DuckDBToolBase._collect_export_layers(
            [layer("Road-Work", "Road-Work")],
            [],
            [],
        )


def test_duckdb_field_schema_supports_64_bit_oids_and_skips_rasters(
    duckdb_toolbox,
) -> None:
    fields = [
        types.SimpleNamespace(name="OBJECTID", type="OID"),
        types.SimpleNamespace(name="name", type="String"),
        types.SimpleNamespace(name="photo", type="Raster"),
        types.SimpleNamespace(name="Shape", type="Geometry"),
    ]
    desc = types.SimpleNamespace(
        OIDFieldName="OBJECTID",
        shapeFieldName="Shape",
        fields=fields,
        hasOID64=True,
    )
    field_descriptions = [
        types.SimpleNamespace(fieldName=field.name, visible=True) for field in fields
    ]
    layer = types.SimpleNamespace(
        getDefinition=lambda _version: types.SimpleNamespace(
            featureTable=types.SimpleNamespace(fieldDescriptions=field_descriptions)
        )
    )

    with patch.object(duckdb_toolbox.arcpy, "Describe", return_value=desc, create=True):
        _oid, _shape, cursor_fields, columns, schema = (
            duckdb_toolbox.DuckDBToolBase._describe_fields(layer)
        )

    assert cursor_fields == ["OBJECTID", "name", "SHAPE@WKB"]
    assert columns == ["OBJECTID", "name", "Shape"]
    assert schema.field("OBJECTID").type == pa.int64()
    assert schema.field("Shape").type == pa.binary()


def test_duckdb_field_schema_supports_standalone_tables(duckdb_toolbox) -> None:
    fields = [
        types.SimpleNamespace(name="OBJECTID", type="OID"),
        types.SimpleNamespace(name="name", type="String"),
    ]
    desc = types.SimpleNamespace(
        OIDFieldName="OBJECTID",
        fields=fields,
        hasOID64=False,
    )
    table = types.SimpleNamespace(
        name="Owners",
        longName=r"Tables\Owners",
        isBroken=False,
        getDefinition=lambda _version: types.SimpleNamespace(
            fieldDescriptions=[
                types.SimpleNamespace(fieldName=field.name, visible=True) for field in fields
            ]
        ),
    )

    with patch.object(duckdb_toolbox.arcpy, "Describe", return_value=desc, create=True):
        oid, shape, cursor_fields, columns, schema = duckdb_toolbox.DuckDBToolBase._describe_fields(
            table
        )

    assert oid == "OBJECTID"
    assert shape == ""
    assert cursor_fields == columns == ["OBJECTID", "name"]
    assert schema.names == ["OBJECTID", "name"]
    assert duckdb_export.arrow_batch([], schema, pa).num_rows == 0
    assert duckdb_toolbox.DuckDBToolBase._collect_export_layers([table], ["Owners"], []) == [
        (table, "Owners")
    ]


def test_duckdb_export_skips_web_backed_layers(duckdb_toolbox) -> None:
    web_layer = types.SimpleNamespace(
        name="Hosted Places",
        longName="Hosted Places",
        isGroupLayer=False,
        isBasemapLayer=False,
        isBroken=False,
        isFeatureLayer=True,
        isWebLayer=True,
        dataSource="https://example.com/FeatureServer/0",
    )

    assert duckdb_toolbox.DuckDBToolBase._collect_export_layers([web_layer], [], []) == []


def test_duckdb_export_rejects_reserved_geometry_attribute(duckdb_toolbox) -> None:
    fields = [
        types.SimpleNamespace(name="OBJECTID", type="OID"),
        types.SimpleNamespace(name="geometry", type="String"),
        types.SimpleNamespace(name="Shape", type="Geometry"),
    ]
    desc = types.SimpleNamespace(
        OIDFieldName="OBJECTID",
        shapeFieldName="Shape",
        fields=fields,
    )
    layer = types.SimpleNamespace(
        getDefinition=lambda _version: types.SimpleNamespace(
            featureTable=types.SimpleNamespace(fieldDescriptions=[])
        )
    )

    with (
        patch.object(duckdb_toolbox.arcpy, "Describe", return_value=desc, create=True),
        pytest.raises(ValueError, match="reserved geometry column"),
    ):
        duckdb_toolbox.DuckDBToolBase._describe_fields(layer)


def test_duckdb_writes_standalone_tables_without_spatial_sql(duckdb_toolbox) -> None:
    class Connection:
        def __init__(self):
            self.statements = []

        def register(self, *_args):
            pass

        def execute(self, statement):
            self.statements.append(statement)

        def unregister(self, *_args):
            pass

    conn = Connection()
    table = pa.table({"OBJECTID": pa.array([], type=pa.int32())})

    duckdb_export.write_batch(conn, "Owners", "", table, True, replace=True)
    duckdb_export.create_indices(conn, "Owners", "OBJECTID", has_geometry=False)

    sql = " ".join(conn.statements)
    assert 'CREATE OR REPLACE TABLE "Owners" AS SELECT * FROM _aprx_batch' in sql
    assert 'PRIMARY KEY ("OBJECTID")' in sql
    assert "ST_GeomFromWKB" not in sql
    assert "RTREE" not in sql


def test_rows_to_arrow_handles_new_temporal_types(
    duckdb_toolbox,
) -> None:
    schema = pa.schema(
        [
            pa.field(
                "recorded_on",
                duckdb_toolbox.DuckDBToolBase.ARCGIS_TO_ARROW["Date"],
            ),
            pa.field(
                "captured_at",
                duckdb_toolbox.DuckDBToolBase.ARCGIS_TO_ARROW["TimestampOffset"],
            ),
            pa.field(
                "local_time",
                duckdb_toolbox.DuckDBToolBase.ARCGIS_TO_ARROW["TimeOnly"],
            ),
        ]
    )
    captured_at = datetime(2026, 6, 27, 12, 30, tzinfo=timezone(-timedelta(hours=4)))

    table = duckdb_export.arrow_batch([(date(2026, 6, 27), captured_at, time(12, 30))], schema, pa)

    assert table["recorded_on"].to_pylist() == [datetime(2026, 6, 27)]
    assert table["captured_at"].to_pylist() == [datetime(2026, 6, 27, 16, 30, tzinfo=timezone.utc)]
    assert table["local_time"].to_pylist() == [time(12, 30)]


def test_web_backed_layers_get_no_table_name(layers_toolbox) -> None:
    """A hosted feature service must never be marked database-backed.

    In _desc_layer_list the dispatch is `if isFeatureLayer ... elif isWebLayer`,
    and a hosted feature service sets BOTH — so it lands in _desc_layer(), not
    _desc_layer_web(). It has no table in the spatial DB: emitting a table_name
    would flip _is_db_layer() to True, report it to the agent as `source:
    database`, and hand over a SELECT against a table that does not exist.
    """
    is_web_backed = layers_toolbox.is_web_backed

    class Lyr:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    # Hosted feature service: both flags set, http source.
    assert is_web_backed(
        Lyr(isFeatureLayer=True, isWebLayer=True, dataSource="https://x/FeatureServer/0")
    )
    # isWebLayer alone is enough.
    assert is_web_backed(Lyr(isFeatureLayer=True, isWebLayer=True, dataSource=""))
    # So is an http source alone, if the flag is missing on this layer type.
    assert is_web_backed(Lyr(isFeatureLayer=True, dataSource="http://x/FeatureServer/0"))
    # A local geodatabase feature class is not web-backed.
    assert not is_web_backed(
        Lyr(isFeatureLayer=True, isWebLayer=False, dataSource=r"C:\P\NorthSea.gdb\Wells")
    )
    # A Windows path is not mistaken for a URL.
    assert not is_web_backed(Lyr(isFeatureLayer=True, dataSource=r"C:\httpdocs\NorthSea.gdb\Wells"))


def test_web_branch_skips_services_with_no_queryable_sublayers(
    layers_toolbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WMS layer must be skipped, not crash the whole run.

    `_desc_layer_list` routes on `elif layer.isWebLayer`, which is true for every
    service-backed layer -- WMS, WMTS, tiled, image, vector tile -- while
    `_desc_layer_web` reads only what a map/feature service exposes. A CIMWMSLayer's
    children are CIMWMSSubLayer and carry no `definitionExpression`, so reading one
    raised AttributeError out of `execute` and no Layers.json was written at all.
    """

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Lyr:
        def __init__(self, cim):
            self._cim = cim
            self.dataSource = "https://example.invalid/service"

        def getDefinition(self, _version):  # noqa: N802 - arcpy dictates the name
            return self._cim

    warnings: list[str] = []
    monkeypatch.setattr(layers_toolbox.arcpy, "AddWarning", warnings.append)

    tool = layers_toolbox.PrepareTool()
    tool.layers = []

    # WMS: sublayers exist but are a different CIM class.
    wms = Obj(subLayers=[Obj(name="LANDSAT 24 hrs fires/hotspots", visibility=True)])
    tool._desc_layer_web(Lyr(wms), "NASA FIRMS WMS Fires/Hotspots")
    # A tiled or vector-tile layer has no subLayers attribute at all.
    tool._desc_layer_web(Lyr(Obj()), "Cached Basemap")

    assert tool.layers == []
    assert len(warnings) == 2

    # A map service still gets through: its sublayers carry definitionExpression.
    seen = []

    class Flc:
        layers: list = []

        def __init__(self, url, gis):
            seen.append(url)

    monkeypatch.setattr(layers_toolbox, "FeatureLayerCollection", Flc)
    monkeypatch.setattr(tool, "_get_gis_for_data_source", lambda _source: None)
    service = Obj(subLayers=[Obj(name="Wells", visibility=True, definitionExpression="")])
    tool._desc_layer_web(Lyr(service), "Wells Map Service")

    assert seen == ["https://example.invalid/service"]
    assert len(warnings) == 2


def test_web_branch_traverses_mixed_nested_sublayers(layers_toolbox, monkeypatch):
    from types import SimpleNamespace as Obj

    class Properties(dict):
        __getattr__ = dict.__getitem__

    def leaf(name, visible=True):
        return Obj(name=name, visibility=visible, definitionExpression=f"name = '{name}'")

    cim = Obj(
        subLayers=[
            Obj(
                visibility=True,
                subLayers=[
                    Obj(subLayers=[leaf("Nested")]),
                    leaf("Hidden leaf", False),
                ],
            ),
            leaf("Sibling"),
            Obj(visibility=False, subLayers=[Obj(subLayers=[leaf("Hidden parent")])]),
            Obj(name="Unsupported", visibility=True),
        ]
    )
    layer = Obj(dataSource="https://example.invalid/service", getDefinition=lambda _: cim)
    names = ["Nested", "Sibling", "Hidden leaf", "Hidden parent"]
    services = [
        Obj(
            url=f"https://example.invalid/{i}",
            properties=Properties(
                name=name,
                description="",
                geometryType="esriGeometryPoint",
                displayField="name",
            ),
        )
        for i, name in enumerate(names)
    ]
    monkeypatch.setattr(
        layers_toolbox, "FeatureLayerCollection", lambda *args: Obj(layers=services)
    )
    tool = layers_toolbox.PrepareTool()
    tool.layers = []
    monkeypatch.setattr(tool, "_get_gis_for_data_source", lambda _: None)
    monkeypatch.setattr(
        tool, "_desc_columns_web", lambda *args: [layers_toolbox.Column("name", "name", "String")]
    )
    monkeypatch.setattr(tool, "_add_domains_to_values", lambda _: None)
    queries = []
    monkeypatch.setattr(
        tool, "_desc_values_web", lambda fl, name, cols, query: queries.append(query)
    )
    tool._desc_layer_web(layer, "Service")
    assert [item.name for item in tool.layers] == ["Service/Nested", "Service/Sibling"]
    assert queries == ["name = 'Nested'", "name = 'Sibling'"]


def test_layer_serializes_table_name_only_when_set(layers_toolbox) -> None:
    """table_name is omitted entirely (not null) when absent, because
    _is_db_layer() keys on truthiness and a null would be indistinguishable
    from a real value to a reader scanning keys."""
    layer_cls = layers_toolbox.Layer

    db = layer_cls(
        name="Wells",
        alias="wells",
        stype="Point",
        uri=r"C:\P\NorthSea.gdb\Wellbores",
        table_name="Wells",
    )
    web = layer_cls(
        name="Utilities/Mains",
        alias="mains",
        stype="Polyline",
        uri="https://services.arcgis.com/x/FeatureServer/0",
    )

    assert db.to_dict()["table_name"] == "Wells"
    assert "table_name" not in web.to_dict()
    # name stays first so the catalog diff stays readable.
    assert next(iter(db.to_dict())) == "name"
    # prune_columns must carry it through, or a pruned catalog loses enrichment.
    assert db.prune_columns().to_dict()["table_name"] == "Wells"
    assert "table_name" not in web.prune_columns().to_dict()


def test_coded_hint_is_a_pasteable_where_fragment(layers_toolbox) -> None:
    """Subtypes and coded domains share one grammar, and the label reads as prose."""
    assert (
        layers_toolbox.coded_hint("PIPETYPE", 3, "Water Main")
        == "Use 'PIPETYPE=3' for 'water main'."
    )
    # "/" is a choice, not a path -- and it must not leave a double space behind.
    assert (
        layers_toolbox.coded_hint("MATERIAL", "PVC/HDPE", "Plastic / Composite")
        == "Use 'MATERIAL=PVC/HDPE' for 'plastic or composite'."
    )
    # A padded slash expands to a three-space run; one non-overlapping pass of
    # replace("  ", " ") would leave a double space behind.
    assert (
        layers_toolbox.coded_hint("MATERIAL", "X", "Plastic /  Composite")
        == "Use 'MATERIAL=X' for 'plastic or composite'."
    )
    # Non-string codes and padded labels survive intact.
    assert layers_toolbox.coded_hint("STATUS", 1, "  Active  ") == "Use 'STATUS=1' for 'active'."


@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime(2024, 11, 30, 16, 16, 48, 1), "2024-11-30 16:16:48"),
        (datetime(2024, 11, 30, 23, 59, 59, 999999), "2024-11-30 23:59:59"),
        (
            datetime(2024, 11, 30, 16, 16, 48, 123456, tzinfo=timezone(timedelta(hours=-4))),
            "2024-11-30 16:16:48-04:00",
        ),
        (time(16, 16, 48, 123456), "16:16:48"),
        (date(2024, 11, 30), "2024-11-30"),
        ("version 1.000001", "version 1.000001"),
    ],
)
def test_sample_timestamps_stop_at_seconds(layers_toolbox, value, expected):
    from layers_json.catalog import CatalogBuilder

    assert str(CatalogBuilder()._round(value)) == expected
    assert str(layers_toolbox.PrepareTool()._round(value)) == expected


def test_fractional_dates_merge_before_sample_limit(layers_toolbox):
    class FeatureLayer:
        def query(self, **_):
            return {
                "features": [
                    {"attributes": {"recorded": stamp}}
                    for stamp in [
                        datetime(2024, 11, 30, 16, 16, 48, 1),
                        datetime(2024, 11, 30, 16, 16, 48, 999999),
                        datetime(2024, 11, 30, 16, 16, 49, 1),
                    ]
                ]
            }

    tool = layers_toolbox.PrepareTool()
    tool.max_records = 3
    tool.max_values = 2
    column = layers_toolbox.Column("recorded", "recorded", "Date")
    tool._desc_values_web(FeatureLayer(), "Records", [column])
    assert column.values == ["2024-11-30 16:16:48", "2024-11-30 16:16:49"]
