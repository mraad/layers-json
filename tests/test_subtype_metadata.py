"""Subtype labels must survive every catalog extraction path."""

from types import SimpleNamespace
from unittest.mock import patch

from toolbox_support import load_toolbox


def test_local_subtypes_take_precedence_over_field_domains():
    toolbox = load_toolbox()
    field = SimpleNamespace(name="KIND", type="Integer", aliasName="Kind", domain="Kinds")
    desc = SimpleNamespace(fields=[field], subtypeFieldName="kind", catalogPath="places")
    domain = SimpleNamespace(domainType="CodedValue", codedValues={1: "Wrong label"})
    with patch.object(
        toolbox.arcpy.da, "ListSubtypes", return_value={1: {"Name": "Hospitals"}}, create=True
    ):
        (column,) = toolbox.PrepareTool()._desc_columns("Places", desc, {"Kinds": domain})
    assert column.keyval == {"1": "Hospitals"}
    assert column.hints == ["Use 'KIND = cast(1 as INTEGER)' for 'hospitals'."]
    assert not any("Wrong label" in hint for hint in column.hints)


def test_service_subtypes_are_extracted_before_domains():
    toolbox = load_toolbox()

    class Properties(dict):
        __getattr__ = dict.__getitem__

    field = SimpleNamespace(
        name="KIND",
        type="esriFieldTypeInteger",
        alias="Kind",
        domain=SimpleNamespace(
            type="codedValue", codedValues=[SimpleNamespace(code=1, name="Wrong label")]
        ),
    )
    props = Properties(
        fields=[field],
        typeIdField="kind",
        geometryProperties=None,
        types=[{"id": 1, "name": "Hospitals"}],
    )
    (column,) = toolbox.PrepareTool()._desc_columns_web(SimpleNamespace(properties=props), "Places")
    assert column.keyval == {"1": "Hospitals"}
    assert column.hints == ["Use 'KIND = cast(1 as INTEGER)' for 'hospitals'."]
    assert not any("Wrong label" in hint for hint in column.hints)


def test_ogr_subtypes_take_precedence_over_field_domains():
    from layers_json.layers_from_aprx import GdbItem, describe_columns

    toolbox = load_toolbox()
    field = SimpleNamespace(
        GetName=lambda: "KIND", GetAlternativeName=lambda: "Kind", GetDomainName=lambda: "Kinds"
    )
    defn = SimpleNamespace(GetFieldCount=lambda: 1, GetFieldDefn=lambda i: field)
    layer = SimpleNamespace(GetLayerDefn=lambda: defn)
    item = GdbItem(subtype_field="kind", subtypes={"1": "Hospitals"})
    with (
        patch("layers_json.layers_from_aprx.field_dtype", return_value="Integer"),
        patch(
            "layers_json.layers_from_aprx._domain_values", return_value=({"1": "Wrong label"}, [])
        ),
    ):
        (column,) = describe_columns(
            toolbox.PrepareTool(),
            None,
            layer,
            SimpleNamespace(hidden=set(), aliases={}),
            item,
        )
    assert column.keyval == {"1": "Hospitals"}
    assert column.hints == ["Use 'KIND = cast(1 as INTEGER)' for 'hospitals'."]
    assert not any("Wrong label" in hint for hint in column.hints)


def test_cast_hints_are_readable_without_sampled_subtype_rows():
    from collections import Counter

    toolbox = load_toolbox()
    field = SimpleNamespace(name="KIND", type="SmallInteger", aliasName="Kind", domain="")
    desc = SimpleNamespace(fields=[field], subtypeFieldName="kind", catalogPath="places")
    with patch.object(
        toolbox.arcpy.da,
        "ListSubtypes",
        return_value={
            1: {"Name": "Villa"},
            16: {"Name": "Hospital"},
            49: {"Name": "Sea / Park"},
        },
        create=True,
    ):
        columns = toolbox.PrepareTool()._desc_columns("Master", desc, {})
    toolbox.PrepareTool()._add_values_hints(columns, [Counter()])
    assert columns[0].hints == [
        "Use 'KIND = cast(1 as SMALLINT)' for 'villa'.",
        "Use 'KIND = cast(16 as SMALLINT)' for 'hospital'.",
        "Use 'KIND = cast(49 as SMALLINT)' for 'sea or park'.",
    ]
    toolbox.PrepareTool()._add_values_hints(columns, [Counter({"1": 10})])
    assert len([hint for hint in columns[0].hints if hint.startswith("Use ")]) == 3
    assert not any(">>" in hint for hint in columns[0].hints)


