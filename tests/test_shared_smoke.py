"""Integration smoke checks for the shared modules and their entry points."""

import io
import json
import subprocess
import sys
import zipfile
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from toolbox_support import load_toolbox

from layers_json import aprx_to_duckdb, catalog, duckdb_export


def test_pro_adapter_uses_the_shared_catalog():
    toolbox = load_toolbox()
    assert toolbox.Column is catalog.Column
    assert toolbox.Layer is catalog.Layer
    assert toolbox.Layers is catalog.Layers
    assert toolbox.PrepareTool._add_values_hints is catalog.CatalogBuilder._add_values_hints
    assert aprx_to_duckdb.write_batch is duckdb_export.write_batch


def test_offline_commands_with_real_file_geodatabase(tmp_path):
    ogr = pytest.importorskip("osgeo.ogr")
    osr = pytest.importorskip("osgeo.osr")
    ds = ogr.GetDriverByName("OpenFileGDB").CreateDataSource(str(tmp_path / "Survey.gdb"))
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(4326)
    layer = ds.CreateLayer("Wellbores", srs=sr, geom_type=ogr.wkbPoint)
    layer.CreateField(ogr.FieldDefn("well_name", ogr.OFTString))
    for name in ["Alpha", "Beta"]:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("well_name", name)
        feature.SetGeometry(ogr.CreateGeometryFromWkt("POINT (1 2)"))
        layer.CreateFeature(feature)
    feature = layer = ds = None
    aprx = tmp_path / "Survey.aprx"
    doc = {
        "type": "CIMFeatureLayer",
        "name": "Wells",
        "layerType": "Operational",
        "featureTable": {
            "displayField": "well_name",
            "fieldDescriptions": [
                {"fieldName": "well_name", "alias": "Well Name", "visible": True}
            ],
            "dataConnection": {
                "type": "CIMStandardDataConnection",
                "workspaceFactory": "FileGDB",
                "workspaceConnectionString": "DATABASE=./Survey.gdb",
                "dataset": "Wellbores",
            },
        },
    }
    with zipfile.ZipFile(aprx, "w") as archive:
        archive.writestr(
            "Map/Map.json",
            json.dumps({"type": "CIMMap", "name": "Map", "layers": ["CIMPATH=Map/Wells.json"]}),
        )
        archive.writestr("Map/Wells.json", json.dumps(doc))
    original = aprx.read_bytes()
    # A fresh process actively refuses Pro-only imports and any .pyt loading.
    code = """
import importlib.abc, importlib.machinery, sys
class NoPro(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'arcpy', 'arcgis', 'requests', 'duckdb', 'pydantic'}:
            raise AssertionError('unexpected dependency: ' + fullname)
sys.meta_path.insert(0, NoPro())
original = importlib.machinery.SourceFileLoader.exec_module
def exec_module(self, module):
    assert not self.path.endswith('.pyt'), self.path
    return original(self, module)
importlib.machinery.SourceFileLoader.exec_module = exec_module
from layers_json import layers_from_aprx, okf_from_aprx, hide_update_aprx
assert layers_from_aprx.main([sys.argv[1], '-o', sys.argv[2]]) == 0
assert okf_from_aprx.main([sys.argv[1], '-o', sys.argv[3]]) == 0
assert hide_update_aprx.main([sys.argv[1], '-c', sys.argv[4]]) == 0
"""
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"field_aliases": [[r"Wells\.well_name", "Borehole name"]]}))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(aprx),
            str(tmp_path / "catalog"),
            str(tmp_path / "knowledge"),
            str(rules),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    saved = json.loads((tmp_path / "catalog/Layers.json").read_text())
    well = saved["layers"][0]
    assert well["table_name"] == "Wells"
    assert well["columns"][0]["alias"] == "well name"
    assert set(well["columns"][0]["values"]) == {"Alpha", "Beta"}
    assert (
        "Well Name" in (tmp_path / "knowledge/Wells.md").read_text()
        or "well name" in (tmp_path / "knowledge/Wells.md").read_text()
    )
    assert (tmp_path / "knowledge/index.md").is_file()
    assert aprx.read_bytes() == original
    with zipfile.ZipFile(tmp_path / "Survey.updated.aprx") as archive:
        updated = json.loads(archive.read("Map/Wells.json"))
    assert updated["featureTable"]["fieldDescriptions"][0]["alias"] == "Borehole Name"


