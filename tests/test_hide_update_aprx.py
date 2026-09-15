from __future__ import annotations

import json
import zipfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from toolbox_support import load_toolbox

from layers_json import hide_update
from layers_json import hide_update_aprx as script


def _rules(tmp_path: Path) -> script.Rules:
    config = tmp_path / "HideUpdateTool.json"
    config.write_text(
        json.dumps(
            {
                "include_layers": [r"Group\Wells"],
                "exclude_layers": [],
                "exclude_fields": [r".*_id$"],
                "field_aliases": [[r".+\.(.+)_hc_(.+)", r"\1 hydrocarbon \2"]],
            }
        ),
        encoding="utf-8",
    )
    return script.load_rules(config)


def _project(tmp_path: Path) -> Path:
    aprx = tmp_path / "NorthSea.aprx"
    fields = [
        {"fieldName": "OBJECTID", "alias": "Object Id", "visible": False},
        {"fieldName": "Shape", "alias": "Shape", "visible": False},
        {"fieldName": "WellType", "alias": "Well Type", "visible": False},
        {"fieldName": "Shape_Length", "alias": "Shape Length", "visible": True},
        {"fieldName": "owner_id", "alias": "Owner Id", "visible": True},
        {"fieldName": "oil_hc_volume", "alias": "Oil Hc Volume", "visible": True},
        {"fieldName": "hidden_hc_value", "alias": "Original", "visible": False},
    ]
    layer = {
        "type": "CIMFeatureLayer",
        "name": "Wells",
        "layerType": "Operational",
        "featureTable": {"fieldDescriptions": fields, "dataConnection": {}},
    }
    group = {
        "type": "CIMGroupLayer",
        "name": "Group",
        "layers": ["CIMPATH=Map/Wells.json"],
    }
    with zipfile.ZipFile(aprx, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Map/Map.json",
            json.dumps({"type": "CIMMap", "name": "Map", "layers": ["CIMPATH=Map/Group.json"]}),
        )
        archive.writestr("Map/Group.json", json.dumps(group))
        archive.writestr("Map/Wells.json", json.dumps(layer))
        archive.writestr("untouched.bin", b"same bytes")
    return aprx


def test_update_project_matches_toolbox_precedence_and_preserves_source(tmp_path: Path) -> None:
    source = _project(tmp_path)
    original = source.read_bytes()
    output = tmp_path / "NorthSea.updated.aprx"
    schema = script.Schema(
        visible=frozenset({"objectid", "shape", "welltype"}),
        hidden=frozenset({"shape_length"}),
    )

    changes, skipped = script.update_project(
        source,
        output,
        _rules(tmp_path),
        schema_reader=lambda _doc, _parent: schema,
    )

    assert source.read_bytes() == original
    assert skipped == []
    assert changes == [
        script.LayerChange(
            r"Group\Wells",
            ("Shape_Length", "owner_id"),
            (("oil_hc_volume", "Oil Hydrocarbon Volume"),),
        )
    ]
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert archive.read("untouched.bin") == b"same bytes"
        doc = json.loads(archive.read("Map/Wells.json"))
    fields = {field["fieldName"]: field for field in doc["featureTable"]["fieldDescriptions"]}
    assert all(fields[name]["visible"] for name in ("OBJECTID", "Shape", "WellType"))
    assert not fields["Shape_Length"]["visible"]
    assert not fields["owner_id"]["visible"]
    assert fields["oil_hc_volume"]["alias"] == "Oil Hydrocarbon Volume"
    # The toolbox never updates an alias on a field that was already hidden.
    assert fields["hidden_hc_value"]["alias"] == "Original"