def test_numeric_and_string_hints_use_the_same_sentence():
    from collections import Counter

    toolbox = load_toolbox()
    column = toolbox.Column("distance", "distance from a hospital", "Double")
    toolbox.PrepareTool()._add_values_hints([column], [Counter({"100": 1})])
    assert column.hints[-1] == (
        "Use 'distance = cast(100 as DOUBLE PRECISION)' for 'distance from a hospital is 100'."
    )
    assert toolbox.coded_hint("MATERIAL", "O'Brien", "Owner's material", "VARCHAR") == (
        "Use 'MATERIAL = 'O''Brien'' for 'owner's material'."
    )


def test_subtype_alias_suffix_names_the_feature():
    toolbox = load_toolbox()
    assert toolbox.coded_hint("discovery_type", 3, "Oil", "INTEGER", suffix="discoveries") == (
        "Use 'discovery_type = cast(3 as INTEGER)' for 'oil discoveries'."
    )
    assert toolbox.coded_hint("content_type", 5, "OIL/GAS", "SMALLINT", suffix="Wells") == (
        "Use 'content_type = cast(5 as SMALLINT)' for 'oil or gas wells'."
    )
    # A label that already ends with the alias is not doubled.
    assert toolbox.coded_hint("KIND", 1, "Dry Wells", "INTEGER", suffix="wells") == (
        "Use 'KIND = cast(1 as INTEGER)' for 'dry wells'."
    )
    # Off by default: a table named `master` is not a noun anyone would say.
    prepare = toolbox.PrepareTool()
    field = SimpleNamespace(name="KIND", type="Integer", aliasName="Kind", domain="")
    desc = SimpleNamespace(
        fields=[field], subtypeFieldName="kind", catalogPath="places", aliasName="Discoveries"
    )
    with patch.object(
        toolbox.arcpy.da, "ListSubtypes", return_value={3: {"Name": "Oil"}}, create=True
    ):
        (column,) = prepare._desc_columns("Places", desc, {})
        assert column.hints == ["Use 'KIND = cast(3 as INTEGER)' for 'oil'."]
        prepare.subtype_alias_suffix = True
        (column,) = prepare._desc_columns("Places", desc, {})
    assert column.hints == ["Use 'KIND = cast(3 as INTEGER)' for 'oil discoveries'."]


def test_ogr_subtype_alias_suffix_matches_layer_alias():
    from layers_json.layers_from_aprx import GdbItem, describe_columns

    toolbox = load_toolbox()
    prepare = toolbox.PrepareTool()
    prepare.subtype_alias_suffix = True
    field = SimpleNamespace(
        GetName=lambda: "KIND", GetAlternativeName=lambda: "Kind", GetDomainName=lambda: ""
    )
    defn = SimpleNamespace(GetFieldCount=lambda: 1, GetFieldDefn=lambda i: field)
    layer = SimpleNamespace(GetLayerDefn=lambda: defn)
    item = GdbItem(subtype_field="kind", subtypes={"0": "DRY"}, alias="Well_Bores")
    with patch("layers_json.layers_from_aprx.field_dtype", return_value="SmallInteger"):
        (column,) = describe_columns(
            prepare,
            None,
            layer,
            SimpleNamespace(name="Wells", hidden=set(), aliases={}),
            item,
        )
    assert column.hints == ["Use 'KIND = cast(0 as SMALLINT)' for 'dry well bores'."]