def test_project_export_with_real_spatial_database(tmp_path, monkeypatch):
    duckdb = pytest.importorskip("duckdb")
    rich = pytest.importorskip("rich.console")
    # Requires a locally installed extension; no network install during tests.
    with duckdb.connect() as conn:
        try:
            conn.execute("LOAD spatial")
        except duckdb.Error:
            pytest.skip("DuckDB spatial extension is not installed")
        wkb = conn.execute("SELECT ST_AsWKB(ST_Point(1, 2))").fetchone()[0]
    sr = SimpleNamespace(name="WGS 84", exportToString=lambda: "WGS84")
    fields = [
        SimpleNamespace(name=n, type=t, aliasName=n, domain="")
        for n, t in [("OBJECTID", "OID"), ("name", "String"), ("Shape", "Geometry")]
    ]
    desc = SimpleNamespace(
        fields=fields,
        OIDFieldName="OBJECTID",
        shapeFieldName="Shape",
        spatialReference=sr,
        extent=None,
    )
    selections = []
    layer = SimpleNamespace(
        name="Wells",
        isFeatureLayer=True,
        definitionQuery="OBJECTID > 0",
        setSelectionSet=lambda *args: selections.append(args),
    )
    map_obj = SimpleNamespace(name="Map", listLayers=lambda: [layer], listTables=lambda: [])
    arcpy = SimpleNamespace(
        mp=SimpleNamespace(ArcGISProject=lambda _: SimpleNamespace(listMaps=lambda: [map_obj])),
        SpatialReference=lambda _: sr,
        Describe=lambda _: desc,
        ListTransformations=lambda *a: [],
        EnvManager=lambda **kw: nullcontext(),
        management=SimpleNamespace(GetCount=lambda _: [2]),
        da=SimpleNamespace(
            SearchCursor=lambda *a, **kw: nullcontext(iter([(1, "Alpha", wkb), (2, None, None)]))
        ),
    )
    monkeypatch.setitem(sys.modules, "arcpy", arcpy)
    aprx = tmp_path / "Survey.aprx"
    aprx.write_bytes(b"project is opened by the ArcPy test double")
    console = rich.Console(file=io.StringIO())
    output = aprx_to_duckdb.export_project(aprx, batch_size=1, console=console)
    with duckdb.connect(str(output), read_only=True) as conn:
        conn.execute("LOAD spatial")
        assert conn.execute(
            "SELECT OBJECTID, name, ST_AsText(geometry) FROM Wells ORDER BY OBJECTID"
        ).fetchall() == [(1, "Alpha", "POINT (1 2)"), (2, None, None)]
        assert conn.execute("SELECT wkid FROM sp_ref").fetchone() == (4326,)
        metadata = json.loads(conn.execute("SELECT metadata FROM _export_layers").fetchone()[0])
        assert metadata["rows"] == 2 and metadata["null_geometries"] == 1
        assert conn.execute("SELECT index_name FROM duckdb_indexes()").fetchall() == [
            ("Wells_rtree",)
        ]
    assert selections == [([], "NEW")]
    assert not list(tmp_path.glob(".aprx-duckdb-*"))
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        aprx_to_duckdb.export_project(aprx, console=console)
    assert output.read_bytes() == before


def test_bundled_layers_toolbox_loads_without_site_packages(tmp_path):
    from pathlib import Path

    helper = Path(__file__).resolve().with_name("toolbox_support.py")
    code = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("toolbox_support", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
assert importlib.util.find_spec("layers_json") is None
module = helper.load_toolbox()
assert module.PrepareTool.__mro__[1].__module__ == "layers_json.catalog"
assert len(module.Toolbox().tools) == 4
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(helper)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