def test_load_rules_rejects_invalid_regex(tmp_path: Path) -> None:
    config = tmp_path / "HideUpdateTool.json"
    config.write_text('{"exclude_fields": ["["]}', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid exclude_fields regex"):
        script.load_rules(config)


def test_rule_application_matches_the_toolbox_adapter(tmp_path: Path) -> None:
    rules = _rules(tmp_path)
    fields = [
        {"fieldName": "OBJECTID", "alias": "Object Id", "visible": False},
        {"fieldName": "Shape", "alias": "Shape", "visible": False},
        {"fieldName": "WellType", "alias": "Well Type", "visible": False},
        {"fieldName": "Shape_Length", "alias": "Shape Length", "visible": True},
        {"fieldName": "owner_id", "alias": "Owner Id", "visible": True},
        {"fieldName": "oil_hc_volume", "alias": "Oil Hc Volume", "visible": True},
        {"fieldName": "hidden_hc_value", "alias": "Original", "visible": False},
    ]
    schema = script.Schema(
        visible=frozenset({"objectid", "shape", "welltype"}),
        hidden=frozenset({"shape_length"}),
    )
    standalone = {"featureTable": {"fieldDescriptions": deepcopy(fields)}}
    script.apply_rules(standalone, r"Group\Wells", schema, rules)

    toolbox = load_toolbox()
    tool = toolbox.HideUpdateTool()
    tool.rules = rules
    cim = SimpleNamespace(
        featureTable=SimpleNamespace(
            fieldDescriptions=[SimpleNamespace(**field) for field in deepcopy(fields)]
        )
    )
    layer = SimpleNamespace(
        getDefinition=lambda _version: cim,
        setDefinition=lambda value: setattr(layer, "definition", value),
    )
    desc = SimpleNamespace(
        fields=[
            SimpleNamespace(name=field["fieldName"], aliasName=field["alias"], type="String")
            for field in fields
        ],
        OIDFieldName="OBJECTID",
        shapeFieldName="Shape",
        subtypeFieldName="WellType",
        areaFieldName="",
        lengthFieldName="Shape_Length",
        globalIDFieldName="",
    )
    with patch.object(toolbox.arcpy, "Describe", return_value=desc, create=True):
        tool._process_layer(layer, r"Group\Wells")

    toolbox_fields = [vars(field) for field in layer.definition.featureTable.fieldDescriptions]
    assert standalone["featureTable"]["fieldDescriptions"] == toolbox_fields


def test_update_project_does_not_write_when_every_layer_is_unsupported(tmp_path: Path) -> None:
    source = _project(tmp_path)
    output = tmp_path / "NorthSea.updated.aprx"

    def unsupported(_doc, _parent):
        raise ValueError("unsupported FeatureService workspace")

    with pytest.raises(ValueError, match="no feature layers selected"):
        script.update_project(source, output, _rules(tmp_path), schema_reader=unsupported)

    assert not output.exists()


def test_multi_map_project_requires_an_explicit_map(tmp_path: Path) -> None:
    source = _project(tmp_path)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(
            "Other/Other.json",
            json.dumps({"type": "CIMMap", "name": "Other", "layers": []}),
        )
        archive.writestr(
            "Index.json",
            json.dumps(
                {
                    "Nodes": [
                        {"NodeType": "Map", "FileName": "Map/Map.json"},
                        {"NodeType": "Map", "FileName": "Other/Other.json"},
                    ]
                }
            ),
        )
    output = tmp_path / "NorthSea.updated.aprx"
    schema = script.Schema(frozenset(), frozenset())

    with pytest.raises(ValueError, match="multiple maps"):
        script.update_project(
            source,
            output,
            _rules(tmp_path),
            schema_reader=lambda _doc, _parent: schema,
        )

    changes, _skipped = script.update_project(
        source,
        output,
        _rules(tmp_path),
        map_name="Map",
        schema_reader=lambda _doc, _parent: schema,
    )
    assert [change.name for change in changes] == [r"Group\Wells"]


def test_malformed_gdb_definition_falls_back_to_ogr_schema() -> None:
    fallback = script.Schema(frozenset({"objectid"}), frozenset({"blob"}))

    assert script._merge_definition(fallback, "<broken") == fallback


def test_sde_is_skipped_without_exact_arcpy_field_metadata(tmp_path: Path) -> None:
    doc = {
        "featureTable": {
            "dataConnection": {
                "workspaceFactory": "SDE",
                "workspaceConnectionString": "DATABASE=x",
                "dataset": "public.places",
            }
        }
    }

    with pytest.raises(ValueError, match="unsupported SDE workspace"):
        script.describe_schema(doc, tmp_path, {}, {}, {})


def test_main_keeps_output_beside_source_to_preserve_relative_connections(
    tmp_path: Path,
) -> None:
    source = _project(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        script.main([str(source), "-o", str(tmp_path / "elsewhere" / "updated.aprx")])

    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    "config",
    [
        {"exclude_fields": ["["]},
        {"field_aliases": [["(", "Alias"]]},
        {"field_aliases": [["(.*)", r"\2"]]},
        {"field_aliases": [["(.*)", r"\g<missing>"]]},
        {"include_layers": "Wells"},
    ],
)
def test_both_entry_points_reject_invalid_configuration(config, tmp_path):
    path = tmp_path / "HideUpdateTool.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        script.load_rules(path)
    toolbox = load_toolbox()
    tool = toolbox.HideUpdateTool()
    # Validate the shared parser used by the UI as well as the file reader.
    with pytest.raises(ValueError):
        toolbox.rules_from_config(config)
    assert tool.rules == hide_update.rules_from_config({})


def test_shared_rules_roundtrip_and_exclusion_precedence():
    config = {
        "include_layers": ["Wells", "Pipes"],
        "exclude_layers": ["Pipes"],
        "exclude_fields": [r".*_id$"],
        "field_aliases": [[r".*\.(.*)", r"\1"]],
    }
    rules = hide_update.rules_from_config(config)
    assert rules.includes_layer("Wells")
    assert not rules.includes_layer("Pipes")
    assert not rules.includes_layer("Other")
    assert hide_update.rules_from_config(rules.to_config()) == rules


def test_rule_precedence_casefold_first_match_and_idempotence():
    rules = hide_update.rules_from_config(
        {
            "exclude_fields": [r".*\.(OBJECTID|secret)$"],
            "field_aliases": [[r".*\.(.*)", r"first_\1"], [r".*", "Second"]],
        }
    )
    doc = {
        "featureTable": {
            "fieldDescriptions": [
                {"fieldName": "ObjectID", "visible": False},
                {"fieldName": "secret", "visible": True},
                {"fieldName": "blob", "visible": True},
                {"fieldName": "already_hidden", "visible": False, "alias": "Keep"},
                {"fieldName": "oil_volume", "visible": True, "alias": "Old"},
            ]
        }
    }
    schema = hide_update.Schema(frozenset({"objectid"}), frozenset({"blob"}))
    change = hide_update.apply_rules(doc, "Wells", schema, rules)
    fields = doc["featureTable"]["fieldDescriptions"]
    assert fields[0]["visible"] is True
    assert change.hidden == ("secret", "blob")
    assert fields[3]["alias"] == "Keep"
    assert change.aliases == (("oil_volume", "First Oil Volume"),)
    assert hide_update.apply_rules(doc, "Wells", schema, rules) == hide_update.LayerChange(
        "Wells", (), ()
    )


def test_toolbox_converts_value_tables_to_shared_config():
    tool = load_toolbox().HideUpdateTool()
    rows = [[r".*\.(.*)", r"\1"]]
    value_table = SimpleNamespace(rowCount=1, getValue=lambda row, col: rows[row][col])
    assert tool._parameter_rows(value_table, 2) == rows
    assert tool._parameter_rows(value_table, 1) == [rows[0][0]]
    assert tool._parameter_rows(None, 1) == []


def test_entry_points_import_common_rules_without_calling_each_other():
    toolbox = load_toolbox()
    assert script.apply_rules is toolbox.apply_rules is hide_update.apply_rules
    assert script.build_schema is toolbox.build_schema is hide_update.build_schema
    assert toolbox.rules_from_config is hide_update.rules_from_config


@pytest.mark.parametrize("column,value", [(2, ["["]), (3, [["(.*)", r"\2"]])])
def test_tool_execute_rejects_bad_rules_before_accessing_project(column, value):
    toolbox = load_toolbox()
    tool = toolbox.HideUpdateTool()
    parameters = [SimpleNamespace(value=None, valueAsText=None) for _ in range(4)]
    parameters[column].value = value
    with (
        patch.object(toolbox.arcpy, "env", SimpleNamespace(), create=True),
        patch.object(toolbox.arcpy, "mp", create=True) as mapping,
    ):
        with pytest.raises(ValueError):
            tool.execute(parameters, None)
        mapping.ArcGISProject.assert_not_called()


@pytest.mark.parametrize("initial_visible,expected_writes", [(True, 0), (False, 1)])
def test_tool_writes_only_changed_cim_including_restored_visibility(
    initial_visible, expected_writes
):
    toolbox = load_toolbox()
    tool = toolbox.HideUpdateTool()
    field = SimpleNamespace(fieldName="OBJECTID", alias="Object ID", visible=initial_visible)
    cim = SimpleNamespace(featureTable=SimpleNamespace(fieldDescriptions=[field]))
    writes = []
    layer = SimpleNamespace(getDefinition=lambda _: cim, setDefinition=writes.append)
    desc = SimpleNamespace(
        fields=[SimpleNamespace(name="OBJECTID", aliasName="Object ID", type="OID")],
        OIDFieldName="OBJECTID",
        shapeFieldName="",
        subtypeFieldName="",
        areaFieldName="",
        lengthFieldName="",
        globalIDFieldName="",
    )
    with patch.object(toolbox.arcpy, "Describe", return_value=desc, create=True):
        tool._process_layer(layer, "Wells")
    assert field.visible is True
    assert len(writes) == expected_writes


@pytest.mark.parametrize("initial", [None, []])
def test_empty_cim_is_initialized_and_rules_applied(initial):
    toolbox = load_toolbox()
    tool = toolbox.HideUpdateTool()
    tool.rules = hide_update.rules_from_config({"field_aliases": [[r"Wells.name$", "Well Name"]]})
    cim = SimpleNamespace(featureTable=SimpleNamespace(fieldDescriptions=initial))
    writes = []
    layer = SimpleNamespace(getDefinition=lambda _: cim, setDefinition=writes.append)
    desc = SimpleNamespace(
        fields=[
            SimpleNamespace(name="OBJECTID", aliasName="Object ID", type="OID"),
            SimpleNamespace(name="name", aliasName="Source Name", type="String"),
        ],
        OIDFieldName="OBJECTID",
        shapeFieldName="",
        subtypeFieldName="",
        areaFieldName="",
        lengthFieldName="",
        globalIDFieldName="",
    )
    factory = SimpleNamespace(
        CreateCIMObjectFromClassName=lambda *_: SimpleNamespace(alias=None, visible=False)
    )
    with patch.object(toolbox.arcpy, "Describe", return_value=desc, create=True):
        with patch.object(toolbox.arcpy, "cim", factory, create=True):
            tool._process_layer(layer, "Wells")
            assert len(writes) == 1
            fields = cim.featureTable.fieldDescriptions
            assert [(f.fieldName, f.alias, f.visible) for f in fields] == [
                ("OBJECTID", "Object ID", True),
                ("name", "Well Name", True),
            ]
            tool._process_layer(layer, "Wells")
            assert len(writes) == 1


def test_script_initializes_missing_field_descriptions(tmp_path):
    source = _project(tmp_path)
    output = tmp_path / "updated.aprx"
    doc = {"featureTable": {"fieldDescriptions": []}}
    schema = script.Schema(
        frozenset({"objectid"}),
        frozenset(),
        (("OBJECTID", "Object ID"), ("oil_hc_volume", "Volume")),
    )
    with patch.object(
        script, "iter_feature_layers", return_value=iter([("Map/Wells.json", doc, r"Group\Wells")])
    ):
        changes, skipped = script.update_project(
            source, output, _rules(tmp_path), schema_reader=lambda *_: schema
        )
    assert not skipped
    assert changes[0].aliases == (("oil_hc_volume", "Oil Hydrocarbon Volume"),)
    with zipfile.ZipFile(output) as archive:
        fields = json.loads(archive.read("Map/Wells.json"))["featureTable"]["fieldDescriptions"]
    assert fields[0] == {
        "type": "CIMFieldDescription",
        "fieldName": "OBJECTID",
        "alias": "Object ID",
        "visible": True,
    }
    assert fields[1]["alias"] == "Oil Hydrocarbon Volume"


def test_cancelled_tool_does_not_edit_or_save_parameters(tmp_path):
    toolbox = load_toolbox()
    tool = toolbox.HideUpdateTool()
    parameters = [SimpleNamespace(value=None, valueAsText=None) for _ in range(4)]
    project = SimpleNamespace(
        homeFolder=str(tmp_path), activeMap=SimpleNamespace(listLayers=lambda: [object()])
    )
    with patch.object(toolbox.arcpy, "mp", create=True) as mapping:
        mapping.ArcGISProject.return_value = project
        with patch.object(toolbox.arcpy, "env", SimpleNamespace(isCancelled=True), create=True):
            with patch.object(tool, "_process_layer") as process:
                tool.execute(parameters, None)
                process.assert_not_called()
    assert not (tmp_path / "HideUpdateTool.json").exists()
