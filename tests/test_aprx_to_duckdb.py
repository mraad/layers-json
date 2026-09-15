"""Data preservation and publication checks for the standalone APRX exporter."""

from contextlib import nullcontext
from datetime import date, datetime, time, timezone
from types import SimpleNamespace

import pyarrow as pa
import pytest

from layers_json import aprx_to_duckdb as export


def test_maps_tables_and_name_collisions():
    def feature(name):
        return SimpleNamespace(name=name, isFeatureLayer=True)

    table = SimpleNamespace(name="Water Device")  # ArcPy Table has no isTable property.
    map_obj = SimpleNamespace(
        name="Map",
        listLayers=lambda: [
            feature("Water Device"),
            feature("sp_ref"),
            SimpleNamespace(name="Base"),
        ],
        listTables=lambda: [table],
    )
    project = SimpleNamespace(listMaps=lambda: [map_obj, map_obj])
    items, skipped = export.collect_items(project)
    assert [info["table"] for _, info in items] == [
        "Water_Device",
        "sp_ref_2",
        "Water_Device_2",
        "Water_Device_3",
        "sp_ref_3",
        "Water_Device_4",
    ]
    assert len(skipped) == 2
    with pytest.raises(ValueError, match="No maps"):
        export.collect_items(project, "Missing")


def test_temporal_binary_and_oid_values():
    schema = pa.schema(
        [
            ("oid", pa.int64()),
            ("date", pa.timestamp("us")),
            ("time", pa.time64("us")),
            ("offset", pa.timestamp("us", tz="UTC")),
            ("blob", pa.binary()),
        ]
    )
    rows = [
        (
            2**40,
            date(2026, 1, 1),
            datetime(2026, 1, 1, 12),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            bytearray(b"abc"),
        )
    ]
    batch = export.arrow_batch(rows, schema, pa)
    assert batch.to_pylist() == [
        dict(
            oid=2**40,
            date=datetime(2026, 1, 1),
            time=time(12),
            offset=datetime(2026, 1, 1, tzinfo=timezone.utc),
            blob=b"abc",
        )
    ]
    assert export.arrow_batch([], schema, pa).schema == schema


def test_preserves_attributes_and_rejects_geometry_collision():
    fields = [
        SimpleNamespace(name=name, type=kind)
        for name, kind in [
            ("OBJECTID", "OID"),
            ("GlobalID", "GlobalId"),
            ("hidden", "String"),
            ("Shape", "Geometry"),
        ]
    ]
    desc = SimpleNamespace(fields=fields, OIDFieldName="OBJECTID", shapeFieldName="Shape")
    _, _, names, schema = export.describe_fields(desc, pa)
    assert names == ["OBJECTID", "GlobalID", "hidden", "SHAPE@WKB"]
    assert schema.field("OBJECTID").type == pa.int64()
    fields.append(SimpleNamespace(name="geometry", type="String"))
    with pytest.raises(ValueError, match="conflicts"):
        export.describe_fields(desc, pa)


def test_batches_empty_table_and_quoted_identifiers():
    duckdb = pytest.importorskip("duckdb")
    schema = pa.schema([('odd"field', pa.int64())])
    with duckdb.connect() as conn:
        export.write_batch(conn, 'odd"table', "", export.arrow_batch([], schema, pa), True)
        export.write_batch(
            conn, 'odd"table', "", export.arrow_batch([(2**40,), (None,)], schema, pa), False
        )
        assert conn.execute('SELECT * FROM "odd""table"').fetchall() == [(2**40,), (None,)]


def test_count_mismatch_fails():
    duckdb = pytest.importorskip("duckdb")
    field = SimpleNamespace(name="id", type="OID", aliasName="id", domain="")
    desc = SimpleNamespace(fields=[field], OIDFieldName="id")
    arcpy = SimpleNamespace(
        Describe=lambda _: desc,
        management=SimpleNamespace(GetCount=lambda _: [2]),
        da=SimpleNamespace(SearchCursor=lambda *a, **kw: nullcontext(iter([(1,)]))),
    )
    with duckdb.connect() as conn, pytest.raises(RuntimeError, match="Source count 2, exported 1"):
        export.export_layer(
            conn, SimpleNamespace(), {"table": "data"}, arcpy, pa, None, 1, lambda *args: None
        )


def test_publish_never_clobbers_without_overwrite(tmp_path):
    staged, output = tmp_path / "staged", tmp_path / "output"
    staged.write_bytes(b"new")
    output.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        export.publish(staged, output, False)
    assert output.read_bytes() == b"old"
    export.publish(staged, output, True)
    assert output.read_bytes() == b"new"


def test_failed_export_leaves_existing_database(tmp_path, monkeypatch):
    pytest.importorskip("rich")
    duckdb = pytest.importorskip("duckdb")
    import sys

    aprx = tmp_path / "project.aprx"
    aprx.touch()
    output = aprx.with_suffix(".duckdb")
    output.write_bytes(b"original database")
    arcpy = SimpleNamespace(
        mp=SimpleNamespace(ArcGISProject=lambda _: object()),
        SpatialReference=lambda _: SimpleNamespace(exportToString=lambda: "WGS84"),
        EnvManager=lambda **kw: nullcontext(),
    )
    monkeypatch.setitem(sys.modules, "arcpy", arcpy)
    monkeypatch.setattr(
        export,
        "collect_items",
        lambda *a: ([(object(), {"map": "Map", "layer": "Bad", "table": "Bad"})], []),
    )

    def fail(*args):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(export, "export_layer", fail)
    # Keep this test offline: only the extension-loading SQL is bypassed.
    real_connect = duckdb.connect

    class Connection:
        def __enter__(self):
            self.conn = real_connect()
            return self

        def __exit__(self, *args):
            self.conn.close()

        def execute(self, sql, *args):
            if sql == "LOAD spatial":
                return self
            return self.conn.execute(sql, *args)

    monkeypatch.setattr(duckdb, "connect", lambda *a, **kw: Connection())
    with pytest.raises(RuntimeError, match="source unavailable"):
        export.export_project(aprx, overwrite=True)
    assert output.read_bytes() == b"original database"
    assert not list(tmp_path.glob(".aprx-duckdb-*"))


def test_missing_arcpy_exits_without_traceback_or_output(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.delitem(sys.modules, "arcpy", raising=False)
    real_find = export.importlib.util.find_spec
    monkeypatch.setattr(
        export.importlib.util,
        "find_spec",
        lambda name: None if name == "arcpy" else real_find(name),
    )
    aprx = tmp_path / "project.aprx"
    aprx.touch()
    assert export.main([str(aprx)]) == 1
    captured = capsys.readouterr()
    assert "ArcPy is not available" in captured.err
    assert "ArcGIS Pro" in captured.err
    assert "Traceback" not in captured.err
    assert not aprx.with_suffix(".duckdb").exists()
